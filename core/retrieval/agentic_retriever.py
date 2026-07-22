"""
Agentic Retriever - Integrated into the core retrieval engine
Intelligent retrieval based on document chapter structure, no hardcoded keywords
"""
import json
import logging
import re
import sqlite3
import struct
import numpy as np
from typing import Dict, Any, List, Optional

from ..config import settings
from ..models.client import get_model_client
from ..db.tenant_db import get_tenant_metadata_db

logger = logging.getLogger(__name__)


class AgenticRetriever:
    """
    Agentic Retriever - Integrated into the OpenLAD core engine
    """
    
    def __init__(self, tenant_id: str, config_path: str = None):
        self.tenant_id = tenant_id
        self.model_client = get_model_client()
        self.metadata_db = get_tenant_metadata_db(tenant_id)
        
        # Load configuration
        self.config = self._load_config(config_path)
        
        # Load vector index
        self.vec_index = self._load_vec_index()
        
        # Build catalog
        self.catalog = self._build_catalog()
    
    def _load_config(self, config_path: str = None) -> dict:
        """Load configuration file, no defaults"""
        import json
        import os
        
        if config_path and os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return json.load(f)
        
        # Default config path (relative to project root)
        default_path = str(settings.BASE_DIR / 'config' / 'agentic_search.json')
        if os.path.exists(default_path):
            with open(default_path, 'r') as f:
                return json.load(f)
        
        return {}
    
    def _load_vec_index(self) -> list:
        """Load vector database into memory"""
        vec_db_path = str(settings.get_tenant_vec_db_path(self.tenant_id))
        conn = sqlite3.connect(vec_db_path)
        
        chunks = []
        cur = conn.execute("SELECT page_id, chunk_idx, doc_id, embedding, chunk_text_preview, chunk_text FROM l2_chunks")
        for r in cur.fetchall():
            page_id, chunk_idx, doc_id, emb_bytes, preview, chunk_text = r
            emb = struct.unpack(f'{len(emb_bytes)//4}f', emb_bytes)
            chunks.append({
                "page_id": page_id,
                "chunk_idx": chunk_idx,
                "doc_id": doc_id,
                "embedding": np.array(emb),
                "preview": preview,
                "chunk_text": chunk_text or ""
            })
        
        conn.close()
        return chunks
    
    def _build_catalog(self) -> dict:
        """Build a compact collection catalog"""
        catalog = {"documents": []}
        
        with self.metadata_db.get_connection() as conn:
            docs = conn.execute("SELECT id, title FROM documents").fetchall()
            
            for doc in docs:
                doc_info = {
                    "doc_id": doc["id"],
                    "title": doc["title"],
                    "chapters": []
                }
                
                # Fetch all chapters, but limit count to avoid exceeding context
                # Prioritize first 10 pages (typically Features/Overview) and chapters with key terms
                chapter_limit = settings.AGENTIC_CONFIG.get('catalog_chapter_limit', 40)
                chapters = conn.execute(
                    "SELECT section_title, start_page, end_page "
                    "FROM doc_structure_index WHERE doc_id = ? "
                    "ORDER BY start_page LIMIT ?",
                    (doc["id"], chapter_limit)
                ).fetchall()
                
                # Prioritize key chapters (generic terms; industry-specific terms extended by industry packages)
                # FIX: Load key terms from config instead of hardcoding
                key_terms = settings.AGENTIC_CONFIG.get('catalog_key_terms',
                    ['feature', 'overview', 'specification', 'introduction'])
                
                priority_chapters = []
                other_chapters = []
                for ch in chapters:
                    title_lower = (ch["section_title"] or "").lower()
                    if any(term in title_lower for term in key_terms):
                        priority_chapters.append(ch)
                    else:
                        other_chapters.append(ch)
                
                # Keep priority chapters + others, max total
                cfg = settings.AGENTIC_CONFIG
                priority_max = cfg.get('catalog_priority_chapters_max', 20)
                other_max = cfg.get('catalog_other_chapters_max', 10)
                total_max = cfg.get('catalog_total_chapters_max', 30)
                selected = priority_chapters[:priority_max] + other_chapters[:other_max]
                selected.sort(key=lambda x: x["start_page"])
                
                for ch in selected:
                    doc_info["chapters"].append({
                        "title": ch["section_title"],
                        "pages": f"{ch['start_page']}-{ch['end_page']}"
                    })
                
                catalog["documents"].append(doc_info)
        
        return catalog
    
    def _get_document_structure(self, doc_title: str) -> list:
        """Get the chapter structure of a document"""
        # Find from catalog
        for doc in self.catalog.get("documents", []):
            if doc["title"] == doc_title or doc_title in doc["title"]:
                return [ch["title"] for ch in doc.get("chapters", [])]
        
        # If not in catalog, query from database
        with self.metadata_db.get_connection() as conn:
            # Find doc_id
            doc = conn.execute(
                "SELECT id FROM documents WHERE title LIKE ?",
                (f"%{doc_title}%",)
            ).fetchone()
            
            if not doc:
                return []
            
            # Get chapter structure
            rows = conn.execute(
                "SELECT section_title FROM doc_structure_index "
                "WHERE doc_id = ? ORDER BY start_page",
                (doc["id"],)
            ).fetchall()
            
            return [r["section_title"] for r in rows if r["section_title"]]
    
    def _expand_keywords(self, query: str, doc_title: str = "") -> list:
        """
        Have the model generate expanded keywords based on document chapter structure.
        No hardcoding; keywords must come from actual document chapters.
        """
        # First get the document's chapter structure
        chapters = self._get_document_structure(doc_title)
        
        if not chapters:
            # If structure unavailable, fall back to original query terms
            return [query]
        
        prompt = f"""Query: {query}

Document: {doc_title}

Document chapter structure:
{json.dumps(chapters[:settings.AGENTIC_CONFIG.get('expand_chapters_max', 20)], ensure_ascii=False)}  # Max chapters to avoid exceeding context

From the chapter titles above, select 3-5 chapter title keywords most relevant to the query.
Only choose from existing chapter titles; do not generate new terms.

**Important**: If the query involves specific features (e.g., "video encoding", "camera interface"), prioritize **parent chapters** that contain those features (e.g., "Features", "Overview"), as these chapters typically contain detailed sub-feature parameters.

Output as JSON:
{{"keywords": ["chapter title 1", "chapter title 2"]}}
"""
        
        data = self.model_client.generate_json(prompt, max_tokens=1024, temperature=0.3)
        
        keywords = data.get("keywords", []) if data else []
        # Cap at configured max
        kw_max = settings.AGENTIC_CONFIG.get('expand_keywords_max', 5)
        return keywords[:kw_max]
    
    def _fts_search(self, query: str, doc_id: str = None, top_k: int = 10, keywords: list = None) -> list:
        """FTS search - using model-generated keywords"""
        results = []
        
        # If no keywords provided, extract from query
        if not keywords:
            keywords = []
            for token in query.split():
                token = token.strip()
                if len(token) >= 3:
                    keywords.append(token)
        
        if not keywords:
            return results
        
        # Build FTS query - ensure keyword safety
        safe_keywords = []
        for kw in keywords:
            # Remove FTS special characters and parenthesized content
            kw = kw.replace('"', '').replace("'", "")
            # Remove parentheses and their content (e.g., "Video Codec (Neural Process Unit)" -> "Video Codec")
            import re
            kw = re.sub(r'\s*\([^)]*\)', '', kw)
            # Keep only first N words to avoid excessive length
            word_limit = settings.AGENTIC_CONFIG.get('fts_keyword_word_limit', 3)
            words = kw.split()[:word_limit]
            kw = ' '.join(words)
            min_len = settings.AGENTIC_CONFIG.get('fts_min_keyword_length', 2)
            if len(kw) >= min_len:
                safe_keywords.append(kw)
        
        # Deduplicate
        safe_keywords = list(dict.fromkeys(safe_keywords))
        
        if not safe_keywords:
            return results
        
        # Join with OR, wrap each keyword in quotes
        fts_max_kw = settings.AGENTIC_CONFIG.get('fts_max_keywords', 10)
        fts_query = ' OR '.join(f'"{kw}"' for kw in safe_keywords[:fts_max_kw])  # Max keywords
        
        with self.metadata_db.get_connection() as conn:
            if doc_id:
                rows = conn.execute(
                    "SELECT dp.id as page_id, dp.page_num, dp.section_title, dp.raw_text, "
                    "fts.rank as rank_score "
                    "FROM doc_pages_fts fts "
                    "JOIN doc_pages dp ON fts.rowid = dp.id "
                    "WHERE fts.doc_pages_fts MATCH ? AND dp.doc_id = ? "
                    "ORDER BY fts.rank LIMIT ?",
                    (fts_query, doc_id, top_k)
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT dp.id as page_id, dp.page_num, dp.section_title, dp.raw_text, "
                    "fts.rank as rank_score "
                    "FROM doc_pages_fts fts "
                    "JOIN doc_pages dp ON fts.rowid = dp.id "
                    "WHERE fts.doc_pages_fts MATCH ? "
                    "ORDER BY fts.rank LIMIT ?",
                    (fts_query, top_k)
                ).fetchall()
            
            for r in rows:
                results.append({
                    'page_id': r['page_id'],
                    'page_num': r['page_num'],
                    'section_title': r['section_title'] or '',
                    'score': -r['rank_score'] if r['rank_score'] else 1.0,
                    'source': 'fts'
                })
        
        return results
    
    def _semantic_search(self, query: str, doc_id: str = None, top_k: int = 10) -> list:
        """Semantic search - returns chunk with full text"""
        query_emb = np.array(self.model_client.embed(query))
        
        results = []
        for chunk in self.vec_index:
            if doc_id and chunk["doc_id"] != doc_id:
                continue
            
            score = np.dot(query_emb, chunk["embedding"]) / (
                np.linalg.norm(query_emb) * np.linalg.norm(chunk["embedding"])
            )
            results.append((score, chunk))
        
        results.sort(key=lambda x: x[0], reverse=True)
        return results[:top_k]
    
    def _hybrid_search(self, query: str, doc_id: str = None, top_k: int = 10, 
                       strategy: dict = None, keywords: list = None) -> list:
        """
        Hybrid search - strategy passed in externally
        """
        strategy = strategy or {}
        
        # Get strategy parameters (no hardcoded defaults beyond these fallback values)
        cfg = settings.AGENTIC_CONFIG
        fts_first = strategy.get('fts_first', True)
        fts_threshold = strategy.get('fts_threshold', cfg.get('fts_threshold', 5.0))
        min_fts_results = strategy.get('min_fts_results', cfg.get('fts_min_results', 2))
        vector_fallback = strategy.get('vector_fallback', True)
        
        # 1. FTS search
        fts_results = self._fts_search(query, doc_id, top_k=top_k, keywords=keywords)
        
        # 2. Check if FTS results are good enough
        has_exact = any(r.get('score', 0) > fts_threshold for r in fts_results)
        
        if fts_first and has_exact and len(fts_results) >= min_fts_results:
            return fts_results
        
        # 3. Supplement with vector search
        if vector_fallback:
            vec_results = self._semantic_search(query, doc_id, top_k=top_k)
            
            # Merge results
            combined = {}
            for r in fts_results:
                combined[r['page_id']] = r
            
            for score, chunk in vec_results:
                page_id = chunk['page_id']
                chunk_idx = chunk.get('chunk_idx', 0)
                key = f"{page_id}_{chunk_idx}"
                if key not in combined:
                    combined[key] = {
                        'page_id': page_id,
                        'chunk_idx': chunk_idx,
                        'score': score * settings.AGENTIC_CONFIG.get('vector_score_scale', 10.0),
                        'source': 'vector',
                        'chunk_text': chunk.get('chunk_text', '')
                    }
            
            results = sorted(combined.values(), key=lambda x: x['score'], reverse=True)
            return results[:top_k]
        
        return fts_results
    
    def _read_pages(self, doc_id: str, page_nums: list) -> list:
        """Read specified pages"""
        pages = []
        
        with self.metadata_db.get_connection() as conn:
            placeholders = ','.join('?' * len(page_nums))
            rows = conn.execute(
                f"SELECT page_num, section_title, raw_text "
                f"FROM doc_pages WHERE doc_id = ? AND page_num IN ({placeholders}) "
                f"ORDER BY page_num",
                (doc_id,) + tuple(page_nums)
            ).fetchall()
            
            for r in rows:
                pages.append({
                    'page_num': r['page_num'],
                    'section_title': r['section_title'] or '',
                    'content': r['raw_text'] or ''
                })
        
        return pages
    
    def _verify_pages_have_answer(self, pages: list, query: str, doc_title: str = "") -> dict:
        """Verify whether pages contain the answer"""
        if not pages:
            return {"has_answer": False, "reason": "No page content"}
        
        # Concatenate page content (only include pages relevant to the question, avoiding noise)
        relevant_pages = []
        verification_config = self.config.get('verification', {})
        relevant_keywords = verification_config.get('relevant_keywords', [])
        fallback_count = verification_config.get('fallback_page_count', 2)
        
        if relevant_keywords:
            for p in pages:
                content = p['content']
                if any(kw in content.lower() for kw in relevant_keywords):
                    relevant_pages.append(p)
        
        if not relevant_pages:
            relevant_pages = pages[:fallback_count]  # If no relevant pages found, take first N
        
        v_cfg = settings.AGENTIC_CONFIG
        pages_text = "\n\n".join([
            f"Page {p['page_num']} ({p['section_title']}):\n{p['content'][:v_cfg.get('verify_page_content_limit', 1500)]}"
            for p in relevant_pages[:v_cfg.get('verify_max_pages', 2)]
        ])
        
        # FIX: Load comparison keywords from config instead of hardcoding Chinese terms
        compare_kw_cfg = settings.AGENTIC_CONFIG.get('compare_keywords', ['vs', 'versus', 'compare', 'comparison', 'diff', 'difference'])
        is_compare = any(kw in query.lower() for kw in compare_kw_cfg)
        
        if is_compare:
            entity = doc_title.strip().split()[0] if doc_title and doc_title.strip() else "该文档"
            prompt = f"""Please determine whether the following document excerpts contain specification parameters for {entity}.

Question: {query}
Document excerpts:
{pages_text}

Assessment criteria:
- If the excerpts contain specific specification data for {entity} (e.g., CPU architecture, compute power, memory, etc.), respond with has_answer=true
- If the excerpts only contain table of contents, cover pages, or completely irrelevant content, respond with has_answer=false
- A complete listing of all parameters is not required; having relevant specification data is sufficient

Output as JSON:
{{"has_answer": true/false, "evidence": "Specific data found (if any)", "reason": "Reason for the assessment"}}
"""
        else:
            prompt = f"""Please determine whether the following document excerpts contain information relevant to the user's question.

Question: {query}

Document excerpts:
{pages_text}

Assessment criteria:
- If the document excerpts contain technical parameters, feature descriptions, or specification data relevant to the user's question, respond with has_answer=true
- If the excerpts only contain table of contents, cover pages, or completely irrelevant content, respond with has_answer=false
- A complete listing of all information is not required; having relevant data is sufficient
- If the excerpt is truncated but clearly mentions a relevant topic (e.g., "Video Encoder: support H.26x"), treat it as containing relevant information

Output as JSON:
{{"has_answer": true/false, "evidence": "Relevant information found (if any)", "reason": "Reason for the assessment"}}
"""
        
        data = self.model_client.generate_json(prompt, max_tokens=1024, temperature=0.3)
        
        if data and data.get("has_answer") is not None:
            return data
        else:
            total_chars = sum(len(p["content"]) for p in pages)
            return {"has_answer": total_chars > 500, "reason": "Content length heuristic"}
    
    def _extract_notes(self, pages: list, query: str) -> str:
        """Take notes"""
        e_cfg = settings.AGENTIC_CONFIG
        extract_limit = e_cfg.get('extract_page_content_limit', 2000)
        pages_text = "\n\n".join([
            f"Page {p['page_num']} ({p['section_title']}):\n{p['content'][:extract_limit]}"
            for p in pages
        ])
        
        prompt = f"""Please read the following document excerpts and extract key information relevant to the user's question.

User question: {query}

Document excerpts:
{pages_text}

Please extract:
1. Specific technical parameters and data
2. Supported standards/protocols
3. Performance metrics (speed, resolution, bandwidth, etc.)
4. Key features

Output as concise notes, preserving original citations."""
        
        return self.model_client.generate(prompt, max_tokens=2000, temperature=0.3)
    
    def _query_single_document(self, query: str, doc_id: str, doc_title: str) -> dict:
        """
        Execute an independent query against a single document, as if the user asked this question in isolation.
        Returns: {"has_answer": bool, "answer": str, "pages": list, "sources": list}
        """
        # 1. Keyword expansion
        expanded_keywords = self._expand_keywords(query, doc_title)
        
        # 2. Hybrid search
        first_word = doc_title.strip().split()[0] if doc_title and doc_title.strip() else ""
        search_query = f"{first_word} {query}" if first_word else query
        hybrid_results = self._hybrid_search(
            search_query, doc_id=doc_id, top_k=15,
            keywords=expanded_keywords
        )
        
        # 3. Structure filtering + expansion
        page_filter_config = self.config.get('page_filtering', {})
        skip_keywords = page_filter_config.get('skip_keywords', [])
        expand_pages = page_filter_config.get('expand_pages', 0)
        
        candidate_pages = set()
        candidate_chunks = []  # Store chunk_text from vector results
        for r in hybrid_results:
            page_num = r.get('page_num', 0)
            section_title = r.get('section_title', '')
            if skip_keywords and any(kw in section_title.lower() for kw in skip_keywords):
                continue
            if page_num > 0:
                candidate_pages.add(page_num)
                if expand_pages > 0:
                    for p in range(page_num + 1, page_num + 1 + expand_pages):
                        candidate_pages.add(p)
            # Collect chunk_text from vector results for direct use
            if r.get('chunk_text'):
                candidate_chunks.append(r['chunk_text'])
        
        q_cfg = settings.AGENTIC_CONFIG
        candidate_pages = sorted(list(candidate_pages))[:q_cfg.get('candidate_pages_max', 10)]
        if not candidate_pages and not candidate_chunks:
            return {"has_answer": False, "answer": "", "pages": [], "sources": []}
        
        # 4. Read pages
        pages = self._read_pages(doc_id, candidate_pages)
        
        # 5. Generate answer directly (like a single query, no verification step)
        query_limit = q_cfg.get('query_page_content_limit', 2000)
        content_parts = []
        
        # Add page content from database
        for p in pages:
            content_parts.append(f"Page {p['page_num']} ({p['section_title']}):\n{p['content'][:query_limit]}")
        
        # Add chunk_text from vector search results (contains complete chunk text)
        for i, chunk_text in enumerate(candidate_chunks[:5]):  # Limit to top 5 chunks
            content_parts.append(f"[Retrieved Chunk {i+1}]:\n{chunk_text[:query_limit]}")
        
        pages_text = "\n\n".join(content_parts)
        
        prompt = f"""Based on the following document content, extract technical parameters and specification information relevant to the question.

User question: {query}

Document: {doc_title}

Document content:
{pages_text}

Please extract all technical parameters, performance metrics, supported standards/protocols, etc. from this document that are relevant to the question.
If the document truly does not contain relevant information, please reply only with "No relevant information found".
You are consulting the documentation for {doc_title}. Please only extract information from this document; the query may involve multiple entities, but you only need to cover the portions covered by this document.
请用中文回答。"""
        
        answer = self.model_client.generate(prompt, max_tokens=2000, temperature=0.3)
        
        # Determine: as long as the answer doesn't explicitly say "not found" and has sufficient length, consider it as having an answer
        min_len = settings.AGENTIC_CONFIG.get('answer_min_length', 100)
        has_answer = "No relevant information found" not in answer and "未找到相关信息" not in answer and len(answer) > min_len
        
        return {
            "has_answer": has_answer,
            "answer": answer,
            "pages": [p['page_num'] for p in pages],
            "sources": [{
                "doc_id": doc_id,
                "title": doc_title,
                "pages": [p['page_num'] for p in pages]
            }]
        }
    
    def retrieve(self, query: str) -> dict:
        """
        Complete Agent workflow - main public interface.
        Returns format compatible with the existing engine.
        """
        logger.info(f"[AGENTIC] Query: {query}")
        
        # Step 1: Understand intent + locate documents
        query_lower = query.lower()
        mentioned_docs = []
        for doc in self.catalog.get("documents", []):
            title = doc.get("title", "")
            model_match = re.search(r'([A-Z][A-Z0-9]+\d+[A-Z]?)', title)
            if model_match:
                model_name = model_match.group(1)
                if model_name.lower() in query_lower:
                    mentioned_docs.append(doc)
        
        # Force-include hint
        forced_selection = ""
        if mentioned_docs:
            forced_selection = "\n\n**Mandatory selection rule**:\n"
            forced_selection += f"The query explicitly mentions the following models: {', '.join(d.get('title') for d in mentioned_docs)}\n"
            forced_selection += "These documents MUST be listed in target_docs!"
        
        analysis_prompt = f"""You are a librarian. Please carefully review all collection documents and answer the reader's question.

Collection document list:
{json.dumps(self.catalog, ensure_ascii=False, indent=2)[:settings.AGENTIC_CONFIG.get('catalog_json_max', 4000)]}

Reader question: "{query}"{forced_selection}

Important notes:
- Determine which product a document corresponds to based on model names and keywords in the document titles
- "Datasheet / Technical Manual" type documents contain product technical specification parameters
- "User Manual" type documents contain usage instructions and configuration guides
- "Reference Manual" type documents contain detailed technical references and programming guides

Please analyze:
1. What does the reader want?
2. Which documents might contain the answer?
3. Which chapters in each document are most relevant? Provide specific page numbers.

Output as JSON:
{{
    "intent": "Intent description",
    "target_docs": [
        {{
            "doc_id": "ID",
            "title": "Title",
            "reason": "Why selected",
            "target_pages": [8, 9, 10]
        }}
    ]
}}
"""
        
        analysis = self.model_client.generate_json(analysis_prompt, max_tokens=2048, temperature=0.3)
        
        if not analysis:
            analysis = {"target_docs": []}
        
        target_docs = analysis.get('target_docs', [])
        
        # Force-include documents explicitly mentioned in the query
        if mentioned_docs:
            existing_ids = {d.get('doc_id') for d in target_docs}
            for doc in mentioned_docs:
                if doc.get('doc_id') not in existing_ids:
                    logger.info(f"[AGENTIC] Force-including missed document: {doc.get('title')}")
                    target_docs.append({
                        'doc_id': doc.get('doc_id'),
                        'title': doc.get('title'),
                        'reason': 'Model explicitly mentioned in query',
                        'target_pages': []
                    })
        
        logger.info(f"[AGENTIC] Target documents: {[d.get('title') for d in target_docs]}")
        
        # Step 2: Query each target document independently (like a single query)
        # Group by product model to ensure at least one document per model is queried
        from collections import defaultdict
        doc_groups = defaultdict(list)  # model_name -> [doc_info, ...]
        
        for doc_info in target_docs:
            doc_title = doc_info.get("title", "")
            model_match = re.search(r'([A-Z][A-Z0-9]+\d+[A-Z]?)', doc_title)
            if model_match:
                model_name = model_match.group(1)
                doc_groups[model_name].append(doc_info)
            else:
                doc_groups["other"].append(doc_info)
        
        all_results = []
        all_sources = []
        
        for model_name, docs in doc_groups.items():
            model_found = False
            
            # Documents are already sorted by the LLM in Step 1; query in order
            for doc_info in docs:
                doc_id = doc_info.get("doc_id")
                doc_title = doc_info.get("title", "Unknown")
                
                logger.info(f"[AGENTIC] Independent query: {doc_title}")
                result = self._query_single_document(query, doc_id, doc_title)
                
                if result.get("has_answer"):
                    all_results.append({
                        "doc_title": doc_title,
                        "answer": result["answer"]
                    })
                    all_sources.extend(result.get("sources", []))
                    logger.info(f"[AGENTIC] {doc_title} - answer found")
                    model_found = True
                    break  # Answer found for this product, skip remaining documents
                else:
                    logger.info(f"[AGENTIC] {doc_title} - no answer found")
            
            if not model_found and model_name != "other":
                logger.warning(f"[AGENTIC] {model_name} - no answer found in any document")
        
        # Step 3: Merge results + original question → final answer
        if not all_results:
            return {
                "query": query,
                "context": "",
                "sources": [],
                "total_results": 0,
                "total_chars": 0,
                "strategy": "agentic_retrieve",
                "answer": "根据当前知识库中的文档，未找到相关信息。"
            }
        
        # Build merge prompt
        individual_answers = "\n\n".join([
            f"=== {r['doc_title']} ===\n{r['answer']}"
            for r in all_results
        ])
        
        summary_prompt = f"""Based on the independent query results from each document below, answer the user's original question.

User's original question: {query}

Results from each document:
{individual_answers}

Answering rules:
1. Synthesize the above information to provide a complete and accurate answer
2. For comparison questions, present parameter comparisons using a table
3. Cite information sources (document names)
4. For each product, only answer based on information actually provided in that product's documentation. If a product's documentation mentions a parameter (e.g., H.264 encoding frame rate), you may quote it faithfully; if the documentation makes no mention of a parameter or feature at all (e.g., the doc only mentions H.264 and never H.265), do NOT infer that the product also supports it just because another product does
5. **Comparison table iron rule: If a product truly has no data or explicitly does not support a given dimension, that cell MUST be marked as "Not mentioned" or "Not supported". Never fabricate data from other products to fill the table for completeness**
6. **Key judgment: If the retrieved document content truly lacks the specific information the user is looking for, objectively state "Based on the document content in the current knowledge base, no relevant information was found." This is a data completeness issue in the knowledge base, not a limitation of your capability. Never fabricate, guess, or mix in your own knowledge.**
**Note: Cross-document synthesis is NOT fabrication — putting parameters documented separately in different documents into a single comparison table is expected behavior, as long as the data comes from the actual content of each document.**
7. Do not simply declare "insufficient information" just because a document doesn't directly list a certain parameter — if the document contains indirectly relevant parameters (e.g., image processing capability, maximum resolution), these can serve as supplementary references, but you must clearly state in the answer that they are indirect inferences

请用中文回答。"""
        
        answer = self.model_client.generate(summary_prompt, max_tokens=settings.AGENTIC_CONFIG.get('summary_max_tokens', 4000), temperature=0.3)
        
        context = individual_answers
        
        return {
            "query": query,
            "context": context,
            "sources": all_sources,
            "total_results": sum(len(s.get("pages", [])) for s in all_sources),
            "total_chars": len(context),
            "strategy": "agentic_retrieve",
            "answer": answer
        }
    
    def release(self):
        """Release resources"""
        logger.info("[AGENTIC] Resources released")
