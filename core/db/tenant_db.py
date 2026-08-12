"""
Tenant Database Factory
Creates and manages independent database connections (MetadataDB + VectorDB) per tenant
"""
import contextlib
import json
import logging
import sqlite3
import threading
from collections.abc import Generator
from pathlib import Path

from ..config import settings

logger = logging.getLogger(__name__)

# CJK Unified Ideographs Basic Block (Unicode U+4E00 ~ U+9FFF)
# Covers the vast majority of common Chinese characters; used to distinguish continuous Chinese character strings from English/digits/symbols
CJK_START = '\u4e00'  # U+4E00 (CJK start)
CJK_END = '\u9fff'

# =============================================================================
# Tenant-level Metadata Database
# =============================================================================

class TenantMetadataDB:
    """Tenant-level SQLite Metadata Database"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_schema()
        self._run_migrations()

    @contextlib.contextmanager
    def get_connection(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        # WAL mode: writers don't block readers, and vice versa.
        # Increase autocheckpoint threshold from default 1000 pages (~4 MB)
        # to 20000 pages (~80 MB) so checkpoints are less frequent, avoiding
        # write-stall 500 errors during sustained query loads.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=20000")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self):
        with self.get_connection() as conn:
            cursor = conn.cursor()

            # Document master table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    original_path TEXT,
                    title TEXT,
                    doc_type TEXT,
                    skill_id TEXT,
                    topic_tags TEXT,
                    metadata_json TEXT,
                    status TEXT DEFAULT 'pending_meta',
                    file_hash TEXT,
                    is_mixed BOOLEAN DEFAULT FALSE,
                    text_source TEXT DEFAULT 'direct_extract',
                    default_permission TEXT DEFAULT 'private',
                    category_level1 TEXT,
                    category_level2 TEXT,
                    category_level3 TEXT,
                    industry_package_id TEXT,
                    summary TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Page table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS doc_pages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT REFERENCES documents(id),
                    page_num INTEGER,
                    section_title TEXT,
                    section_level INTEGER DEFAULT 0,
                    section_path TEXT,
                    is_table_header BOOLEAN DEFAULT 0,
                    table_caption TEXT,
                    page_summary TEXT,
                    entities TEXT,
                    content_json TEXT,
                    raw_text TEXT,
                    page_type TEXT DEFAULT 'text_body',
                    text_source TEXT DEFAULT 'direct_extract',
                    ocr_confidence REAL,
                    page_image_path TEXT,
                    extra_data TEXT
                )
            """)
            # Migration: add extra_data column to existing tables (OpenLAD Hook mechanism: generic storage)
            try:
                cursor.execute("ALTER TABLE doc_pages ADD COLUMN extra_data TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
            # Legacy field schematic_data (for reading existing data)
            try:
                cursor.execute("ALTER TABLE doc_pages ADD COLUMN schematic_data TEXT")
            except sqlite3.OperationalError:
                pass

            # Structure index table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS doc_structure_index (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT REFERENCES documents(id),
                    section_path TEXT NOT NULL,
                    section_title TEXT NOT NULL,
                    section_level INTEGER,
                    start_page INTEGER,
                    end_page INTEGER,
                    section_type TEXT,
                    parent_path TEXT,
                    keywords TEXT,
                    summary TEXT
                )
            """)

            # Spec facts table: structured (entity, attribute, value) assertions
            # extracted from authoritative page text (NEVER from VLM descriptions).
            # Assertion-level index so spec queries ("X 的 Y 是多少") bypass
            # page/chapter granularity entirely — the missing abstraction layer
            # that vector-hybrid / VLM-penalty / chapter-scope patches compensate for.
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS spec_facts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT REFERENCES documents(id),
                    entity TEXT,
                    attribute TEXT,
                    value TEXT,
                    unit TEXT,
                    page_num INTEGER,
                    source_text TEXT,
                    extractor TEXT DEFAULT 'rule',
                    verified INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_spec_facts_doc ON spec_facts(doc_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_spec_facts_attr ON spec_facts(attribute)")

            # Migration: per-section identifier inventory (e.g. UART0-UART9)
            # so instance-level queries can match the right chapter.
            try:
                cursor.execute("ALTER TABLE doc_structure_index ADD COLUMN entities TEXT")
            except sqlite3.OperationalError:
                pass

            # Fragment table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS doc_fragments (
                    id TEXT PRIMARY KEY,
                    doc_id TEXT REFERENCES documents(id),
                    start_page INTEGER NOT NULL,
                    end_page INTEGER NOT NULL,
                    fragment_type TEXT,
                    skill_id TEXT,
                    summary TEXT,
                    status TEXT DEFAULT 'pending_meta',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Query log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS query_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    query TEXT NOT NULL,
                    query_hash TEXT,
                    intent TEXT,
                    industry_package_id TEXT,
                    elapsed_ms INTEGER,
                    results_count INTEGER,
                    answer_length INTEGER,
                    trace_json TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Audit log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    resource_type TEXT,
                    resource_id TEXT,
                    user_id TEXT,
                    tenant_id TEXT,
                    details TEXT,
                    ip_address TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Session table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    title TEXT DEFAULT 'New Chat',
                    industry TEXT DEFAULT 'auto',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Message table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS chat_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT REFERENCES chat_sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sources TEXT,
                    query_info TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # FTS5 virtual table (page-level, kept for compatibility)
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS doc_pages_fts USING fts5(raw_text, tokenize='trigram')
            """)

            # OpenLAD: Chunk-level storage table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS doc_chunks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT REFERENCES documents(id),
                    page_id INTEGER REFERENCES doc_pages(id),
                    page_num INTEGER,
                    chunk_idx INTEGER,
                    section_path TEXT,
                    section_title TEXT,
                    chunk_text TEXT,
                    chunk_text_preview TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # OpenLAD: Chunk-level FTS virtual table
            cursor.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS doc_chunks_fts USING fts5(chunk_text, tokenize='trigram')
            """)

            # V4.7: Upload task status table (replaces in-memory _upload_tasks)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS upload_tasks (
                    task_id TEXT PRIMARY KEY,
                    doc_id TEXT,
                    tenant_id TEXT,
                    filename TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    progress INTEGER DEFAULT 0,
                    message TEXT DEFAULT 'Waiting for processing',
                    result TEXT,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Indexes
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_doc_pages_doc_id ON doc_pages(doc_id)",
                "CREATE INDEX IF NOT EXISTS idx_doc_pages_page_num ON doc_pages(page_num)",
                "CREATE INDEX IF NOT EXISTS idx_query_log_hash ON query_log(query_hash)",
                "CREATE INDEX IF NOT EXISTS idx_doc_fragments_doc_id ON doc_fragments(doc_id)",
                "CREATE INDEX IF NOT EXISTS idx_chat_messages_session ON chat_messages(session_id)",
                "CREATE INDEX IF NOT EXISTS idx_chat_sessions_updated ON chat_sessions(updated_at)",
                "CREATE INDEX IF NOT EXISTS idx_structure_doc_id ON doc_structure_index(doc_id)",
                "CREATE INDEX IF NOT EXISTS idx_structure_doc_path ON doc_structure_index(doc_id, section_path)",
                "CREATE INDEX IF NOT EXISTS idx_documents_category ON documents(category_level1)",
                "CREATE INDEX IF NOT EXISTS idx_documents_industry ON documents(industry_package_id)",
                "CREATE INDEX IF NOT EXISTS idx_doc_chunks_doc_id ON doc_chunks(doc_id)",
                "CREATE INDEX IF NOT EXISTS idx_doc_chunks_page_id ON doc_chunks(page_id)",
                "CREATE INDEX IF NOT EXISTS idx_upload_tasks_tenant ON upload_tasks(tenant_id)",
                "CREATE INDEX IF NOT EXISTS idx_upload_tasks_status ON upload_tasks(status)",
            ]:
                cursor.execute(idx_sql)

            conn.commit()

    def _run_migrations(self):
        """Incremental migrations run on every database open"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            # V5.0: summary column is now used for document-level summary (L2 retrieval)
            # M001 removed (was dropping summary column, now needed)
            try:
                cursor.execute("ALTER TABLE documents ADD COLUMN summary TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

    # === Document CRUD ===
    def save_document(self, doc_id: str, **kwargs) -> str:
        # Handle special fields
        if "topic_tags" in kwargs and isinstance(kwargs["topic_tags"], list):
            kwargs["topic_tags"] = ",".join(kwargs["topic_tags"])
        if "metadata" in kwargs and isinstance(kwargs["metadata"], dict):
            kwargs["metadata_json"] = json.dumps(kwargs.pop("metadata"))
        # Build SQL
        cols = ", ".join(kwargs.keys())
        ph = ", ".join(["?"] * len(kwargs))
        with self.get_connection() as conn:
            conn.execute(f"""
                INSERT OR REPLACE INTO documents (id, {cols}, updated_at)
                VALUES (?, {ph}, CURRENT_TIMESTAMP)
            """, [doc_id] + list(kwargs.values()))
            conn.commit()
        return doc_id

    def get_document(self, doc_id: str) -> dict | None:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
            return _doc_from_row(row) if row else None

    def get_document_by_hash(self, file_hash: str) -> dict | None:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM documents WHERE file_hash = ?", (file_hash,)).fetchone()
            return _doc_from_row(row) if row else None

    def count_documents(self, status: str = None, industry_package_id: str = None) -> int:
        query = "SELECT COUNT(*) FROM documents WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if industry_package_id:
            query += " AND industry_package_id = ?"
            params.append(industry_package_id)
        with self.get_connection() as conn:
            row = conn.execute(query, params).fetchone()
            return row[0] if row else 0

    def count_pages(self) -> int:
        """Count total pages"""
        with self.get_connection() as conn:
            row = conn.execute("SELECT COUNT(*) FROM doc_pages").fetchone()
            return row[0] if row else 0

    def list_documents(self, status: str = None, industry_package_id: str = None,
                       skip: int = 0, limit: int = 100) -> list[dict]:
        query = "SELECT * FROM documents WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if industry_package_id:
            query += " AND industry_package_id = ?"
            params.append(industry_package_id)
        query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, skip])
        with self.get_connection() as conn:
            return [_doc_from_row(r) for r in conn.execute(query, params).fetchall()]

    def get_all_documents(self, status: str = None) -> list[dict]:
        """Get all documents (for internal components like Planner, no pagination)"""
        query = "SELECT * FROM documents WHERE 1=1"
        params = []
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY created_at DESC"
        with self.get_connection() as conn:
            return [_doc_from_row(r) for r in conn.execute(query, params).fetchall()]

    def delete_document(self, doc_id: str) -> bool:
        try:
            with self.get_connection() as conn:
                conn.execute("DELETE FROM doc_pages_fts WHERE rowid IN (SELECT id FROM doc_pages WHERE doc_id = ?)", (doc_id,))
                conn.execute("DELETE FROM doc_chunks_fts WHERE rowid IN (SELECT id FROM doc_chunks WHERE doc_id = ?)", (doc_id,))
                conn.execute("DELETE FROM doc_chunks WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM doc_pages WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM doc_structure_index WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM doc_fragments WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM spec_facts WHERE doc_id = ?", (doc_id,))
                conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
                conn.commit()
                return True
        except Exception as e:
            logger.error(f"[TenantDB] Failed to delete document: {e}")
            return False

    # === Page Operations ===
    def save_page(self, doc_id: str, page_num: int, raw_text: str = None, **kwargs) -> int:
        # Serialize JSON fields
        if "content_json" in kwargs and isinstance(kwargs["content_json"], dict):
            kwargs["content_json"] = json.dumps(kwargs["content_json"], ensure_ascii=False)
        if "entities" in kwargs and isinstance(kwargs["entities"], (list, dict)):
            kwargs["entities"] = json.dumps(kwargs["entities"], ensure_ascii=False)
        if "extra_data" in kwargs and isinstance(kwargs["extra_data"], dict):
            kwargs["extra_data"] = json.dumps(kwargs["extra_data"], ensure_ascii=False)
        if "schematic_data" in kwargs and isinstance(kwargs["schematic_data"], dict):
            kwargs["schematic_data"] = json.dumps(kwargs["schematic_data"], ensure_ascii=False)
        fields = ["doc_id", "page_num"] + list(kwargs.keys())
        if raw_text:
            fields.append("raw_text")
        values = [doc_id, page_num] + list(kwargs.values())
        if raw_text:
            values.append(raw_text)
        cols = ", ".join(fields)
        ph = ", ".join(["?"] * len(fields))
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"INSERT INTO doc_pages ({cols}) VALUES ({ph})", values)
            page_id = cursor.lastrowid
            if raw_text:
                try:
                    cursor.execute("INSERT INTO doc_pages_fts(rowid, raw_text) VALUES (?, ?)", (page_id, raw_text))
                except Exception as e:
                    logger.warning(f"FTS index failed: {e}")
            conn.commit()
            return page_id

    def get_document_pages(self, doc_id: str) -> list[dict]:
        with self.get_connection() as conn:
            return [_page_from_row(r) for r in conn.execute(
                "SELECT * FROM doc_pages WHERE doc_id = ? ORDER BY page_num", (doc_id,)
            ).fetchall()]

    def count_document_pages(self, doc_id: str) -> int:
        """Count total pages for a document"""
        with self.get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM doc_pages WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            return row[0] if row else 0

    def get_document_pages_batch(self, doc_id: str, offset: int = 0, limit: int = 100) -> list[dict]:
        """Get pages in batches (streaming) to avoid loading all pages into memory"""
        with self.get_connection() as conn:
            return [_page_from_row(r) for r in conn.execute(
                "SELECT * FROM doc_pages WHERE doc_id = ? ORDER BY page_num LIMIT ? OFFSET ?",
                (doc_id, limit, offset)
            ).fetchall()]

    def get_page(self, page_id: int) -> dict | None:
        with self.get_connection() as conn:
            row = conn.execute("SELECT * FROM doc_pages WHERE id = ?", (page_id,)).fetchone()
            return _page_from_row(row) if row else None

    # === Chunk Operations ===
    def save_chunk(self, doc_id: str, page_id: int, page_num: int, chunk_idx: int,
                   section_path: str, section_title: str, chunk_text: str) -> int:
        """Save chunk to doc_chunks table and doc_chunks_fts virtual table"""
        preview = chunk_text[:200] if chunk_text else ""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO doc_chunks (doc_id, page_id, page_num, chunk_idx, section_path, section_title, chunk_text, chunk_text_preview)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (doc_id, page_id, page_num, chunk_idx, section_path, section_title, chunk_text, preview))
            chunk_db_id = cursor.lastrowid
            if chunk_text:
                try:
                    cursor.execute("INSERT INTO doc_chunks_fts(rowid, chunk_text) VALUES (?, ?)", (chunk_db_id, chunk_text))
                except Exception as e:
                    logger.warning(f"Chunk FTS index failed: {e}")
            conn.commit()
            return chunk_db_id

    def get_document_chunks(self, doc_id: str, page_id: int = None) -> list[dict]:
        """Get document chunks"""
        with self.get_connection() as conn:
            if page_id:
                rows = conn.execute(
                    "SELECT * FROM doc_chunks WHERE doc_id = ? AND page_id = ? ORDER BY page_num, chunk_idx",
                    (doc_id, page_id)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM doc_chunks WHERE doc_id = ? ORDER BY page_num, chunk_idx",
                    (doc_id,)
                ).fetchall()
            return [dict(r) for r in rows]

    def search_fts_chunks(self, query: str, limit: int = 20, force_bigram_only: bool = False, page_filter: set[int] | None = None) -> list[dict]:
        """Chunk-level FTS search (OpenLAD: downgraded from page-level to chunk-level)

        FIX: When force_bigram_only=True, skip FTS trigram search and go directly to bigram LIKE search.
        This handles queries consisting entirely of 2-character Chinese words, since the trigram tokenizer cannot index 2-character words.

        Phase 2 FIX: Added page_filter parameter to restrict search to specific page numbers.
        When page_filter is provided, only chunks from those pages are returned.
        This enables "L2 structure index → FTS within chapter range" workflow.
        """
        import re
        clean_query = re.sub(rf'[^\w\s{CJK_START}-{CJK_END}]', ' ', query)
        clean_query = re.sub(r'\s+', ' ', clean_query).strip()
        if not clean_query:
            return []
        cjk_range = f"{CJK_START}-{CJK_END}"
        clean_query = re.sub(rf'([{cjk_range}])([A-Za-z0-9])', r'\1 \2', clean_query)
        clean_query = re.sub(rf'([A-Za-z0-9])([{cjk_range}])', r'\1 \2', clean_query)
        clean_query = re.sub(r'\s+', ' ', clean_query).strip()

        tokens = clean_query.split()

        expanded_tokens = []
        for t in tokens:
            if len(t) >= 4 and all(CJK_START <= c <= CJK_END for c in t):
                # Split Chinese strings of 4+ characters into 2-character bigrams
                # (overlapping: ABCD -> AB, BC, CD) for LIKE fallback search
                for i in range(len(t) - 1):
                    expanded_tokens.append(t[i:i+2])
            elif len(t) == 3 and all(CJK_START <= c <= CJK_END for c in t):
                # 3-character words: split into first 2 chars and last 2 chars (overlapping)
                expanded_tokens.append(t[0:2])
                expanded_tokens.append(t[1:3])
            else:
                expanded_tokens.append(t)
        tokens = expanded_tokens

        trigram_tokens = [t for t in tokens if len(t) >= 3 and not re.match(r'^\d+$', t)]
        # OpenLAD FIX: All 2-character words cannot be indexed by the trigram tokenizer; all need LIKE fallback
        bigram_tokens = [t for t in tokens if len(t) == 2]

        all_results = []
        seen_chunk_ids = set()

        # Phase 2: Build page filter SQL clause
        page_filter_sql = ""
        page_filter_params = []
        if page_filter:
            page_filter_sql = f"AND dc.page_num IN ({','.join(['?'] * len(page_filter))})"
            page_filter_params = list(page_filter)
            logger.info(f"[FTS] Applying page_filter: {sorted(page_filter)[:20]}{'...' if len(page_filter) > 20 else ''} ({len(page_filter)} pages)")

        # 1. FTS5 trigram search (3+ characters)
        # FIX: When force_bigram_only=True, skip FTS search and go directly to bigram LIKE
        if trigram_tokens and not force_bigram_only:
            # Try AND search first
            if len(trigram_tokens) >= 2:
                and_query = ' AND '.join(trigram_tokens)
                try:
                    with self.get_connection() as conn:
                        rows = conn.execute(f"""
                            SELECT dc.id, dc.doc_id, dc.page_id, dc.page_num, dc.section_title,
                                   dc.chunk_text, dc.chunk_text_preview, rank
                            FROM doc_chunks_fts JOIN doc_chunks dc ON doc_chunks_fts.rowid = dc.id
                            WHERE doc_chunks_fts MATCH ? {page_filter_sql}
                            ORDER BY rank LIMIT ?
                        """, (and_query,) + tuple(page_filter_params) + (limit,)).fetchall()
                        for r in rows:
                            cid = r["id"]
                            if cid not in seen_chunk_ids:
                                seen_chunk_ids.add(cid)
                                all_results.append({
                                    "chunk_id": cid, "doc_id": r["doc_id"], "page_id": r["page_id"],
                                    "page_num": r["page_num"], "section_title": r["section_title"],
                                    "chunk_text": r["chunk_text"], "chunk_text_preview": r["chunk_text_preview"],
                                    "score": -r["rank"] if r["rank"] < 0 else r["rank"]
                                })
                except Exception as e:
                    logger.warning(f"Chunk FTS AND search error: {e}")

            # If AND results are less than 30% of limit, supplement with OR
            if len(all_results) < int(max(limit * 0.3, 3)):
                match_query = ' OR '.join(trigram_tokens)
                try:
                    with self.get_connection() as conn:
                        rows = conn.execute(f"""
                            SELECT dc.id, dc.doc_id, dc.page_id, dc.page_num, dc.section_title,
                                   dc.chunk_text, dc.chunk_text_preview, rank
                            FROM doc_chunks_fts JOIN doc_chunks dc ON doc_chunks_fts.rowid = dc.id
                            WHERE doc_chunks_fts MATCH ? {page_filter_sql}
                            ORDER BY rank LIMIT ?
                        """, (match_query,) + tuple(page_filter_params) + (limit * 2,)).fetchall()
                        for r in rows:
                            cid = r["id"]
                            if cid not in seen_chunk_ids:
                                seen_chunk_ids.add(cid)
                                all_results.append({
                                    "chunk_id": cid, "doc_id": r["doc_id"], "page_id": r["page_id"],
                                    "page_num": r["page_num"], "section_title": r["section_title"],
                                    "chunk_text": r["chunk_text"], "chunk_text_preview": r["chunk_text_preview"],
                                    "score": -r["rank"] if r["rank"] < 0 else r["rank"]
                                })
                except Exception as e:
                    logger.warning(f"Chunk FTS OR search error: {e}")

        # Deduplicate and sort by score
        all_results.sort(key=lambda x: x["score"], reverse=True)

        # 2. Supplementary recall: search 2-character Chinese words with LIKE
        # FIX: Sort by number of matched keywords (more matches rank higher), not simply by page_num
        if bigram_tokens:
            like_limit = max(limit * 2, 40)
            try:
                with self.get_connection() as conn:
                    conditions = " OR ".join(["dc.chunk_text LIKE ?" for _ in bigram_tokens])
                    params = [f"%{t}%" for t in bigram_tokens]
                    # Calculate match quality: more matched keywords → higher ranking
                    match_score = " + ".join(["CASE WHEN dc.chunk_text LIKE ? THEN 1 ELSE 0 END" for _ in bigram_tokens])
                    if seen_chunk_ids:
                        exclude_sql = f"AND dc.id NOT IN ({','.join(['?'] * len(seen_chunk_ids))})"
                        sql = f"""
                            SELECT dc.id, dc.doc_id, dc.page_id, dc.page_num, dc.section_title,
                                   dc.chunk_text, dc.chunk_text_preview, ({match_score}) as match_count
                            FROM doc_chunks dc
                            WHERE ({conditions}) {exclude_sql} {page_filter_sql}
                            ORDER BY match_count DESC, dc.page_num
                            LIMIT ?
                        """
                        query_params = params + params + list(seen_chunk_ids) + page_filter_params + [like_limit]
                    else:
                        sql = f"""
                            SELECT dc.id, dc.doc_id, dc.page_id, dc.page_num, dc.section_title,
                                   dc.chunk_text, dc.chunk_text_preview, ({match_score}) as match_count
                            FROM doc_chunks dc
                            WHERE ({conditions}) {page_filter_sql}
                            ORDER BY match_count DESC, dc.page_num
                            LIMIT ?
                        """
                        query_params = params + params + page_filter_params + [like_limit]
                    rows = conn.execute(sql, query_params).fetchall()
                    for r in rows:
                        cid = r["id"]
                        if cid not in seen_chunk_ids:
                            seen_chunk_ids.add(cid)
                            match_count = r["match_count"]
                            # Matching multiple keywords gets a higher base score
                            base_score = 0.1 + min(match_count * 0.05, 0.3)
                            all_results.append({
                                "chunk_id": cid, "doc_id": r["doc_id"], "page_id": r["page_id"],
                                "page_num": r["page_num"], "section_title": r["section_title"],
                                "chunk_text": r["chunk_text"], "chunk_text_preview": r["chunk_text_preview"],
                                "score": base_score
                            })
            except Exception as e:
                logger.warning(f"Chunk bigram LIKE search error: {e}")

        return all_results[:limit]

    # === Structure Index ===
    def save_structure_index(self, doc_id: str, section_path: str, section_title: str,
                             section_level: int = 0, start_page: int = None,
                             end_page: int = None, section_type: str = "section",
                             parent_path: str = None, keywords: str = None,
                             summary: str = None, entities: str = None) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM doc_structure_index WHERE doc_id = ? AND section_path = ?", (doc_id, section_path))
            cursor.execute("""
                INSERT INTO doc_structure_index
                (doc_id, section_path, section_title, section_level, start_page, end_page,
                 section_type, parent_path, keywords, summary, entities)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (doc_id, section_path, section_title, section_level, start_page, end_page,
                  section_type, parent_path, keywords, summary, entities))
            conn.commit()
            return cursor.lastrowid

    def get_structure_index(self, doc_id: str) -> list[dict]:
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM doc_structure_index WHERE doc_id = ? ORDER BY section_path", (doc_id,)
            ).fetchall()]

    def search_structure_index(self, doc_id: str, keyword: str) -> list[dict]:
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute("""
                SELECT * FROM doc_structure_index
                WHERE doc_id = ? AND (section_title LIKE ? OR keywords LIKE ? OR summary LIKE ? OR entities LIKE ?)
                ORDER BY section_level, start_page
            """, (doc_id, f"%{keyword}%", f"%{keyword}%", f"%{keyword}%", f"%{keyword}%")).fetchall()]

    # === Spec Facts (assertion-level index) ===

    def insert_spec_fact(self, doc_id: str, entity: str, attribute: str, value: str,
                         page_num: int, source_text: str, unit: str = "",
                         extractor: str = "rule", verified: int = 1):
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO spec_facts (doc_id, entity, attribute, value, unit,
                                        page_num, source_text, extractor, verified)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (doc_id, entity, attribute, value, unit, page_num, source_text, extractor, verified))
            conn.commit()

    def clear_spec_facts(self, doc_id: str | None = None):
        """Clear spec facts (all docs or one doc) before re-extraction."""
        with self.get_connection() as conn:
            if doc_id:
                conn.execute("DELETE FROM spec_facts WHERE doc_id = ?", (doc_id,))
            else:
                conn.execute("DELETE FROM spec_facts")
            conn.commit()

    def search_spec_facts(self, keywords: list[str], doc_id_filter: set | None = None,
                          limit: int = 20, verified_only: bool = True) -> list[dict]:
        """Match spec facts by keywords against entity/attribute/value/source_text.

        Any keyword hit scores; more hits rank higher. Only verified facts by
        default (values confirmed to appear in the original page text).
        """
        if not keywords:
            return []
        where, params = [], []
        if verified_only:
            where.append("verified = 1")
        if doc_id_filter:
            where.append(f"doc_id IN ({','.join('?' * len(doc_id_filter))})")
            params.extend(doc_id_filter)
        sql_where = ("WHERE " + " AND ".join(where)) if where else ""
        with self.get_connection() as conn:
            rows = [dict(r) for r in conn.execute(
                f"SELECT * FROM spec_facts {sql_where} LIMIT 2000", params).fetchall()]
        scored = []
        for r in rows:
            hay = f"{r.get('entity','')} {r.get('attribute','')} {r.get('value','')} {r.get('source_text','')}".lower()
            hits = sum(1 for kw in keywords if kw and kw.lower() in hay)
            if hits:
                scored.append((hits, r))
        scored.sort(key=lambda x: (-x[0], x[1].get("page_num") or 0))
        return [r for _, r in scored[:limit]]

    def get_spec_fact_entities(self) -> list[str]:
        """Distinct entity vocabulary of the assertion index (verified facts)."""
        try:
            with self.get_connection() as conn:
                return [r[0] for r in conn.execute(
                    "SELECT DISTINCT entity FROM spec_facts WHERE verified = 1 AND entity != ''"
                ).fetchall()]
        except Exception:
            return []

    def find_pages_containing(self, doc_id: str, keyword: str, limit: int = 21) -> list[int]:
        """Return page numbers whose raw_text contains the keyword verbatim.

        Used by the retriever's rare-token rescue: exact identifiers that are rare
        in the structure index must not be excluded by chapter page filters.
        Caller passes limit = max_pages + 1 to detect "too common to discriminate".
        """
        with self.get_connection() as conn:
            return [r[0] for r in conn.execute(
                "SELECT DISTINCT page_num FROM doc_pages WHERE doc_id = ? AND raw_text LIKE ? LIMIT ?",
                (doc_id, f"%{keyword}%", limit)).fetchall()]

    # === FTS Search ===
    def search_fts(self, query: str, limit: int = 20) -> list[dict]:
        import re
        clean_query = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', query)
        clean_query = re.sub(r'\s+', ' ', clean_query).strip()
        if not clean_query:
            return []
        # FIX: Insert spaces between Chinese characters and English/digits to ensure correct tokenization
        # e.g. "compare RK3562" → "compare RK3562"
        clean_query = re.sub(r'([\u4e00-\u9fff])([A-Za-z0-9])', r'\1 \2', clean_query)
        clean_query = re.sub(r'([A-Za-z0-9])([\u4e00-\u9fff])', r'\1 \2', clean_query)
        clean_query = re.sub(r'\s+', ' ', clean_query).strip()

        tokens = clean_query.split()

        # Split long Chinese strings into 2-character bigrams for LIKE fallback
        expanded_tokens = []
        for t in tokens:
            if len(t) > 4 and all(CJK_START <= c <= CJK_END for c in t):
                # No function words: split by 2-character bigrams
                for i in range(0, len(t) - 1, 2):
                    expanded_tokens.append(t[i:i+2])
                if len(t) % 2 == 1:
                    expanded_tokens.append(t[-2:])
            else:
                expanded_tokens.append(t)
        tokens = expanded_tokens

        trigram_tokens = [t for t in tokens if len(t) >= 3 and not re.match(r'^\d+$', t)]
        # OpenLAD FIX: All 2-character words cannot be indexed by the trigram tokenizer; all need LIKE fallback
        bigram_tokens = [t for t in tokens if len(t) == 2]

        all_results = []
        seen_page_ids = set()

        # 1. FTS5 trigram search (3+ characters)
        # OpenLAD FIX: Try AND logic first (reduces noise), then supplement with OR if insufficient
        if trigram_tokens:
            # Try AND search first
            if len(trigram_tokens) >= 2:
                and_query = ' AND '.join(trigram_tokens)
                try:
                    with self.get_connection() as conn:
                        rows = conn.execute("""
                            SELECT dp.id, dp.doc_id, dp.page_num, dp.section_title, dp.raw_text, rank
                            FROM doc_pages_fts JOIN doc_pages dp ON doc_pages_fts.rowid = dp.id
                            WHERE doc_pages_fts MATCH ? ORDER BY rank LIMIT ?
                        """, (and_query, limit)).fetchall()
                        for r in rows:
                            pid = r["id"]
                            if pid not in seen_page_ids:
                                seen_page_ids.add(pid)
                                all_results.append({
                                    "page_id": pid, "doc_id": r["doc_id"],
                                    "page_num": r["page_num"], "section_title": r["section_title"],
                                    "raw_text": r["raw_text"], "score": -r["rank"] if r["rank"] < 0 else r["rank"]
                                })
                except Exception as e:
                    logger.warning(f"FTS AND search error: {e}")

            # If AND results are less than 30% of limit, supplement with OR
            if len(all_results) < int(max(limit * 0.3, 3)):
                match_query = ' OR '.join(trigram_tokens)
                try:
                    with self.get_connection() as conn:
                        rows = conn.execute("""
                            SELECT dp.id, dp.doc_id, dp.page_num, dp.section_title, dp.raw_text, rank
                            FROM doc_pages_fts JOIN doc_pages dp ON doc_pages_fts.rowid = dp.id
                            WHERE doc_pages_fts MATCH ? ORDER BY rank LIMIT ?
                        """, (match_query, limit * 2)).fetchall()
                        for r in rows:
                            pid = r["id"]
                            if pid not in seen_page_ids:
                                seen_page_ids.add(pid)
                                all_results.append({
                                    "page_id": pid, "doc_id": r["doc_id"],
                                    "page_num": r["page_num"], "section_title": r["section_title"],
                                    "raw_text": r["raw_text"], "score": -r["rank"] if r["rank"] < 0 else r["rank"]
                                })
                except Exception as e:
                    logger.warning(f"FTS OR search error: {e}")

        # Deduplicate and sort by score (AND results typically have higher scores, ranked first)
        all_results.sort(key=lambda x: x["score"], reverse=True)

        # 2. Supplementary recall: search 2-character Chinese words with LIKE (FTS5 trigram tokenizer doesn't index 2-character words)
        # OpenLAD FIX: Sort by number of matched keywords; more matches rank higher
        if bigram_tokens:
            like_limit = max(limit * 2, 40)
            try:
                with self.get_connection() as conn:
                    conditions = " OR ".join(["dp.raw_text LIKE ?" for _ in bigram_tokens])
                    params = [f"%{t}%" for t in bigram_tokens]
                    match_score = " + ".join(["CASE WHEN dp.raw_text LIKE ? THEN 1 ELSE 0 END" for _ in bigram_tokens])
                    if seen_page_ids:
                        exclude_sql = f"AND dp.id NOT IN ({','.join(['?'] * len(seen_page_ids))})"
                        sql = f"""
                            SELECT dp.id, dp.doc_id, dp.page_num, dp.section_title, dp.raw_text, ({match_score}) as match_count
                            FROM doc_pages dp
                            WHERE ({conditions}) {exclude_sql}
                            ORDER BY match_count DESC, dp.page_num
                            LIMIT ?
                        """
                        query_params = params + params + list(seen_page_ids) + [like_limit]
                    else:
                        sql = f"""
                            SELECT dp.id, dp.doc_id, dp.page_num, dp.section_title, dp.raw_text, ({match_score}) as match_count
                            FROM doc_pages dp
                            WHERE ({conditions})
                            ORDER BY match_count DESC, dp.page_num
                            LIMIT ?
                        """
                        query_params = params + params + [like_limit]
                    rows = conn.execute(sql, query_params).fetchall()
                    for r in rows:
                        pid = r["id"]
                        if pid not in seen_page_ids:
                            seen_page_ids.add(pid)
                            match_count = r["match_count"]
                            base_score = 0.1 + min(match_count * 0.05, 0.3)
                            all_results.append({
                                "page_id": pid, "doc_id": r["doc_id"],
                                "page_num": r["page_num"], "section_title": r["section_title"],
                                "raw_text": r["raw_text"], "score": base_score
                            })
            except Exception as e:
                logger.warning(f"Bigram LIKE search error: {e}")

        return all_results[:limit]

    # === Chat ===
    def create_chat_session(self, session_id: str, user_id: str = None,
                            title: str = "New Chat", industry: str = "auto") -> str:
        with self.get_connection() as conn:
            conn.execute("INSERT INTO chat_sessions (id, user_id, title, industry) VALUES (?, ?, ?, ?)",
                         (session_id, user_id, title, industry))
            conn.commit()
        return session_id

    def get_chat_sessions(self, user_id: str = None, limit: int = 100) -> list[dict]:
        query = """
            SELECT s.id, s.title, s.industry, s.created_at, s.updated_at, COUNT(m.id) as message_count
            FROM chat_sessions s LEFT JOIN chat_messages m ON s.id = m.session_id
        """
        params = []
        if user_id:
            query += " WHERE s.user_id = ?"
            params.append(user_id)
        query += " GROUP BY s.id ORDER BY s.updated_at DESC LIMIT ?"
        params.append(limit)
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute(query, params).fetchall()]

    def save_chat_message(self, session_id: str, role: str, content: str,
                          sources: str = None, query_info: str = None) -> int:
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO chat_messages (session_id, role, content, sources, query_info)
                VALUES (?, ?, ?, ?, ?)
            """, (session_id, role, content, sources, query_info))
            conn.execute("UPDATE chat_sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?", (session_id,))
            conn.commit()
            return cursor.lastrowid

    def get_chat_messages(self, session_id: str) -> list[dict]:
        with self.get_connection() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY created_at",
                (session_id,)
            ).fetchall()]

    # === Query Log ===
    def log_query(self, query: str, user_id: str = None, intent: str = None,
                  industry_package_id: str = None, elapsed_ms: int = 0,
                  results_count: int = 0, answer_length: int = 0, trace: dict = None) -> int:
        import hashlib
        query_hash = hashlib.md5(query.encode()).hexdigest()
        trace_json = json.dumps(trace) if trace else None
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO query_log (user_id, query, query_hash, intent, industry_package_id,
                                       elapsed_ms, results_count, answer_length, trace_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (user_id, query, query_hash, intent, industry_package_id,
                  elapsed_ms, results_count, answer_length, trace_json))
            conn.commit()
            return cursor.lastrowid

    def log_audit(self, action: str, resource_type: str = None, resource_id: str = None,
                  user_id: str = None, tenant_id: str = None,
                  details: dict = None, ip_address: str = None) -> int:
        """Record audit log"""
        details_json = json.dumps(details, ensure_ascii=False) if details else None
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO audit_log (action, resource_type, resource_id, user_id, tenant_id, details, ip_address)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (action, resource_type, resource_id, user_id, tenant_id, details_json, ip_address))
            conn.commit()
            return cursor.lastrowid

    # -------------------------------------------------------------------------
    # Upload Task Status (V4.7: replaces in-memory _upload_tasks)
    # -------------------------------------------------------------------------
    def create_upload_task(self, task_id: str, doc_id: str, filename: str, tenant_id: str = "") -> str:
        """Create upload task record in DB"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO upload_tasks (task_id, doc_id, tenant_id, filename, status, progress, message)
                VALUES (?, ?, ?, ?, 'pending', 0, 'Waiting for processing')
            """, (task_id, doc_id, tenant_id, filename))
            conn.commit()
            return task_id

    def update_upload_task(self, task_id: str, **kwargs) -> bool:
        """Update upload task status in DB. Only updates every 10% progress or status change to reduce writes."""
        # Build dynamic update
        allowed_fields = {'status', 'progress', 'message', 'result', 'error'}
        updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
        if not updates:
            return False

        # Add updated_at
        updates['updated_at'] = 'CURRENT_TIMESTAMP'

        set_clause = ', '.join(f"{k} = ?" for k in updates.keys())
        values = list(updates.values())
        values.append(task_id)

        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                UPDATE upload_tasks SET {set_clause} WHERE task_id = ?
            """, values)
            conn.commit()
            return cursor.rowcount > 0

    def get_upload_task(self, task_id: str) -> dict | None:
        """Get upload task by ID"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM upload_tasks WHERE task_id = ?
            """, (task_id,))
            row = cursor.fetchone()
            if row:
                return dict(row)
            return None

    def cleanup_upload_tasks(self, max_age_hours: int = 24) -> int:
        """Clean up old completed/failed tasks"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                DELETE FROM upload_tasks
                WHERE updated_at < datetime('now', '-{max_age_hours} hours')
                AND status IN ('completed', 'failed', 'already_imported')
            """)
            conn.commit()
            return cursor.rowcount

    def restore_interrupted_tasks(self, tenant_id: str = None) -> list[dict]:
        """On API startup, find tasks that were 'processing' when API last crashed"""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            if tenant_id:
                cursor.execute("""
                    SELECT * FROM upload_tasks
                    WHERE status = 'processing' AND tenant_id = ?
                """, (tenant_id,))
            else:
                cursor.execute("""
                    SELECT * FROM upload_tasks WHERE status = 'processing'
                """)
            rows = cursor.fetchall()
            return [dict(row) for row in rows]


# =============================================================================
# Tenant-level Vector/Full-text Database
# =============================================================================

class TenantVectorDB:
    """Tenant-level sqlite-vec vector search library
    OpenLAD FIX: Changed from page-level to chunk-level embedding; semantic embedding applied after splitting original text into chunks
    """

    def __init__(self, vec_db_path: Path):
        self.vec_db_path = vec_db_path
        self._init_vec_db()

    def _init_vec_db(self):
        try:
            conn = sqlite3.connect(self.vec_db_path)
            conn.enable_load_extension(True)
            try:
                import sqlite_vec
                sqlite_vec.load(conn)
            except Exception as e:
                logger.warning(f"sqlite-vec extension not available: {e}")
            cursor = conn.cursor()
            for table_sql in [
                # Legacy tables (existing data untouched)
                "CREATE TABLE IF NOT EXISTS l2_pages (page_id INTEGER PRIMARY KEY, doc_id TEXT, embedding BLOB)",
                "CREATE TABLE IF NOT EXISTS l2_formulas (formula_id TEXT PRIMARY KEY, page_id INTEGER, doc_id TEXT, embedding BLOB)",
                # New chunk-level vector table
                """CREATE TABLE IF NOT EXISTS l2_chunks (
                    page_id INTEGER NOT NULL,
                    chunk_idx INTEGER NOT NULL DEFAULT 0,
                    doc_id TEXT NOT NULL,
                    embedding BLOB NOT NULL,
                    chunk_text_preview TEXT,
                    chunk_text TEXT,
                    PRIMARY KEY (page_id, chunk_idx)
                )""",
            ]:
                cursor.execute(table_sql)
            # Migration: older dbs created before chunk_text column existed.
            try:
                cols = [r[1] for r in cursor.execute("PRAGMA table_info(l2_chunks)").fetchall()]
                if "chunk_text" not in cols:
                    cursor.execute("ALTER TABLE l2_chunks ADD COLUMN chunk_text TEXT")
            except Exception as mig_e:
                logger.warning(f"l2_chunks chunk_text migration failed (non-critical): {mig_e}")
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"sqlite-vec init failed: {e}")

    # --- Legacy interface: page-level (kept for compatibility) ---
    def store_l2_embedding(self, page_id: int, doc_id: str, embedding: list[float]):
        try:
            conn = sqlite3.connect(self.vec_db_path)
            import struct
            emb_bytes = struct.pack(f'{len(embedding)}f', *embedding)
            conn.execute("INSERT OR REPLACE INTO l2_pages (page_id, doc_id, embedding) VALUES (?, ?, ?)",
                         (page_id, doc_id, emb_bytes))
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"L2 embedding failed: {e}")

    def search_l2(self, query_embedding: list[float], limit: int = 20,
                  doc_id_filter: set[str] = None, min_score: float = 0.40) -> list[dict]:
        """Legacy page-level search (compatibility)"""
        try:
            conn = sqlite3.connect(self.vec_db_path)
            conn.enable_load_extension(True)
            try:
                import sqlite_vec
                sqlite_vec.load(conn)
            except Exception:
                conn.close()
                return []
            import struct
            emb_bytes = struct.pack(f'{len(query_embedding)}f', *query_embedding)
            max_distance = 1.0 - min_score
            if doc_id_filter is not None and len(doc_id_filter) > 0 and "__ALL__" not in doc_id_filter:
                ph = ",".join("?" * len(doc_id_filter))
                results = conn.execute(f"""
                    SELECT page_id, doc_id, vec_distance_cosine(embedding, ?) as distance
                    FROM l2_pages WHERE doc_id IN ({ph}) AND distance < ? ORDER BY distance LIMIT ?
                """, (emb_bytes,) + tuple(doc_id_filter) + (max_distance, limit)).fetchall()
            else:
                results = conn.execute("""
                    SELECT page_id, doc_id, vec_distance_cosine(embedding, ?) as distance
                    FROM l2_pages WHERE distance < ? ORDER BY distance LIMIT ?
                """, (emb_bytes, max_distance, limit)).fetchall()
            conn.close()
            return [{"page_id": r[0], "doc_id": r[1], "score": 1.0 - r[2]} for r in results]
        except Exception as e:
            logger.error(f"L2 search failed: {e}")
            return []

    # --- New interface: chunk-level ---
    def store_l2_chunk(self, page_id: int, chunk_idx: int, doc_id: str,
                       embedding: list[float], chunk_text_preview: str = "",
                       chunk_text: str = ""):
        """Store chunk-level embedding"""
        try:
            conn = sqlite3.connect(self.vec_db_path)
            import struct
            emb_bytes = struct.pack(f'{len(embedding)}f', *embedding)
            conn.execute(
                """INSERT OR REPLACE INTO l2_chunks
                   (page_id, chunk_idx, doc_id, embedding, chunk_text_preview, chunk_text)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (page_id, chunk_idx, doc_id, emb_bytes, chunk_text_preview[:200], chunk_text)
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.error(f"L2 chunk store failed: {e}")

    def search_l2_chunks(self, query_embedding: list[float], limit: int = 20,
                         doc_id_filter: set[str] = None, min_score: float = 0.35) -> list[dict]:
        """Chunk-level semantic search, returns page_id-level aggregated results (multiple chunks in the same page take the highest score)"""
        try:
            conn = sqlite3.connect(self.vec_db_path)
            conn.enable_load_extension(True)
            try:
                import sqlite_vec
                sqlite_vec.load(conn)
            except Exception:
                conn.close()
                return []
            import struct
            emb_bytes = struct.pack(f'{len(query_embedding)}f', *query_embedding)
            max_distance = 1.0 - min_score

            if doc_id_filter is not None and len(doc_id_filter) > 0 and "__ALL__" not in doc_id_filter:
                ph = ",".join("?" * len(doc_id_filter))
                results = conn.execute(f"""
                    SELECT page_id, doc_id, vec_distance_cosine(embedding, ?) as distance
                    FROM l2_chunks WHERE doc_id IN ({ph}) AND distance < ?
                    ORDER BY distance LIMIT ?
                """, (emb_bytes,) + tuple(doc_id_filter) + (max_distance, limit * 3)).fetchall()
            else:
                results = conn.execute("""
                    SELECT page_id, doc_id, vec_distance_cosine(embedding, ?) as distance
                    FROM l2_chunks WHERE distance < ?
                    ORDER BY distance LIMIT ?
                """, (emb_bytes, max_distance, limit * 3)).fetchall()
            conn.close()

            # Multiple chunks in the same page take the highest similarity
            page_best = {}
            for r in results:
                pid, did, dist = r[0], r[1], r[2]
                score = 1.0 - dist
                key = (pid, did)
                if key not in page_best or score > page_best[key]["score"]:
                    page_best[key] = {"page_id": pid, "doc_id": did, "score": score}

            # Sort by score, take top limit
            aggregated = sorted(page_best.values(), key=lambda x: -x["score"])[:limit]
            return aggregated

        except Exception as e:
            logger.error(f"L2 chunk search failed: {e}")
            return []

    def delete_doc_vectors(self, doc_id: str) -> bool:
        try:
            conn = sqlite3.connect(self.vec_db_path)
            for table in ["l2_pages", "l2_formulas", "l2_chunks"]:
                conn.execute(f"DELETE FROM {table} WHERE doc_id = ?", (doc_id,))
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            logger.error(f"Delete doc vectors failed: {e}")
            return False


# =============================================================================
# Tenant Database Factory
# =============================================================================

class TenantDBFactory:
    """Tenant Database Factory

    Creates and manages independent database instances per tenant.
    Uses singleton pattern with caching; one set of instances per tenant.
    """

    _instances: dict[str, tuple] = {}  # tenant_id -> (metadata_db, vector_db)
    _lock = threading.Lock()

    @classmethod
    def init_tenant_databases(cls, tenant_id: str):
        """Initialize tenant databases (called on first creation)"""
        db_path = settings.get_tenant_db_path(tenant_id)
        vec_path = settings.get_tenant_vec_db_path(tenant_id)

        # Ensure directory exists
        db_path.parent.mkdir(parents=True, exist_ok=True)

        # Initialization auto-creates schema
        TenantMetadataDB(db_path)
        TenantVectorDB(vec_path)
        logger.info(f"[TenantDBFactory] Initialized tenant database: {tenant_id}")

    @classmethod
    def get_databases(cls, tenant_id: str) -> tuple:
        """Get tenant database instances (cached)"""
        if tenant_id not in cls._instances:
            with cls._lock:
                if tenant_id not in cls._instances:
                    db_path = settings.get_tenant_db_path(tenant_id)
                    vec_path = settings.get_tenant_vec_db_path(tenant_id)
                    # If DB file doesn't exist, initialize first
                    if not db_path.exists():
                        cls.init_tenant_databases(tenant_id)
                    metadata_db = TenantMetadataDB(db_path)
                    vector_db = TenantVectorDB(vec_path)
                    cls._instances[tenant_id] = (metadata_db, vector_db)
        return cls._instances[tenant_id]

    @classmethod
    def clear_cache(cls, tenant_id: str = None):
        """Clear cache"""
        with cls._lock:
            if tenant_id:
                cls._instances.pop(tenant_id, None)
            else:
                cls._instances.clear()


# Convenience functions

def get_tenant_metadata_db(tenant_id: str) -> TenantMetadataDB:
    metadata_db, _ = TenantDBFactory.get_databases(tenant_id)
    return metadata_db


def get_tenant_vector_db(tenant_id: str) -> TenantVectorDB:
    _, vector_db = TenantDBFactory.get_databases(tenant_id)
    return vector_db


# =============================================================================
# Helper functions
# =============================================================================

def _doc_from_row(row) -> dict:
    doc = dict(row)
    if doc.get("topic_tags"):
        doc["topic_tags"] = doc["topic_tags"].split(",")
    if doc.get("metadata_json"):
        doc["metadata"] = json.loads(doc["metadata_json"])
    return doc


def _page_from_row(row) -> dict | None:
    if not row:
        return None
    page = dict(row)
    if page.get("entities"):
        page["entities"] = json.loads(page["entities"])
    if page.get("content_json"):
        page["content"] = json.loads(page["content_json"])
    if page.get("schematic_data"):
        page["schematic_data"] = json.loads(page["schematic_data"])
    if page.get("extra_data"):
        page["extra_data"] = json.loads(page["extra_data"])
    page.setdefault("section_level", 0)
    page.setdefault("section_path", "")
    page.setdefault("is_table_header", False)
    page.setdefault("table_caption", "")
    return page
