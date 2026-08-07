"""
Four-Level Knowledge Pyramid Index Builder
Multi-tenant + industry plugin system adapter
"""
import gc
import hashlib
import logging
from pathlib import Path
from typing import Any

from PIL import Image

from ..config import INGEST_MAX_WORKERS, SECTION_ENTITY_HARVEST_ENABLED, settings
from ..db.tenant_db import get_tenant_metadata_db, get_tenant_vector_db
from ..models import get_model_client
from ..plugins import get_plugin_registry
from .classifier import DocumentClassifier
from .layout import ChartAnalyzer, FormulaRecognizer, LayoutAnalyzer
from .parser import DocumentParser, ParsedDocument, ParsedPage
from .preprocessing import DocumentPreprocessor, PagePreprocessResult

logger = logging.getLogger(__name__)


class DocumentIndexBuilder:
    """Document index builder (two layers: L2 content index + vector embeddings)"""

    def __init__(self, tenant_id: str = None):
        self.tenant_id = tenant_id
        self.model_client = get_model_client()
        self.parser = DocumentParser()
        self.classifier = DocumentClassifier()
        self.preprocessor = DocumentPreprocessor()
        self.layout_analyzer = LayoutAnalyzer(settings.LAYOUT_CONFIG)
        self.formula_recognizer = FormulaRecognizer()
        self.chart_analyzer = ChartAnalyzer(settings.CHART_CONFIG)

    def _get_dbs(self, tenant_id: str = None):
        """Get tenant databases"""
        tid = tenant_id or self.tenant_id or "default"
        return get_tenant_metadata_db(tid), get_tenant_vector_db(tid)

    def ingest_document(self, file_path: str, tenant_id: str = None,
                        industry_hint: str = None,
                        auto_confirm: bool = False,
                        progress_callback=None) -> dict[str, Any]:
        """Complete document ingestion workflow"""
        tid = tenant_id or self.tenant_id or "default"
        # Store for backward compat (some downstream code may read self.tenant_id)
        self.tenant_id = tid

        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        metadata_db, _ = self._get_dbs(tid)

        # 1. Compute file MD5
        file_hash = self._compute_file_hash(file_path)
        logger.info(f"[HASH_DEBUG] ingest: tenant_id={self.tenant_id}, db_path={metadata_db.db_path}")

        # 2. Check if already imported
        logger.info(f"[HASH_DEBUG] ingest: computed hash={file_hash}, checking dup...")
        existing_doc = metadata_db.get_document_by_hash(file_hash)
        if existing_doc:
            logger.info(f"File already imported (duplicate skipped): {path.name} -> doc_id={existing_doc['id']}")
            return {
                "doc_id": existing_doc["id"],
                "status": "already_imported",
                "file_hash": file_hash,
                "filename": path.name,
                "message": f"This file was already imported at {existing_doc.get('created_at', 'unknown')}, skipping"
            }

        # 3. Parse document
        logger.info(f"Parsing document: {file_path}")
        parsed_doc = self.parser.parse(file_path)

        # Generate document ID
        doc_id = self._generate_doc_id(parsed_doc)

        # 4. Preprocess
        logger.info(f"OpenLAD: Preprocessing document: {path.name}")
        preprocessed_pages = self._preprocess_document(parsed_doc, doc_id)

        # 5. Save preliminary info
        text_source = self._determine_text_source(preprocessed_pages)
        metadata_db.save_document(
            doc_id=doc_id,
            filename=parsed_doc.filename,
            original_path=parsed_doc.original_path,
            status="pending_meta",
            file_hash=file_hash,
            text_source=text_source,
            industry_package_id=industry_hint
        )
        # DEBUG: verify save
        verify_doc = metadata_db.get_document(doc_id)
        logger.info(f"[HASH_DEBUG] ingest: after save, get_document(doc_id) returns: found={verify_doc is not None}, file_hash={verify_doc.get('file_hash') if verify_doc else 'None'}")

        result = {
            "doc_id": doc_id,
            "status": "pending_meta",
            "filename": parsed_doc.filename,
            "total_pages": parsed_doc.total_pages,
            "file_hash": file_hash,
            "text_source": text_source,
        }

        # If auto_confirm, start building index immediately
        if auto_confirm:
            return self.build_index(doc_id, parsed_doc, preprocessed_pages,
                                    industry_hint, progress_callback,
                                    file_hash=file_hash, tenant_id=tid)

        return result

    def build_index(self, doc_id: str, parsed_doc: ParsedDocument = None,
                    preprocessed_pages: list = None,
                    industry_hint: str = None,
                    progress_callback=None,
                    file_hash: str = None,
                    tenant_id: str = None) -> dict[str, Any]:
        """Build document index (two layers: L2 content index + vector embeddings)

        Supports progress_callback(progress_percent, message) for reporting progress.
        file_hash: MD5 hash of the file, passed from ingest_document to avoid
        overwriting due to DB readback failures.
        tenant_id: required for thread-safe multi-tenant support.
        """
        tid = tenant_id or self.tenant_id or "default"

        def _report(p, msg):
            if progress_callback:
                try:
                    progress_callback(p, msg)
                except Exception:
                    pass

        logger.info(f"Building index for doc: {doc_id}")
        metadata_db, vector_db = self._get_dbs(tid)
        logger.info(f"[HASH_DEBUG] build_index: tenant_id={tid}, db_path={metadata_db.db_path}")

        # Set chart analyzer's images_dir (based on current tenant)
        if tid:
            self.chart_analyzer.images_dir = settings.get_tenant_images_dir(tid)

        _report(10, "Parsing document")
        if parsed_doc is None:
            doc = metadata_db.get_document(doc_id)
            if not doc:
                raise ValueError(f"Document not found: {doc_id}")
            parsed_doc = self.parser.parse(doc["original_path"])

        _report(20, "Preprocessing pages")
        if preprocessed_pages is None:
            preprocessed_pages = self._preprocess_document(parsed_doc, doc_id)

        # Load industry plugin
        plugin = None
        registry = get_plugin_registry()
        if industry_hint:
            plugin = registry.get_plugin(industry_hint)

        # Auto-detect industry plugin when no explicit hint is provided.
        # This lets industry packages with detect_document_subtype() hooks
        # (e.g., schematics) take over without forcing users to select the
        # package manually, while leaving generic documents unchanged.
        if plugin is None and parsed_doc is not None:
            plugin = registry.detect_plugin_for_document(parsed_doc)

        # L2: Page-level processing
        _report(30, "Building L2 page index")
        l2_results = self._build_l2(doc_id, parsed_doc, preprocessed_pages, tid, plugin)

        # Document classification
        # V5.0: Generate document-level summary before classification
        _report(50, "Generating document summary")
        logger.info(f"[SUMMARY] Generating document-level summary for: {parsed_doc.filename}")
        doc_summary = self._generate_document_summary(doc_id, parsed_doc, l2_results)
        logger.info(f"[SUMMARY] Document summary length: {len(doc_summary)} chars")

        _report(55, "Classifying document")
        logger.info(f"[CLASSIFY] Starting classification: {parsed_doc.filename}")
        content_sample = self._get_content_sample_for_doc(doc_id, parsed_doc)
        classification = self.classifier.classify(
            filename=parsed_doc.filename,
            title=self._extract_title(parsed_doc.filename),
            content_sample=content_sample,
            plugin=plugin
        )
        logger.info(f"[CLASSIFY] Result: L1={classification['category_level1']}, "
                   f"L2={classification['category_level2']}, L3={classification['category_level3']}")

        # Generate L2 page vector embeddings
        _report(75, "Generating L2 vector embeddings")
        self._build_embeddings(doc_id, l2_results, tid)

        # Update document status
        _report(95, "Saving index results")
        text_source = self._determine_text_source(preprocessed_pages)
        doc_type = classification["category_level2"] or "Other"
        existing_doc = metadata_db.get_document(doc_id)
        logger.info(f"[HASH_DEBUG] build_index save: existing_doc keys={list(existing_doc.keys()) if existing_doc else 'None'}, file_hash={existing_doc.get('file_hash') if existing_doc else 'N/A'}, passed_hash={file_hash}")
        # Prefer passed-in file_hash (from ingest_document, guaranteed non-None)
        final_hash = file_hash or (existing_doc.get("file_hash") if existing_doc else None)
        if not final_hash:
            logger.warning(f"[HASH_DEBUG] build_index: file_hash is None! existing_doc={existing_doc is not None}, passed_hash={file_hash}")
        metadata_db.save_document(
            doc_id=doc_id,
            filename=parsed_doc.filename,
            original_path=parsed_doc.original_path,
            title=self._extract_title(parsed_doc.filename),
            doc_type=doc_type,
            metadata=parsed_doc.metadata,
            status="verified",
            file_hash=final_hash,
            text_source=text_source,
            category_level1=classification["category_level1"],
            category_level2=classification["category_level2"],
            category_level3=classification["category_level3"],
            industry_package_id=industry_hint,
            summary=doc_summary
        )

        _report(100, "Document processing complete")
        logger.info(f"Index built for doc: {doc_id}")

        # Industry package document complete callback
        if plugin and hasattr(plugin.ingestion, 'on_document_complete'):
            try:
                plugin.ingestion.on_document_complete(doc_id, metadata_db)
            except Exception as e:
                logger.warning(f"[BUILDER] Industry package on_document_complete callback failed: {e}")

        gc.collect()
        try:
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass

        return {
            "doc_id": doc_id,
            "status": "verified",
            "l2_page_count": len(l2_results),
            "text_source": text_source
        }

    def _preprocess_document(self, parsed_doc: ParsedDocument,
                            doc_id: str) -> list[Any]:
        """Concurrently preprocess all pages of the document"""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        _thread_local = threading.local()

        def _get_preprocessor():
            if not hasattr(_thread_local, 'preprocessor'):
                _thread_local.preprocessor = DocumentPreprocessor()
            return _thread_local.preprocessor

        def _process_page(idx: int):
            page = parsed_doc.pages[idx]
            page_class = page.content_dict.get('page_class', 'TEXT')

            if page_class == 'BLANK':
                # Blank pages have no extractable content; skip OCR and image correction
                # entirely to avoid wasting compute. Still persist a placeholder image
                # so downstream consumers expecting a page_image_path do not break.
                result = PagePreprocessResult()
                result.page_num = page.page_num
                result.raw_text = ""
                result.text_source = "blank"
                page_image = getattr(page, 'page_image', None)
                if page_image is None:
                    page_image = Image.new('RGB', (800, 1000), color='white')
                image_filename = f"{doc_id}_p{page.page_num}.png" if doc_id else f"page_{page.page_num}.png"
                image_path = settings.IMAGES_DIR / image_filename
                page_image.save(image_path, "PNG")
                result.page_image_path = str(image_path)
                return page.page_num, result

            force_ocr = not page.raw_text or len(page.raw_text.strip()) < 20
            page_image = getattr(page, 'page_image', None)
            if page_image is None:
                page_image = Image.new('RGB', (800, 1000), color='white')

            preprocessor = _get_preprocessor()
            result = preprocessor.preprocess_pdf_page(
                page_image=page_image,
                page_num=page.page_num,
                direct_text=page.raw_text,
                force_ocr=force_ocr,
                doc_id=doc_id
            )
            return page.page_num, result

        max_workers = min(INGEST_MAX_WORKERS, len(parsed_doc.pages))
        if max_workers < 1:
            max_workers = 1
        page_results = {}

        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_process_page, i): i for i in range(len(parsed_doc.pages))}
                for future in futures:
                    try:
                        page_num, result = future.result()
                        page_results[page_num] = result
                    except Exception as e:
                        logger.error(f"Page preprocess failed: {e}", exc_info=True)
        else:
            for i in range(len(parsed_doc.pages)):
                page_num, result = _process_page(i)
                page_results[page_num] = result

        return [page_results[page.page_num] for page in parsed_doc.pages]

    @staticmethod
    def _sanitize_page_text(text: str) -> str:
        """Sanitize page text: filter control chars + PUA garbage + deduplicate watermarks + table format optimization.

        Root cause: PDF text extraction preserves three types of noise:
        1. Format control characters (e.g. 0x01 SOH)
        2. PDF embedded font private encoding (Unicode PUA U+E000-U+F8FF)
        3. Repeated header/footer watermark lines
        4. Duplicate table content (plain text + Markdown table coexist)
        These wastes context quota and may interfere with FTS retrieval and LLM understanding.

        Strategy (generalized, not document-specific):
        1. Filter 0x00-0x1F control chars (keep \\n\\r\\t)
        2. Filter Unicode Private Use Area chars (U+E000-U+F8FF)
        3. Detect lines that repeat ≥3 times on a single page (>15 chars) as watermarks
        4. If page contains both plain text and Markdown tables, prefer Markdown format
        """
        if not text:
            return text

        # Step 1: filter control chars + PUA garbage
        def _is_valid_char(ch: str) -> bool:
            o = ord(ch)
            # Keep newline, carriage return, tab
            if ch in '\n\r\t':
                return True
            # Filter control chars 0x00-0x1F
            if o < 32:
                return False
            # Filter Unicode Private Use Area (PUA)
            # PDF embedded fonts often use PUA to encode math symbols; becomes garbage after extraction
            if 0xE000 <= o <= 0xF8FF:
                return False
            return True

        sanitized = ''.join(ch for ch in text if _is_valid_char(ch))

        # Step 2: watermark dedup (based on statistical features)
        lines = sanitized.split('\n')
        line_counts = {}
        for line in lines:
            stripped = line.strip()
            if len(stripped) > 15:  # Only consider long lines (short lines may be normal formatting)
                line_counts[stripped] = line_counts.get(stripped, 0) + 1

        # Mark lines that repeat ≥3 times as watermarks
        watermarks = {line for line, count in line_counts.items() if count >= 3}

        if watermarks:
            filtered_lines = []
            for line in lines:
                stripped = line.strip()
                if stripped in watermarks:
                    continue  # Remove watermark lines
                filtered_lines.append(line)
            sanitized = '\n'.join(filtered_lines)

        # Step 3: FIX - table format optimization
        # If page contains both plain text tables and Markdown tables, prefer Markdown
        # Detection: if text has [Table N] markers and | separators → Markdown tables present
        # DISABLED: this heuristic is too coarse and may delete legitimate content (e.g. BLOCK DIAGRAM).
        # The cost of duplicate tables is far less than the cost of deleting legitimate content. See notes/2026-06-08.md
        # if '[Table' in sanitized and '|' in sanitized:
        #     sanitized = DocumentIndexBuilder._optimize_table_format(sanitized)

        return sanitized

    @staticmethod
    def _optimize_table_format(text: str) -> str:
        """
        Optimize table format. If a page contains both plain text and Markdown tables,
        remove plain text duplicates, keep Markdown table format (easier for LLM to understand).
        """
        import re

        # Find [Table N] marker positions
        table_markers = list(re.finditer(r'\n\s*\[Table\s*\d+\]\s*\n', text))

        if not table_markers:
            return text

        # If only one table marker with lots of plain text before it, likely duplicate content
        # Strategy: keep Markdown tables, remove preceding plain text table content
        first_marker = table_markers[0]
        marker_start = first_marker.start()

        # Check if content before marker contains lots of numbers (likely plain text table)
        prefix = text[:marker_start]
        prefix_lines = prefix.strip().split('\n')

        # If prefix exceeds 15 lines and contains multiple number-lines, treat as plain text table, remove
        number_lines = sum(1 for line in prefix_lines if re.search(r'\d{3,}', line.strip()))
        if len(prefix_lines) > 15 and number_lines > 5:
            # Keep Markdown table content after the marker
            result = text[marker_start:]
            # Remove the [Table N] marker itself, keep the table content
            result = re.sub(r'^\s*\[Table\s*\d+\]\s*\n', '\n', result)
            return result.strip()

        return text

    def _build_l2(self, doc_id: str, parsed_doc: ParsedDocument,
                  preprocessed_pages: list, tid: str, plugin=None) -> list[dict]:
        """Build L2 layer: page-level processing"""
        from concurrent.futures import ThreadPoolExecutor
        metadata_db, vector_db = self._get_dbs(tid)

        # Clean up old data
        metadata_db.delete_document(doc_id)

        # Document-level entity for spec facts (from filename — the only
        # reliable title source at this stage).
        from .spec_facts_extractor import infer_doc_entity
        spec_entity = infer_doc_entity(parsed_doc.filename) if parsed_doc else ""

        # Industry package document subtype detection
        doc_subtype = None
        if plugin and hasattr(plugin.ingestion, 'detect_document_subtype'):
            try:
                doc_subtype = plugin.ingestion.detect_document_subtype(parsed_doc)
                if doc_subtype:
                    logger.info(f"[BUILDER] Industry package detected subtype '{doc_subtype}': {parsed_doc.filename}")
            except Exception as e:
                logger.warning(f"[BUILDER] Document subtype detection failed: {e}")

        # Phase 1: concurrent analysis
        def _analyze_single_page(idx: int):
            page = parsed_doc.pages[idx]
            preprocessed = preprocessed_pages[idx] if idx < len(preprocessed_pages) else None
            page_text = preprocessed.raw_text if preprocessed else page.raw_text

            # FIX: sanitize page text (control char filtering + watermark dedup)
            page_text = self._sanitize_page_text(page_text)
            original_page_text = page_text

            page_summary = self._generate_page_summary(page_text, page.page_num)
            entities = self._extract_entities(page_text, plugin)

            ocr_results = preprocessed.ocr_results if preprocessed else []
            layout_result = self.layout_analyzer.analyze(
                raw_text=page_text,
                page_num=page.page_num,
                ocr_results=ocr_results
            )

            # parser.py's page_class (CHART/IMAGE/TEXT/BLANK) is more accurate than
            # the legacy _classify_pdf_page page_type, so use it to override the
            # layout analyzer page_type when available.
            page_class = getattr(page, 'content_dict', {}).get('page_class')
            if page_class == 'BLANK':
                layout_result.page_type = 'blank'

            formulas = self._extract_formulas(layout_result, doc_id)

            charts = []
            if page_class != 'BLANK' and settings.CHART_CONFIG.get("enabled", True):
                try:
                    vlm_analysis = getattr(page, 'content_dict', {}).get("vlm_analysis")
                    if not (vlm_analysis and len(str(vlm_analysis).strip()) > 10):
                        page_image = self._get_page_image_for_analysis(page, preprocessed)
                        if page_image:
                            charts = self.chart_analyzer.analyze_page(
                                page_image=page_image,
                                layout_result=layout_result,
                                page_text=page_text,
                                doc_id=doc_id,
                                page_num=page.page_num
                            )
                            if charts and settings.CHART_CONFIG.get("append_to_raw_text", True):
                                enhanced_text = self.chart_analyzer.build_enhanced_text(page_text, charts)
                                page_text = enhanced_text
                except Exception as e:
                    logger.warning(f"Chart analysis failed for page {page.page_num}: {e}")

            content_json = layout_result.to_dict()
            content_json["formulas"] = formulas
            content_json["charts"] = [c.to_dict() for c in charts]

            # Pass parser's VLM classification results to content_json
            content_dict = getattr(page, 'content_dict', {})
            if content_dict.get("page_class"):
                content_json["page_class"] = content_dict["page_class"]
            if content_dict.get("vlm_analysis"):
                content_json["vlm_analysis"] = content_dict["vlm_analysis"]

            # For blank pages, clear any derived text/summary to keep downstream data clean
            if page_class == 'BLANK':
                page_text = ""
                page_summary = ""
                entities = []
                content_json["charts"] = []
                content_json["formulas"] = []

            # Industry package page processing hook
            extra_data = None
            if plugin and hasattr(plugin.ingestion, 'process_page'):
                try:
                    page._doc_subtype = doc_subtype  # Pass document subtype
                    # Provide the rendered page image so industry packages can run
                    # vision models (e.g., VLM-based schematic analysis) without
                    # re-rendering. Core remains image-format agnostic.
                    page_image = self._get_page_image_for_analysis(page, preprocessed)
                    extra = plugin.ingestion.process_page(
                        page=page,
                        raw_text=page.raw_text or original_page_text,
                        layout_result=layout_result,
                        model_client=self.model_client,
                        page_image=page_image
                    )
                    if extra:
                        extra_data = {
                            "subtype": extra.get("subtype"),
                            "data": extra.get("data")
                        }
                        if extra.get("searchable_text"):
                            page_text = f"{page_text}\n\n{extra['searchable_text']}"
                            page_summary = f"{page_summary}\n{extra['searchable_text'][:500]}" if page_summary else extra['searchable_text'][:500]
                        if extra.get("page_type_override"):
                            layout_result.page_type = extra["page_type_override"]
                except Exception as e:
                    logger.warning(f"[BUILDER] Industry package page processing failed (page {page.page_num}): {e}")

            return {
                "page_num": page.page_num,
                "section_title": page.section_title,
                "page_summary": page_summary,
                "entities": entities,
                "content_json": content_json,
                "page_text": page_text,
                "page_type": layout_result.page_type,
                "text_source": preprocessed.text_source if preprocessed else "direct_extract",
                "ocr_confidence": preprocessed.ocr_confidence if preprocessed else None,
                "page_image_path": preprocessed.page_image_path if preprocessed else None,
                "formulas": formulas,
                "charts_count": len(charts),
                "extra_data": extra_data,
            }

        max_workers = min(INGEST_MAX_WORKERS, len(parsed_doc.pages))
        if max_workers < 1:
            max_workers = 1
        page_results = {}
        if max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(_analyze_single_page, i): i for i in range(len(parsed_doc.pages))}
                for future in futures:
                    try:
                        result = future.result()
                        page_results[result["page_num"]] = result
                    except Exception as e:
                        logger.error(f"Page analysis failed: {e}", exc_info=True)
        else:
            for i in range(len(parsed_doc.pages)):
                result = _analyze_single_page(i)
                page_results[result["page_num"]] = result

        # Phase 2: serial writes
        structure_index, explicit_sections = self._build_structure_index(doc_id, page_results, parsed_doc)
        l2_results = []

        for page_num in sorted(page_results.keys()):
            r = page_results[page_num]
            page_structure = structure_index.get(page_num, {})
            structure_title = page_structure.get("title", "")
            section_title = structure_title if structure_title else r["section_title"]

            page_db_id = metadata_db.save_page(
                doc_id=doc_id,
                page_num=r["page_num"],
                section_title=section_title,
                section_level=page_structure.get("level", 0),
                section_path=page_structure.get("path", ""),
                is_table_header=page_structure.get("is_table", False),
                table_caption=page_structure.get("table_caption", ""),
                page_summary=r["page_summary"],
                entities=r["entities"],
                content_json=r["content_json"],
                raw_text=r["page_text"],
                page_type=r["page_type"],
                text_source=r["text_source"],
                ocr_confidence=r["ocr_confidence"],
                page_image_path=r["page_image_path"],
                extra_data=r.get("extra_data")
            )

            l2_results.append({
                "page_id": page_db_id,
                "page_num": r["page_num"],
                "summary": r["page_summary"],
                "page_type": r["page_type"],
                "formulas_count": len(r["formulas"]),
                "charts_count": r["charts_count"],
                "section_title": section_title,
                "page_text": r["page_text"]
            })

            # Extract assertion-level spec facts from this page's authoritative
            # text. The extractor strips VLM description blocks first and
            # self-verifies every value against the original line — fully local,
            # rule-based (no LLM, no external API). Failure never blocks ingest.
            if settings.CONTEXT_CONFIG.get("spec_facts_enabled", True):
                try:
                    from .spec_facts_extractor import extract_spec_facts_from_text
                    for fact in extract_spec_facts_from_text(r["page_text"], r["page_num"], spec_entity, doc_id):
                        metadata_db.insert_spec_fact(
                            doc_id=fact["doc_id"], entity=fact["entity"],
                            attribute=fact["attribute"], value=fact["value"],
                            page_num=fact["page_num"], source_text=fact["source_text"],
                            verified=fact["verified"])
                except Exception as e:
                    logger.warning(f"spec fact extraction failed for page {r['page_num']}: {e}")

        self._save_structure_index_to_db(doc_id, structure_index, page_results, tenant_id=tid,
                                         explicit_sections=explicit_sections)
        return l2_results

    def _build_structure_index(self, doc_id: str, page_results: dict[int, dict],
                                parsed_doc=None):
        """Build document structure index

        Returns: (structure_index, explicit_sections)
          structure_index: page_num -> page-level chapter info (for page labeling)
          explicit_sections: ordered section list with page ranges (TOC path only,
                             preserves multiple sections sharing one page), else None

        Forced LLM full analysis strategy:
        1. Prefer PDF bookmarks/TOC — if high quality
        2. Otherwise force LLM full analysis (ensuring highest quality)
        """
        # Attempt 1: PDF bookmarks/TOC
        toc = parsed_doc.metadata.get("toc", []) if parsed_doc else []
        if toc and len(toc) > 0:
            result, explicit_sections = self._build_structure_from_toc(doc_id, page_results, toc)
            if result and len(result) > 0:
                # Check TOC build quality: whether it covers most pages
                covered = sum(1 for s in result.values() if s.get("title"))
                coverage = covered / len(page_results) if page_results else 0
                if coverage >= 0.5:
                    logger.info(f"[STRUCTURE] Using PDF bookmarks to build structure index: {len(result)} pages, coverage {coverage:.1%}")
                    return result, explicit_sections
                else:
                    # FIX: Even if coverage is low, use PDF bookmarks and extrapolate to remaining pages
                    # This avoids slow LLM analysis in background tasks
                    logger.info(f"[STRUCTURE] PDF bookmark coverage {coverage:.1%}, extrapolating to remaining pages")
                    result = self._extrapolate_structure_to_all_pages(result, page_results)
                    if result:
                        logger.info(f"[STRUCTURE] Using extrapolated PDF bookmarks: {len(result)} pages")
                        return result, explicit_sections
                    logger.info("[STRUCTURE] PDF bookmark extrapolation failed, using LLM full analysis")
            else:
                logger.info("[STRUCTURE] PDF bookmarks empty or invalid, using LLM full analysis")

        # Force LLM full analysis (ensure highest quality) - DISABLED for background tasks
        # LLM analysis is too slow for background processing, use text rules instead
        logger.info(f"[STRUCTURE] LLM full analysis disabled for background tasks, using text rules: {doc_id[:8]}, {len(page_results)} pages")
        result = self._build_structure_from_text(doc_id, page_results)
        if result:
            logger.info(f"[STRUCTURE] Text rules structure complete: {len(result)} pages")
            return result, None

        # Final fallback: return empty structure index
        logger.warning("[STRUCTURE] All methods failed, returning empty structure index")
        return {}, None

    _NON_CONTENT_PATTERNS = [
        'table of content', 'contents', 'figure index', 'table index',
        'warranty disclaimer', 'declaration', 'confidential',
        'revision history', 'about this document', 'reference',
        'list of figures', 'list of tables', 'abbreviations',
    ]

    def _is_non_content_toc(self, title: str) -> bool:
        if not title:
            return True
        t_lower = title.lower().strip()
        if t_lower.startswith('_'):
            return True
        if t_lower.isdigit():
            return True
        for p in self._NON_CONTENT_PATTERNS:
            if p in t_lower:
                return True
        return False

    def _clean_section_title(self, title: str) -> str:
        """Clean section title: remove trailing page numbers, dots, extra spaces"""
        import re
        if not title:
            return ""
        # Remove trailing page numbers (e.g. "Overview  123")
        title = re.sub(r'\s+\d+\s*$', '', title)
        # Remove trailing dots
        title = re.sub(r'\.{2,}$', '', title)
        # Remove trailing whitespace
        title = title.strip()
        # Collapse multiple spaces
        title = re.sub(r'\s+', ' ', title)
        return title

    def _build_structure_from_toc(self, doc_id: str, page_results: dict[int, dict],
                                   toc: list):
        """
        Build structure index from PDF TOC/bookmarks.

        Returns: (structure_index, explicit_sections)
          structure_index: page_num -> page-level chapter info (for page labeling)
          explicit_sections: ordered section list — one entry per TOC item,
                             including multiple sections sharing the same page

        V5.0 Fixes:
        1. Same-page multi-chapter: collect ALL entries, merge into composite title
        2. Title pollution: clean trailing page numbers, dots, extra spaces
        3. End page calculation: based on next same-or-higher-level chapter
        4. Level inference: always infer from path depth (e.g. "1.2.4" -> L3)
        5. Full path construction: "1.2.4" -> "Chapter 1 > 1.2 > 1.2.4"
        """
        import re
        structure_index = {}
        max_page = max(page_results.keys()) if page_results else 0
        if not max_page:
            return structure_index, None

        valid_toc = []
        for entry in toc:
            if len(entry) < 3:
                continue
            level, title, page_num = entry
            if level < 1 or not title or not title.strip():
                continue
            if page_num < 1 or page_num > max_page:
                continue
            clean_title = self._clean_section_title(title.strip())
            if not clean_title or clean_title.isdigit():
                continue
            if self._is_non_content_toc(clean_title):
                continue
            valid_toc.append((level, clean_title, page_num))

        if not valid_toc:
            return structure_index, None

        # V5.0: Build full hierarchy map for path construction
        # path -> {level, title, page_num, parent_path}
        toc_entries = []
        path_map = {}  # path -> entry info

        for level, title, page_num in valid_toc:
            num_match = re.match(r'^(\d+(?:\.\d+)*)\s+(.+)', title)
            if num_match:
                path = num_match.group(1)
                display_title = self._clean_section_title(num_match.group(2).strip())
                # V5.0: ALWAYS infer level from path depth (e.g. "1.2.4" -> L3)
                # This is more reliable than PDF TOC metadata which often has incorrect levels
                inferred_level = len(path.split('.'))
                level = inferred_level  # Always use inferred level
            else:
                alpha_num_match = re.match(r'^([A-Z])\.(?:\d+)?\s+(.+)', title)
                if alpha_num_match:
                    path = alpha_num_match.group(1)
                    display_title = self._clean_section_title(alpha_num_match.group(2).strip())
                else:
                    path = title
                    display_title = title

            # V5.0: Build parent path for full hierarchy
            parent_path = ""
            if '.' in path:
                parent_path = '.'.join(path.split('.')[:-1])

            entry = {
                "page_num": page_num,
                "level": level,
                "path": path,
                "title": display_title,
                "raw_title": title,
                "parent_path": parent_path
            }
            toc_entries.append(entry)
            path_map[path] = entry

        # V5.0: Build full display path (e.g. "1.2.4" -> "Chapter 1 > 1.2 > 1.2.4")
        def build_full_path(entry):
            """Build full hierarchical path for display"""
            path = entry["path"]
            parts = []
            current = entry
            while current:
                parts.insert(0, f"{current['path']} {current['title']}")
                parent_path = current.get("parent_path", "")
                if parent_path and parent_path in path_map:
                    current = path_map[parent_path]
                else:
                    break
            return " > ".join(parts) if len(parts) > 1 else f"{path} {entry['title']}"

        # FIX: collect all chapter entries per page (fix same-page multi-chapter overwrite)
        page_entries = {}  # page_num -> list of entries
        for entry in toc_entries:
            pn = entry["page_num"]
            if pn not in page_entries:
                page_entries[pn] = []
            page_entries[pn].append(entry)

        # V5.0: Assign chapter info to each page with proper hierarchy
        for page_num in sorted(page_results.keys()):
            # Find the most specific (deepest) chapter entry for this page
            best_entry = None
            for entry in toc_entries:
                if entry["page_num"] <= page_num:
                    best_entry = entry
                else:
                    break

            if best_entry:
                # V5.0: Use full path for better identification
                full_path = build_full_path(best_entry)
                structure_index[page_num] = {
                    "level": best_entry["level"],
                    "path": full_path,  # Use full hierarchical path
                    "short_path": best_entry["path"],  # Keep short path for reference
                    "title": best_entry["title"],
                    "is_table": False,
                    "table_caption": ""
                }
            else:
                structure_index[page_num] = {"level": 0, "path": "", "title": "",
                                              "is_table": False, "table_caption": ""}

        # V5.0: Calculate end_page using merged section logic
        # Group consecutive pages with same short_path into sections
        section_ranges = []  # [(short_path, start_page, end_page, level, title)]
        sorted_pages = sorted(structure_index.keys())

        current_path = None
        current_start = None
        current_end = None
        current_level = 0
        current_title = ""

        for page_num in sorted_pages:
            s = structure_index[page_num]
            path = s.get("short_path", "")
            if not path:
                continue

            if path == current_path:
                # Same path, extend end_page
                current_end = page_num
            else:
                # Different path, save previous and start new
                if current_path:
                    section_ranges.append((current_path, current_start, current_end, current_level, current_title))
                current_path = path
                current_start = page_num
                current_end = page_num
                current_level = s.get("level", 0)
                current_title = s.get("title", "")

        # Save last section
        if current_path:
            section_ranges.append((current_path, current_start, current_end, current_level, current_title))

        # Apply end_page to all pages in each section
        for short_path, start_page, end_page, level, title in section_ranges:
            for pn in range(start_page, end_page + 1):
                if pn in structure_index:
                    structure_index[pn]["end_page"] = end_page
                    structure_index[pn]["section_start"] = start_page
                    structure_index[pn]["section_end"] = end_page

        # FIX: Build explicit section list from the ordered TOC entries.
        # The page-keyed structure_index above can only hold ONE section per page,
        # so same-page sections (datasheet style: 1.2.4 / 1.2.5 both on p12) were
        # silently dropped from the DB. Sections are derived directly from the
        # entry sequence instead:
        #   start_page = entry's own page
        #   end_page   = page of the next entry at the same or higher level
        #                (allows 1-page overlap so content flowing onto the next
        #                page — e.g. NPU bullets at the top of p13 — stays covered)
        explicit_sections = []
        for i, entry in enumerate(toc_entries):
            lvl = entry["level"]
            start = entry["page_num"]
            end = max_page
            for nxt in toc_entries[i + 1:]:
                if nxt["level"] <= lvl:
                    nxt_page = nxt["page_num"]
                    end = nxt_page if nxt_page > start else start
                    break
            end = max(start, min(end, max_page))
            explicit_sections.append({
                "short_path": entry["path"],
                "full_path": build_full_path(entry),
                "title": entry["title"],
                "level": lvl,
                "start_page": start,
                "end_page": end,
            })

        return structure_index, explicit_sections

    def _extrapolate_structure_to_all_pages(self, structure_index: dict[int, dict],
                                             page_results: dict[int, dict]) -> dict[int, dict]:
        """Extrapolate structure from bookmark-covered pages to all pages.

        For pages without explicit bookmarks, use the nearest preceding bookmark's structure.
        """
        if not structure_index or not page_results:
            return structure_index

        max_page = max(page_results.keys())

        # Sort pages that have structure
        structured_pages = sorted(structure_index.keys())
        if not structured_pages:
            return structure_index

        # For each page without structure, find nearest preceding structured page
        for page_num in range(1, max_page + 1):
            if page_num not in structure_index:
                # Find nearest preceding page with structure
                prev_structured = None
                for sp in structured_pages:
                    if sp < page_num:
                        prev_structured = sp
                    else:
                        break

                if prev_structured:
                    structure_index[page_num] = {
                        "level": structure_index[prev_structured]["level"],
                        "path": structure_index[prev_structured]["path"],
                        "title": structure_index[prev_structured]["title"],
                        "is_table": False,
                        "table_caption": ""
                    }
                else:
                    # No preceding structure, use first available
                    first_page = structured_pages[0]
                    structure_index[page_num] = {
                        "level": structure_index[first_page]["level"],
                        "path": structure_index[first_page]["path"],
                        "title": structure_index[first_page]["title"],
                        "is_table": False,
                        "table_caption": ""
                    }

        return structure_index

    def _build_structure_from_text(self, doc_id: str, page_results: dict[int, dict]) -> dict[int, dict]:
        import re
        structure_index = {}
        current_path = ""
        current_level = 0
        section_start_pages = {}
        # FIX: record chapter titles for inferring missing parent chapters
        chapter_titles = {}  # chapter_num -> title
        # Extract chapter titles from TOC pages (if any)
        self._extract_chapter_titles_from_toc(page_results, chapter_titles)

        for page_num in sorted(page_results.keys()):
            r = page_results[page_num]
            text = r.get("page_text", "")
            lines = text.split("\n")[:30]

            page_structure = {"level": current_level, "path": current_path,
                              "is_table": False, "table_caption": ""}

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                heading_match = re.match(r'^(#{1,6})\s+(.+)', line)
                if heading_match:
                    level = len(heading_match.group(1))
                    title = heading_match.group(2).strip()
                    if level == 1:
                        current_path = title
                    elif level == 2:
                        current_path = f"{current_path.split('.')[0] if '.' in current_path else current_path}.{title}"
                    else:
                        current_path = f"{current_path}.{title}"
                    current_level = level
                    section_start_pages[current_path] = page_num
                    page_structure.update({"level": level, "path": current_path, "title": title})
                    break

                cn_num_map = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                              '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
                cn_heading_match = re.match(r'^第([一二三四五六七八九十]+)(编|章|节|部|篇)\s+(.+)', line)
                if cn_heading_match:
                    cn_num_str = cn_heading_match.group(1)
                    cn_unit = cn_heading_match.group(2)
                    title = cn_heading_match.group(3).strip()
                    cn_num = cn_num_map.get(cn_num_str, 0)
                    level_map = {'编': 1, '部': 1, '篇': 1, '章': 2, '节': 3}
                    level = level_map.get(cn_unit, 2)
                    path = f"{cn_num}.{cn_unit}.{title}"
                    current_path = path
                    current_level = level
                    section_start_pages[path] = page_num
                    page_structure.update({"level": level, "path": path, "title": title})
                    break

                num_heading_match = re.match(r'^(\d+(?:\.\d+)*)\s+(.+)', line)
                if num_heading_match and len(num_heading_match.group(1)) <= 8:
                    path = num_heading_match.group(1)
                    title = num_heading_match.group(2).strip()
                    level = path.count(".") + 1

                    # FIX: detect new top-level chapter number, infer parent chapter
                    top_num = path.split('.')[0]
                    if top_num.isdigit() and int(top_num) > 0:
                        chapter_num = int(top_num)
                        # If this is a new top-level number with no corresponding Chapter title, create virtual parent chapter
                        if chapter_num not in chapter_titles:
                            # Try to get title from TOC pages
                            parent_title = self._get_chapter_title_from_toc(page_results, chapter_num)
                            if parent_title:
                                chapter_titles[chapter_num] = parent_title
                            else:
                                chapter_titles[chapter_num] = f"Chapter {chapter_num}"

                    current_path = path
                    current_level = level
                    section_start_pages[path] = page_num
                    page_structure.update({"level": level, "path": current_path, "title": title})
                    break

                # FIX: add English chapter heading matching (e.g. "Chapter 1 Introduction")
                chapter_heading_match = re.match(r'^(Chapter|Section|Part)\s+(\d+|[A-Z])\s+(.+)', line, re.IGNORECASE)
                if chapter_heading_match:
                    unit = chapter_heading_match.group(1).capitalize()
                    num = chapter_heading_match.group(2)
                    title = chapter_heading_match.group(3).strip()
                    level = 1 if unit == "Chapter" else 2 if unit == "Section" else 1
                    path = f"{unit} {num}"
                    current_path = path
                    current_level = level
                    section_start_pages[path] = page_num
                    # FIX: record Chapter title
                    if unit == "Chapter" and num.isdigit():
                        chapter_titles[int(num)] = title
                    page_structure.update({"level": level, "path": current_path, "title": title})
                    break

                table_match = re.search(r'[\[【（](?:表格|Table|表)\s*\d+[\]】）]', line)
                if table_match:
                    page_structure["is_table"] = True
                    caption_match = re.search(r'[\[【（](?:表格|Table|表)\s*\d+[\]】）]\s*(.+)', line)
                    if caption_match:
                        page_structure["table_caption"] = caption_match.group(1).strip()

            structure_index[page_num] = page_structure

        # FIX: fill parent chapter info for pages without titles
        self._fill_parent_chapters(structure_index, chapter_titles, page_results)

        sorted_pages = sorted(structure_index.keys())
        for i, page_num in enumerate(sorted_pages):
            path = structure_index[page_num].get("path", "")
            if path in section_start_pages:
                end_page = page_num
                for next_page in sorted_pages[i+1:]:
                    next_path = structure_index[next_page].get("path", "")
                    if next_path != path and next_path.startswith(path + "."):
                        continue
                    if next_path != path:
                        end_page = next_page - 1
                        break
                else:
                    end_page = max(sorted_pages)
                structure_index[page_num]["end_page"] = end_page

        return structure_index

    def _extract_chapter_titles_from_toc(self, page_results: dict[int, dict], chapter_titles: dict):
        """Extract chapter titles from TOC pages"""
        import re
        for page_num, r in page_results.items():
            text = r.get("page_text", "")
            if not text:
                continue
            lines = text.split("\n")
            for line in lines:
                line = line.strip()
                # FIX: match "Chapter X Title" format (TOC page)
                match = re.match(r'^(Chapter|Section|Part)\s+(\d+)\s+(.+)', line, re.IGNORECASE)
                if match:
                    num = int(match.group(2))
                    title = match.group(3).strip()
                    # Clean page numbers and dots from titles
                    title = re.sub(r'\s+\d+$', '', title)  # Remove trailing numbers
                    title = re.sub(r'\.{2,}$', '', title)  # Remove trailing dots
                    title = title.strip()
                    if title and not title.startswith('.'):
                        chapter_titles[num] = title

    def _get_chapter_title_from_toc(self, page_results: dict[int, dict], chapter_num: int) -> str:
        """Find the title of a specific chapter from TOC pages"""
        import re
        for page_num, r in page_results.items():
            text = r.get("page_text", "")
            if not text:
                continue
            lines = text.split("\n")
            for line in lines:
                line = line.strip()
                match = re.match(rf'^Chapter\s+{chapter_num}\s+(.+?)\s+\.', line, re.IGNORECASE)
                if match:
                    return match.group(1).strip()
        return ""

    def _fill_parent_chapters(self, structure_index: dict[int, dict], chapter_titles: dict, page_results: dict[int, dict]):
        """Fill parent chapter info for pages without titles"""
        import re
        current_chapter = None
        for page_num in sorted(structure_index.keys()):
            s = structure_index[page_num]
            if s.get("title"):
                # Check if it's a Chapter title
                path = s.get("path", "")
                if path.startswith("Chapter "):
                    current_chapter = path
                elif re.match(r'^\d+\.\d+', path):
                    # Numbered heading, infer parent chapter
                    top_num = path.split('.')[0]
                    if top_num.isdigit():
                        chapter_num = int(top_num)
                        if chapter_num in chapter_titles:
                            current_chapter = f"Chapter {chapter_num} {chapter_titles[chapter_num]}"
                continue

            # If current page has no title but has a parent chapter, fill parent chapter info
            if current_chapter and not s.get("title"):
                s["path"] = current_chapter
                s["title"] = current_chapter
                s["level"] = 1

    def _llm_analyze_structure(self, doc_id: str, page_results: dict[int, dict]) -> dict[int, dict]:
        """
        Use LLM to fully analyze the document and extract chapter structure.
        Called when both PDF bookmarks and text rules fail.

        Strategy:
        1. Collect all page text, labeled by page number
        2. If total chars < ~130K (128K context limit), analyze in one shot
        3. If exceeds limit, analyze in chunks (each ≤ ~130K chars)
        4. LLM returns chapter structure in JSON format
        """

        # Collect all page texts
        page_texts = []
        total_chars = 0
        for page_num in sorted(page_results.keys()):
            text = page_results[page_num].get("page_text", "")
            if text:
                # Limit to 4000 chars per page (avoid overly long single pages, while keeping enough context for multi-section detection)
                truncated = text[:4000]
                page_texts.append(f"--- Page {page_num} ---\n{truncated}")
                total_chars += len(truncated) + 20  # Add marker overhead

        if not page_texts:
            logger.warning(f"[LLM_STRUCTURE] No page text to analyze: {doc_id}")
            return {}

        # Build prompt
        full_content = "\n\n".join(page_texts)

        # If content is too long, analyze in chunks
        # Derive safe limit from CONTEXT_CONFIG to avoid triggering safety guard truncation
        llm_max = settings.CONTEXT_CONFIG.get("llm_max_tokens", 65536)
        ratio = settings.CONTEXT_CONFIG.get("token_to_char_ratio", 0.7)
        MAX_PROMPT_CHARS = int(llm_max * ratio * 0.85)  # 85% of safety limit
        if len(full_content) > MAX_PROMPT_CHARS:
            logger.info(f"[LLM_STRUCTURE] Document too long ({len(full_content)} chars), analyzing in chunks")
            return self._llm_analyze_structure_chunked(doc_id, page_results, page_texts)

        system_prompt = """You are a document structure analysis expert. Analyze the following document and extract ALL chapter/section structures.

Critical requirements:

1. Identify ALL levels of structure:
   - Level 1: Major chapters (e.g., "Chapter 1 Introduction", "Chapter 2 Overview")
   - Level 2: Sections (e.g., "1.1 Overview", "2.3 Specifications")
   - Level 3: Subsections (e.g., "1.2.1 Core Features", "3.4.1 Performance Metrics")
   - Level 4: Detailed subsections if present

2. Look for these patterns in page headers and content:
   - "Chapter X Title" format
   - "X.Y Title" or "X.Y.Z Title" format
   - Section headers in bold or prominent text

3. For pages that are pure tables or continuation pages without explicit headers:
   - Infer a descriptive title from the table content
   - Examples: "Specifications Table", "Parameter Comparison Table", "Pin Assignment Table"
   - Assign them as Level 3 under the nearest parent chapter

4. Skip non-content pages (cover, revision history, table of contents, index, disclaimer)

5. Return JSON array format:
[
  {"title": "Chapter 1 Introduction", "level": 1, "start_page": 8},
  {"title": "1.1 Overview", "level": 2, "start_page": 8},
  {"title": "1.2 Features", "level": 2, "start_page": 9},
  {"title": "1.2.1 Core Features", "level": 3, "start_page": 12},
  {"title": "Parameter Comparison Table", "level": 3, "start_page": 21},
  ...
]

6. CRITICAL: Every content page must be covered by at least one section. Do not leave any page unassigned.
7. ABSOLUTELY CRITICAL — Section numbering rule:
   - If the source document HAS explicit section numbers (like "1.2", "3.4.1"), use them exactly as-is.
   - If the source document has NO section numbers (e.g., short product briefs with just headings like "Features", "Block Diagram"), do NOT invent any numbers. Use plain descriptive names at appropriate levels.
   - Wrong: "2.1 Audio Processor" (no such number in source)
   - Correct: "Audio Processor" (descriptive name only)
8. Return ONLY valid JSON array, no other text."""

        try:
            logger.info(f"[LLM_STRUCTURE] Sending full analysis request: {len(full_content)} chars")
            result = self.model_client.generate_json_array(
                prompt=full_content,
                system_prompt=system_prompt,
                max_tokens=4096,
                temperature=0.1
            )

            if not result:
                logger.warning("[LLM_STRUCTURE] LLM returned empty result")
                return {}

            return self._parse_llm_structure_result(result, page_results)

        except Exception as e:
            logger.error(f"[LLM_STRUCTURE] LLM analysis failed: {e}")
            return {}

    def _llm_analyze_structure_chunked(self, doc_id: str, page_results: dict[int, dict],
                                        page_texts: list[str]) -> dict[int, dict]:
        """Chunked LLM analysis (oversized documents)"""

        # Derive safe limit from CONTEXT_CONFIG to avoid triggering safety guard truncation
        llm_max = settings.CONTEXT_CONFIG.get("llm_max_tokens", 65536)
        ratio = settings.CONTEXT_CONFIG.get("token_to_char_ratio", 0.7)
        MAX_PROMPT_CHARS = int(llm_max * ratio * 0.85)  # 85% of safety limit
        chunks = []
        current_chunk = []
        current_len = 0

        for pt in page_texts:
            if current_len + len(pt) > MAX_PROMPT_CHARS and current_chunk:
                chunks.append("\n\n".join(current_chunk))
                current_chunk = [pt]
                current_len = len(pt)
            else:
                current_chunk.append(pt)
                current_len += len(pt) + 2

        if current_chunk:
            chunks.append("\n\n".join(current_chunk))

        logger.info(f"[LLM_STRUCTURE] Analyzing in {len(chunks)} chunks")

        all_sections = []
        for i, chunk in enumerate(chunks):
            system_prompt = f"""You are a document structure analysis expert. Analyze the following document content (segment {i+1}/{len(chunks)}), extract all chapter/section structures.

Requirements:
1. Identify ALL chapter/section/subsection titles and their hierarchy
2. Look for: "Chapter X Title", "X.Y Title", "X.Y.Z Title" patterns
3. For table pages without explicit headers, infer descriptive titles from content
4. Return JSON array format:
[
  {{"title": "Chapter Title", "level": 1, "start_page": 1}},
  {{"title": "1.1 Section", "level": 2, "start_page": 3}},
  ...
]
5. CRITICAL: Every content page must be covered. Do not leave any page unassigned.
6. Do NOT invent fictional section numbers for items in feature tables or spec sheets. Only number explicitly numbered sections.
7. If the document has NO section numbers at all, use descriptive names without any numbers.
8. Return ONLY JSON, no other text"""

            try:
                result = self.model_client.generate_json_array(
                    prompt=chunk,
                    system_prompt=system_prompt,
                    max_tokens=4096,
                    temperature=0.1
                )
                if result:
                    all_sections.extend(result)
            except Exception as e:
                logger.error(f"[LLM_STRUCTURE] Chunk {i+1} analysis failed: {e}")

        if not all_sections:
            return {}

        # Merge duplicate chapters (cross-chunk analysis may cause duplicates)
        merged = self._merge_chunked_sections(all_sections)
        return self._parse_llm_structure_result(merged, page_results)

    def _merge_chunked_sections(self, sections: list[dict]) -> list[dict]:
        """Merge chunked analysis results, deduplicate"""
        seen = set()
        merged = []
        for s in sections:
            title = s.get("title", "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            merged.append(s)
        return merged

    def _parse_llm_structure_result(self, sections: list[dict],
                                     page_results: dict[int, dict]) -> dict[int, dict]:
        """Parse LLM-returned chapter structure into structure_index format

FIX: Correctly handle hierarchy, assign the most appropriate chapter to each page.
        Strategy: assign each page to the deepest-level section that contains it (leaf node).
        """
        structure_index = {}
        max_page = max(page_results.keys()) if page_results else 0

        if not sections:
            return structure_index

        # Sort by start_page
        sorted_sections = sorted(sections, key=lambda s: s.get("start_page", 1))

        # Calculate end_page for each chapter (based on next same-or-higher-level chapter's start_page)
        for i, section in enumerate(sorted_sections):
            if "end_page" not in section:
                # Find next same-or-higher-level chapter
                next_start = max_page
                current_level = section.get("level", 1)
                for next_section in sorted_sections[i+1:]:
                    if next_section.get("level", 1) <= current_level:
                        next_start = next_section.get("start_page", max_page) - 1
                        break
                section["end_page"] = next_start

        # Assign each page the deepest-level section (leaf node)
        for page_num in sorted(page_results.keys()):
            # Find all sections that contain this page
            matching_sections = []
            for section in sorted_sections:
                start_page = section.get("start_page", 1)
                end_page = section.get("end_page", max_page)
                if start_page <= page_num <= end_page:
                    matching_sections.append(section)

            if matching_sections:
                # Choose deepest-level section (most specific)
                best_level = max(s.get("level", 1) for s in matching_sections)
                deepest = [s for s in matching_sections if s.get("level", 1) == best_level]

                # Tiebreaker: when multiple sections at same deepest level match,
                # prefer the one whose title text actually appears on this page
                best_section = deepest[0]
                if len(deepest) > 1:
                    page_text = page_results.get(page_num, {}).get("page_text", "")
                    if page_text:
                        for s in deepest:
                            stitle = s.get("title", "")
                            if stitle and len(stitle) > 2 and stitle in page_text:
                                best_section = s
                                break

                title = best_section.get("title", "")
                level = best_section.get("level", 1)

                # Strip hallucinated section numbers: if title starts with a number
                # pattern (e.g. "2.11 Analog Interfaces") but that number doesn't
                # appear verbatim in the page text, remove the number prefix.
                import re as _re
                _num_match = _re.match(r'^(\d+(?:\.\d+)+)\s+(.+)', title)
                if _num_match:
                    _num_prefix = _num_match.group(1)
                    _clean_name = _num_match.group(2)
                    page_text = page_results.get(page_num, {}).get("page_text", "")
                    if page_text and _num_prefix not in page_text:
                        title = _clean_name

                # path includes level info for downstream processing
                path = f"{level}.{title}"
                structure_index[page_num] = {
                    "level": level,
                    "path": path,
                    "title": title,
                    "is_table": False,
                    "table_caption": ""
                }
            else:
                structure_index[page_num] = {
                    "level": 0, "path": "", "title": "",
                    "is_table": False, "table_caption": ""
                }

        # FIX: Multi-section page detection - when a section title appears
        # in the latter half of a page, reassign to the section that starts
        # earliest on that page and covers most of the content.
        import re
        for page_num in sorted(page_results.keys()):
            if page_num not in structure_index:
                continue
            current = structure_index[page_num]
            title = current.get("title", "")
            if not title or len(title) < 2:
                continue

            page_text = page_results[page_num].get("page_text", "")
            if not page_text:
                continue

            title_pos = page_text.find(title)
            page_len = len(page_text)
            if title_pos < 0 or page_len < 100:
                continue

            # Title in latter 40% of page -> most content belongs to earlier section
            if title_pos > page_len * 0.4:
                all_matching = []
                for section in sorted_sections:
                    s_title = section.get("title", "")
                    if not s_title or len(s_title) < 2:
                        continue
                    s_start = section.get("start_page", 1)
                    s_end = section.get("end_page", max_page)
                    if s_start <= page_num <= s_end:
                        pos = page_text.find(s_title)
                        if pos >= 0:
                            all_matching.append((pos, section))

                # NEW: Extract numbered section headers directly from page text.
                # This catches subsections the LLM missed (e.g., "1.2.4 AOV", "1.2.5 AOA"
                # on a page that LLM only tagged as "1.2.6 Video CODEC").
                raw_section_headers = []
                _section_re = re.compile(r'(\d+(?:\.\d+)+)\s+([A-Z][^\n]{2,60})')
                for m in _section_re.finditer(page_text):
                    num_part = m.group(1)
                    raw_title = f"{num_part} {m.group(2).strip()}"
                    raw_section_headers.append((m.start(), raw_title, num_part))

                # Determine if the first raw section header on this page does NOT
                # match the assigned LLM title → evidence that prior subsections
                # were merged onto this page without LLM awareness.
                first_raw_mismatch = False
                if raw_section_headers and all_matching:
                    first_raw_num = raw_section_headers[0][2]
                    assigned_num_match = re.search(r'(\d+(?:\.\d+)+)', title)
                    if assigned_num_match:
                        assigned_num = assigned_num_match.group(1)
                        if first_raw_num != assigned_num:
                            first_raw_mismatch = True
                            logger.info(
                                f"[STRUCTURE] Raw-section mismatch on P{page_num}: "
                                f"first_raw='{first_raw_num}', assigned='{assigned_num}', "
                                f"raw_headers={[h[1] for h in raw_section_headers]}"
                            )

                should_reassign = False
                if all_matching:
                    all_matching.sort(key=lambda x: x[0])
                    # Check if the page has multiple distinct child sections
                    distinct_sections = {}
                    for pos, sec in all_matching:
                        sec_title = sec.get("title", "")
                        if sec_title not in distinct_sections:
                            distinct_sections[sec_title] = sec

                    if len(distinct_sections) >= 2 or first_raw_mismatch:
                        should_reassign = True

                # Edge case: LLM listed zero matched sections but raw headers show subsections
                if not all_matching and len(raw_section_headers) >= 2:
                    should_reassign = True
                    logger.info(
                        f"[STRUCTURE] Raw-only sections on P{page_num}: "
                        f"headers={[h[1] for h in raw_section_headers]}, "
                        f"title='{title}' at pos {title_pos}/{page_len}"
                    )

                if should_reassign:
                    # Reassign to the closest parent section
                    current_level = current.get("level", 0)
                    parent_section = None
                    for section in sorted_sections:
                        s_start = section.get("start_page", 1)
                        s_end = section.get("end_page", max_page)
                        s_level = section.get("level", 1)
                        if (s_start <= page_num <= s_end and
                            s_level < current_level and s_level >= 1):
                            if parent_section is None or s_level > parent_section.get("level", 0):
                                parent_section = section
                    if parent_section:
                        new_title = parent_section.get("title", "")
                        new_level = parent_section.get("level", 1)
                        new_path = f"{new_level}.{new_title}"
                    elif all_matching:
                        # Fallback: use earliest matching section
                        earliest_section = all_matching[0][1]
                        new_title = earliest_section.get("title", "")
                        new_level = earliest_section.get("level", 1)
                        new_path = f"{new_level}.{new_title}"
                    else:
                        # Last resort: keep original
                        new_title = title
                        new_level = current.get("level", 0)
                        new_path = current.get("path", "")

                    logger.info(
                        f"[STRUCTURE] Multi-section reassign P{page_num}: "
                        f"'{title}' -> '{new_title}' "
                        f"(raw_headers={len(raw_section_headers)}, pos={title_pos}/{page_len})"
                    )
                    structure_index[page_num]["title"] = new_title
                    structure_index[page_num]["level"] = new_level
                    structure_index[page_num]["path"] = new_path

        # Calculate end_page (for saving to database)
        for i, section in enumerate(sorted_sections):
            start_page = section.get("start_page", 1)
            end_page = section.get("end_page", max_page)
            for pn in range(start_page, end_page + 1):
                if pn in structure_index:
                    structure_index[pn]["end_page"] = end_page

        return structure_index

    def _save_structure_index_to_db(self, doc_id: str, structure_index: dict[int, dict],
                                     page_results: dict[int, dict] | None = None,
                                     tenant_id: str = None,
                                     explicit_sections: list[dict] | None = None):
        """
        Save structure index to database.
        V5.0: Merge adjacent pages with same short_path to fix 1-page range issue.
        FIX: When explicit_sections is provided (TOC path), save it directly —
        the page-keyed grouping below cannot represent multiple sections that
        start on the same page (e.g. 1.2.4 / 1.2.5 both on p12).
        """
        tid = tenant_id or self.tenant_id or "default"
        from core.db.tenant_db import get_tenant_metadata_db
        metadata_db = get_tenant_metadata_db(tid)

        # V5.0: Merge adjacent pages with same short_path
        # Step 1: Group consecutive pages by short_path
        if explicit_sections:
            merged_sections = [
                (s["short_path"], s["start_page"], s["end_page"],
                 s["level"], s["title"], s["full_path"])
                for s in explicit_sections
            ]
        else:
            merged_sections = []  # [(short_path, start_page, end_page, level, title, full_path)]
            sorted_pages = sorted(structure_index.keys())

            current_short_path = None
            current_start = None
            current_end = None
            current_level = 0
            current_title = ""
            current_full_path = ""

            for page_num in sorted_pages:
                s = structure_index[page_num]
                short_path = s.get("short_path", "")
                if not short_path:
                    continue

                if short_path == current_short_path:
                    # Same path, extend end_page
                    current_end = page_num
                else:
                    # Different path, save previous and start new
                    if current_short_path:
                        merged_sections.append((current_short_path, current_start, current_end, current_level, current_title, current_full_path))
                    current_short_path = short_path
                    current_start = page_num
                    current_end = page_num
                    current_level = s.get("level", 0)
                    current_title = s.get("title", "")
                    current_full_path = s.get("path", "")

            # Save last section
            if current_short_path:
                merged_sections.append((current_short_path, current_start, current_end, current_level, current_title, current_full_path))

        # Step 2: Collect page text for each merged section
        # V5.0 FIX: For the first page of each section, detect and remove previous section's tail
        path_pages = {}
        if explicit_sections and page_results:
            # Section-centric text collection: every section gathers text across
            # its own page range (siblings sharing a page each get the text from
            # their own title position onward).
            for s in explicit_sections:
                texts = []
                for pn in range(s["start_page"], s["end_page"] + 1):
                    r = page_results.get(pn)
                    if not r:
                        continue
                    text = r.get("page_text", "")
                    if not text:
                        continue
                    if pn == s["start_page"]:
                        title_pos = self._find_section_title_in_text(text, s["title"])
                        if title_pos > 0:
                            text = text[title_pos:]
                    texts.append((pn, text))
                path_pages[s["short_path"]] = texts
        elif page_results:
            # Sort pages by page_num
            sorted_pages = sorted(page_results.items(), key=lambda x: x[0])

            for i, (page_num, r) in enumerate(sorted_pages):
                s = structure_index.get(page_num, {})
                short_path = s.get("short_path", "")
                if not short_path:
                    continue

                text = r.get("page_text", "")
                if not text:
                    continue

                # V5.0 FIX: Check if this page contains previous section's tail
                # This happens when a new chapter starts mid-page
                if i > 0:
                    prev_page_num = sorted_pages[i-1][0]
                    prev_s = structure_index.get(prev_page_num, {})
                    prev_short_path = prev_s.get("short_path", "")

                    # If current page has a different short_path than previous page,
                    # check if current page contains previous section's content
                    if prev_short_path and prev_short_path != short_path:
                        # Look for current section title in the page text
                        current_title = s.get("title", "")
                        if current_title:
                            # Try to find the section title in the page text
                            # If found, only use content after the title
                            title_pos = self._find_section_title_in_text(text, current_title)
                            if title_pos > 0:
                                # Found title mid-page, use only content after title
                                text = text[title_pos:]
                                logger.debug(f"[STRUCTURE] Truncated page {page_num} for {short_path} at position {title_pos}")

                if short_path not in path_pages:
                    path_pages[short_path] = []
                path_pages[short_path].append((page_num, text))

        # Step 3: Save merged sections
        for short_path, start_page, end_page, level, title, full_path in merged_sections:
            if not short_path:
                continue

            # V5.0: section_type determined by level and generic patterns, not hardcoded language-specific keywords
            # "chapter" for L1, "section" for L2+, no language-specific hardcoding
            section_type = "chapter" if level == 1 else "section"

            parent_path = ".".join(short_path.split(".")[:-1]) if "." in short_path else ""
            keywords = self._extract_keywords_from_title(title)

            # V5.0: Generate summary from all pages in this section
            summary = title
            entities = ""
            try:
                pages_text = path_pages.get(short_path, [])
                if pages_text:
                    # Sort by page_num and join text
                    full_text = "\n".join([t for _, t in sorted(pages_text, key=lambda x: x[0])])
                    # Harvest identifier inventory from the FULL section text
                    # (before truncation) so instance-level queries (e.g. UART0)
                    # can match this chapter in the structure index.
                    if SECTION_ENTITY_HARVEST_ENABLED:
                        try:
                            from .entity_harvest import harvest_acronyms, harvest_section_entities
                            entities = harvest_section_entities(full_text)
                            # Also harvest acronym/alias pairs (e.g. NPU=Neural Process Unit)
                            # to make FTS searches by abbreviation match the full term.
                            acronyms = harvest_acronyms(self.model_client, full_text, title)
                            if acronyms:
                                entities = (entities + ", " + acronyms) if entities else acronyms
                        except Exception as ee:
                            logger.debug(f"[STRUCTURE] entity harvest failed {short_path}: {ee}")
                    # V5.0: Ensure minimum content length for LLM summary
                    if len(full_text) < 50:
                        summary = f"{title}: {full_text[:200]}"
                    else:
                        max_chars = 4000
                        if len(full_text) > max_chars:
                            full_text = full_text[:max_chars] + "..."
                        summary = self._generate_section_summary(title, full_text)
            except Exception as e:
                logger.warning(f"[STRUCTURE] Failed to generate section summary {short_path}: {e}")

            try:
                # V5.0: Use full_path as section_path for better identification
                metadata_db.save_structure_index(
                    doc_id=doc_id, section_path=full_path, section_title=title,
                    section_level=level, start_page=start_page,
                    end_page=end_page, section_type=section_type,
                    parent_path=parent_path, keywords=keywords, summary=summary,
                    entities=entities
                )
            except Exception as e:
                logger.warning(f"Failed to save structure index {doc_id} {short_path}: {e}")

    def _generate_section_summary(self, title: str, content: str) -> str:
        """
        Generate section summary using LLM for quality, fallback to text preview.
        V5.0: Use LLM to generate meaningful summary instead of raw text truncation.
        """
        if not content or len(content) < 50:
            return title

        # Limit content to avoid overly long prompt
        max_content = 2000
        if len(content) > max_content:
            content = content[:max_content] + "..."

        prompt = f"""请为以下技术文档章节生成一个简洁摘要（50-100字），说明该章节的核心内容：

章节标题: {title}

章节内容:
{content}

请用中文回答，只输出摘要内容，不要其他解释。"""

        try:
            summary = self.model_client.generate(prompt, max_tokens=256, temperature=0.3)
            if summary and len(summary.strip()) > 10:
                return summary.strip()
        except Exception as e:
            logger.warning(f"[STRUCTURE] LLM summary generation failed for '{title}': {e}")

        # Fallback: text preview
        preview = content[:500].replace("\n", " ")
        if len(content) <= 500:
            return f"{title}: {preview}"
        end_preview = content[-300:].replace("\n", " ") if len(content) > 1000 else ""
        return f"{title} | Begin: {preview[:200]}... | End: {end_preview[:200]}..."

    def _generate_document_summary(self, doc_id: str, parsed_doc: Any,
                                     l2_results: list[dict]) -> str:
        """
        V5.0: Generate document-level summary for L2 retrieval.
        Uses LLM to summarize key chapters and their content.
        l2_results is a list of page result dicts, each with 'page_num' key.
        """
        # Build page_num -> result mapping
        page_map = {r.get("page_num", i): r for i, r in enumerate(l2_results)}

        # V5.0: Collect chapter info from structure_index (stored in page_results during _build_l2)
        # Since l2_results doesn't have chapter_info, we need to get it from the structure_index
        # which is built before l2_results. We'll use a fallback approach.

        # V5.0: Get structure info from the first page of each section
        # First, collect all unique sections from page_map
        sections = {}  # section_title -> {page_nums, text_preview}
        for page_num in sorted(page_map.keys()):
            r = page_map[page_num]
            # Get section title from page_summary or page_text
            section_title = r.get("section_title", "")
            if not section_title:
                # Try to extract from page_text first line
                text = r.get("page_text", "")
                if text:
                    first_line = text.split('\n')[0].strip()
                    if first_line and len(first_line) < 100:
                        section_title = first_line

            if section_title:
                if section_title not in sections:
                    sections[section_title] = {"pages": [], "texts": []}
                sections[section_title]["pages"].append(page_num)
                text = r.get("page_text", "")
                if text:
                    sections[section_title]["texts"].append(text[:300])

        # Build chapter summary text
        chapter_lines = []
        for section_title, info in list(sections.items())[:10]:  # Limit to 10 sections
            pages = info["pages"]
            if pages:
                page_range = f"p.{pages[0]}" if len(pages) == 1 else f"p.{pages[0]}-{pages[-1]}"
                chapter_lines.append(f"  {section_title} ({page_range})")

        if not chapter_lines:
            # Fallback: use first 5 pages text
            texts = []
            for page_num in sorted(page_map.keys())[:5]:
                text = page_map.get(page_num, {}).get("page_text", "")
                if text:
                    texts.append(text[:500])
            combined = "\n".join(texts)
            if len(combined) > 2000:
                combined = combined[:2000] + "..."
            return f"Document: {parsed_doc.filename}\n\nContent preview:\n{combined}"

        chapter_text = "\n".join(chapter_lines)

        # Build content preview from first page of each section
        content_previews = []
        for section_title, info in list(sections.items())[:5]:
            if info["texts"]:
                preview = info["texts"][0][:300]
                if preview:
                    content_previews.append(f"[{section_title}]\n{preview}")

        preview_text = "\n\n".join(content_previews)
        if len(preview_text) > 2000:
            preview_text = preview_text[:2000] + "..."

        prompt = f"""请为以下技术文档生成一个简洁的文档级摘要（100-200字），说明文档的核心内容和主要章节：

文档: {parsed_doc.filename}

主要章节:
{chapter_text}

章节内容预览:
{preview_text}

请用中文回答，只输出摘要内容，不要其他解释。"""

        try:
            summary = self.model_client.generate(prompt, max_tokens=512, temperature=0.3)
            if summary and len(summary.strip()) > 20:
                return summary.strip()
        except Exception as e:
            logger.warning(f"[SUMMARY] LLM document summary generation failed: {e}")

        # Fallback
        return f"Document: {parsed_doc.filename}\n\nChapters:\n{chapter_text}"

    def _get_page_image_for_analysis(self, page, preprocessed) -> Image.Image | None:
        if preprocessed and preprocessed.page_image_path:
            try:
                return Image.open(preprocessed.page_image_path).convert('RGB')
            except Exception as e:
                logger.debug(f"Failed to load preprocessed image: {e}")
        if hasattr(page, 'page_image') and page.page_image:
            return page.page_image.convert('RGB')
        return None

    def _extract_formulas(self, layout_result, doc_id: str) -> list[dict]:
        formulas = []
        for elem in layout_result.elements:
            if elem.element_type == "formula":
                formula_id = f"{doc_id}_p{layout_result.page_num}_f{len(formulas)}"
                if elem.source == "image" and hasattr(elem, 'image_path'):
                    rec_result = self.formula_recognizer.recognize(elem.image_path, formula_id)
                    formulas.append({
                        "id": formula_id,
                        "latex": rec_result.get("latex", ""),
                        "confidence": rec_result.get("confidence", 0.0),
                        "image_path": elem.image_path
                    })
                else:
                    formulas.append({
                        "id": formula_id,
                        "latex": str(elem.content),
                        "confidence": 0.5,
                        "image_path": ""
                    })
        return formulas

    def _chunk_by_page_boundary(self, pages: list[tuple], max_chunk_size: int = 1200) -> list[str]:
        """
        Chunk by page boundaries, keeping page content intact when possible.
        If single page content exceeds max_chunk_size, split by structure.

        pages: [(page_id, page_num, raw_text), ...]
        return: List[chunk_text]
        """
        chunks = []
        current_chunk = ""

        for page_id, page_num, raw_text in pages:
            text = raw_text.strip() if raw_text else ""
            if not text:
                continue

            # If adding this page to current chunk stays within limit, merge
            if current_chunk:
                if len(current_chunk) + len(text) + 1 <= max_chunk_size:
                    current_chunk += "\n" + text
                else:
                    chunks.append(current_chunk)
                    current_chunk = text
            else:
                current_chunk = text

            # If a single page exceeds limit, split immediately (by structure)
            if len(current_chunk) > max_chunk_size * 1.2:
                if len(current_chunk) > max_chunk_size:
                    # Split using parser's structure splitter
                    sub_chunks = self.parser._chunk_by_structure(current_chunk, max_chunk_size=max_chunk_size)
                    if len(sub_chunks) <= 1:
                        sub_chunks = self.parser._simple_chunk(current_chunk, chunk_size=max_chunk_size, overlap=80)
                    chunks.extend([c.strip() for c in sub_chunks if len(c.strip()) >= 20])
                    current_chunk = ""

        if current_chunk:
            chunks.append(current_chunk)

        return chunks

    def _process_section_chunks(self, pages, section_path, section_title,
                                 chunk_size, chunk_overlap, chunk_items):
        """Process a section's page list, generate chunks respecting section boundaries.

        Strategy: Within the same section, merge adjacent pages as long as total
        stays within max_chunk_size. Only split when:
        1. A single page exceeds max_chunk_size (rare)
        2. Accumulated pages exceed max_chunk_size

        This preserves section coherence while respecting embedding model limits.
        """
        if not pages:
            return

        # Use max_chunk_size as the hard limit for section merging
        # Must respect embedding model's token limit to avoid 500 errors
        # llama-server batch size limits tokens per input; use chunk_size as safe upper bound
        max_chunk_size = min(chunk_size, settings.EMBEDDING_CONFIG.get("max_chunk_chars", chunk_size * 3))

        chunks_with_pages = []  # [(chunk_text, [(page_id, page_num)], primary_page_id)]

        current_chunk = ""
        current_pages = []

        for page_id, page_num, raw_text in pages:
            text = raw_text.strip()
            if not text:
                continue

            # If single page exceeds max_chunk_size, we must split it
            # Save current chunk first, then handle oversized page
            if len(text) > max_chunk_size:
                if current_chunk:
                    primary_page = current_pages[0] if current_pages else (page_id, page_num)
                    chunks_with_pages.append((current_chunk, current_pages, primary_page[0], primary_page[1]))
                    current_chunk = ""
                    current_pages = []
                # Split oversized page immediately using simple chunking
                sub_chunks = self.parser._simple_chunk(text, chunk_size=max_chunk_size, overlap=chunk_overlap)
                for sc in sub_chunks:
                    sc = sc.strip()
                    if len(sc) >= 20:
                        chunks_with_pages.append((sc, [(page_id, page_num)], page_id, page_num))
                continue

            if current_chunk:
                # Within same section: merge if under max_chunk_size
                if len(current_chunk) + len(text) + 1 <= max_chunk_size:
                    current_chunk += "\n" + text
                    current_pages.append((page_id, page_num))
                else:
                    # Exceeds limit: save current and start new
                    primary_page = current_pages[0] if current_pages else (page_id, page_num)
                    chunks_with_pages.append((current_chunk, current_pages, primary_page[0], primary_page[1]))
                    current_chunk = text
                    current_pages = [(page_id, page_num)]
            else:
                current_chunk = text
                current_pages = [(page_id, page_num)]

        if current_chunk:
            primary_page = current_pages[0] if current_pages else (pages[0][0], pages[0][1])
            chunks_with_pages.append((current_chunk, current_pages, primary_page[0], primary_page[1]))

        # Only split if a single chunk exceeds max_chunk_size significantly
        final_chunks = []
        for chunk_text, span_pages, primary_id, primary_num in chunks_with_pages:
            if len(chunk_text) > max_chunk_size * 1.2:
                # Only split oversized chunks, try structure first
                sub_chunks = self.parser._chunk_by_structure(chunk_text, max_chunk_size=max_chunk_size)
                if len(sub_chunks) <= 1:
                    sub_chunks = self.parser._simple_chunk(chunk_text, chunk_size=max_chunk_size, overlap=chunk_overlap)
                for sc in sub_chunks:
                    sc = sc.strip()
                    if len(sc) >= 20:
                        final_chunks.append((sc, primary_id, primary_num))
            else:
                final_chunks.append((chunk_text, primary_id, primary_num))

        # Merge adjacent small chunks (within same section, under max_chunk_size)
        merged = []
        for chunk_text, primary_id, primary_num in final_chunks:
            if merged and len(merged[-1][0]) + len(chunk_text) + 1 <= max_chunk_size:
                merged[-1] = (merged[-1][0] + "\n" + chunk_text, merged[-1][1], merged[-1][2])
            else:
                merged.append((chunk_text, primary_id, primary_num))

        # Add to chunk_items
        for chunk_idx, (chunk_text, primary_id, primary_num) in enumerate(merged):
            chunk_items.append((
                primary_id, primary_num, chunk_idx,
                section_path, section_title, chunk_text
            ))

    def _build_embeddings(self, doc_id: str, l2_results: list[dict], tid: str):
        """Build L2 chunk-level vector embeddings
FIX: aggregate text across pages by section before chunking to avoid scattering consecutive sections
FIX2: batch embedding + limit chunk count
FIX3: dynamically adjust per-section chunk limit
        """
        metadata_db, vector_db = self._get_dbs(tid)
        emb = settings.EMBEDDING_CONFIG
        MAX_CHUNKS_PER_DOC = emb["max_chunks_per_doc"]
        BATCH_SIZE = emb["batch_size"]
        CHUNK_SIZE = emb["chunk_size"]
        CHUNK_OVERLAP = max(40, CHUNK_SIZE // 10)

        try:
            pages = metadata_db.get_document_pages(doc_id)
            if not pages:
                return

            # Step 1: process pages individually, merge adjacent small pages within the same section
            chunk_items = []
            current_section = ""
            current_section_title = ""  # FIX: save current section title
            current_pages = []  # [(page_id, page_num, raw_text)]

            for page in sorted(pages, key=lambda p: p["page_num"]):
                raw_text = page.get("raw_text", "")
                if not raw_text or len(raw_text.strip()) < 10:
                    continue
                section_path = page.get("section_path", "") or ""
                section_title = page.get("section_title", "") or ""

                # If section changes or current accumulation is too large, process previous first
                if current_section and current_section != section_path:
                    # Process previous section - using saved current_section_title
                    self._process_section_chunks(
                        current_pages, current_section, current_section_title,
                        CHUNK_SIZE, CHUNK_OVERLAP, chunk_items
                    )
                    current_pages = []

                current_section = section_path
                current_section_title = section_title  # FIX: update current section title
                current_pages.append((page["id"], page["page_num"], raw_text))

                # If accumulated content exceeds threshold, process immediately (avoid oversized single section)
                total_len = sum(len(p[2]) for p in current_pages)
                if total_len > CHUNK_SIZE * 2:
                    self._process_section_chunks(
                        current_pages, current_section, current_section_title,
                        CHUNK_SIZE, CHUNK_OVERLAP, chunk_items
                    )
                    current_pages = []

            # Process last section
            if current_pages:
                self._process_section_chunks(
                    current_pages, current_section, current_section_title,
                    CHUNK_SIZE, CHUNK_OVERLAP, chunk_items
                )


            # Step 3: truncate by total document chunk count
            if len(chunk_items) > MAX_CHUNKS_PER_DOC:
                logger.warning(
                    f"[EMBED] Document {doc_id} original chunks={len(chunk_items)}, "
                    f"truncated to {MAX_CHUNKS_PER_DOC}"
                )
                chunk_items = chunk_items[:MAX_CHUNKS_PER_DOC]

            # Step 4: batch embedding and store
            total_stored = 0
            for batch_start in range(0, len(chunk_items), BATCH_SIZE):
                batch = chunk_items[batch_start:batch_start + BATCH_SIZE]
                texts = [item[5] for item in batch]

                embeddings = None
                for attempt in range(3):
                    try:
                        embeddings = self.model_client.embed_batch(texts)
                        break
                    except Exception as e:
                        if attempt < 2:
                            import time
                            time.sleep(2 ** attempt)
                            logger.warning(f"[EMBED] Batch embedding retry {attempt+1}/3: {e}")
                        else:
                            logger.error(f"[EMBED] Batch embedding final failure: {e}")

                if not embeddings or len(embeddings) != len(batch):
                    logger.warning(f"[EMBED] Batch {batch_start}-{batch_start+len(batch)} embedding failed, skipping")
                    continue

                for (page_id, page_num, chunk_idx, section_path, section_title, chunk_text), emb in zip(batch, embeddings):
                    try:
                        # Store in vector DB
                        vector_db.store_l2_chunk(
                            page_id=page_id,
                            chunk_idx=chunk_idx,
                            doc_id=doc_id,
                            embedding=emb,
                            chunk_text_preview=chunk_text[:200],
                            chunk_text=chunk_text
                        )
                        # Store to chunk metadata table (including FTS)
                        metadata_db.save_chunk(
                            doc_id=doc_id,
                            page_id=page_id,
                            page_num=page_num,
                            chunk_idx=chunk_idx,
                            section_path=section_path,
                            section_title=section_title,
                            chunk_text=chunk_text
                        )
                        total_stored += 1
                    except Exception as e:
                        logger.warning(f"[EMBED] store chunk failed page={page_id} idx={chunk_idx}: {e}")

            if total_stored > 0:
                logger.info(
                    f"[EMBED] Document {doc_id}: "
                    f"{len(chunk_items)} chunks, successfully stored {total_stored}"
                )
        except Exception as e:
            logger.error(f"Failed to build embeddings: {e}")

    def _get_content_sample_for_doc(self, doc_id: str, parsed_doc: ParsedDocument) -> str:
        return self._get_content_sample_from_pages(parsed_doc.pages)

    def _compute_file_hash(self, file_path: str) -> str:
        hash_md5 = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                hash_md5.update(chunk)
        return hash_md5.hexdigest()

    def _generate_doc_id(self, parsed_doc: ParsedDocument) -> str:
        content = f"{parsed_doc.filename}:{parsed_doc.file_size}:{parsed_doc.total_pages}"
        for i, page in enumerate(parsed_doc.pages[:3]):
            content += f":{page.raw_text[:500]}"
        return hashlib.md5(content.encode()).hexdigest()

    def _determine_text_source(self, preprocessed_pages: list) -> str:
        sources = [p.text_source for p in preprocessed_pages]
        if all(s == "direct_extract" for s in sources):
            return "direct_extract"
        elif all(s == "ocr" for s in sources):
            return "ocr"
        return "mixed"

    def _get_content_sample_from_pages(self, pages: list[ParsedPage]) -> str:
        sample = []
        current_chars = 0
        max_chars = settings.EMBEDDING_CONFIG["chunk_size"] * 5
        for page in pages:
            if current_chars + len(page.raw_text) > max_chars:
                remaining = max_chars - current_chars
                if remaining > 0:
                    sample.append(page.raw_text[:remaining])
                break
            sample.append(page.raw_text)
            current_chars += len(page.raw_text)
        return "\n".join(sample)

    def _generate_page_summary(self, text: str, page_num: int) -> str:
        if len(text) < 200:
            return text[:100] if text else f"Page {page_num}"
        sentences = text.split("。")
        summary = "。".join(sentences[:3])
        if len(summary) > 200:
            summary = summary[:200] + "..."
        return summary

    def _extract_entities(self, text: str, plugin=None) -> dict:
        """Extract entities based on industry plugin configuration"""
        import re
        entities = {}
        if not plugin or not hasattr(plugin, 'ingestion'):
            return entities
        skill = plugin.ingestion.get_skill_config()
        if not skill:
            return entities
        patterns = skill.get("entity_patterns", {})
        if not patterns:
            return entities
        for entity_type, pattern in patterns.items():
            try:
                matches = re.findall(pattern, text, re.IGNORECASE)
                if matches:
                    unique_matches = []
                    seen = set()
                    for m in matches:
                        key = m.lower() if isinstance(m, str) else str(m).lower()
                        if key not in seen:
                            seen.add(key)
                            unique_matches.append(m)
                    entities[entity_type] = unique_matches
            except re.error as e:
                logger.warning(f"Invalid entity pattern '{entity_type}': {e}")
        return entities

    def _find_section_title_in_text(self, text: str, title: str) -> int:
        """
        Find the position of section title in page text.
        Returns the position after the title, or 0 if not found.

        V5.0: Used to detect when a new chapter starts mid-page,
        so we can exclude previous section's content from current section's summary.
        """
        if not text or not title:
            return 0

        import re

        # Try exact match first
        title.lower()
        text.lower()

        # Look for title with various formats:
        # 1. "1.2.7 Video CODEC" (numbered prefix + title)
        # 2. "Video CODEC" (title only)
        # 3. "Video CODEC\n" (title with newline)

        # Try to find with numbered prefix pattern
        num_match = re.match(r'^(\d+(?:\.\d+)*)\s+(.+)', title)
        if num_match:
            path = num_match.group(1)
            display_title = num_match.group(2).strip()

            # Search for "1.2.7 Video CODEC" or "Video CODEC" in text
            patterns = [
                rf'{re.escape(path)}\s+{re.escape(display_title)}',
                rf'{re.escape(display_title)}',
            ]
        else:
            patterns = [rf'{re.escape(title)}']

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                # Return position after the matched title
                return match.end()

        return 0

    def _extract_keywords_from_title(self, title: str) -> str:
        """Extract keywords from section title (for structure index retrieval)"""
        if not title:
            return ""
        import re
        # Extract Chinese/English words/phrases, no stopword filtering
        words = re.findall(r'[\u4e00-\u9fff]{2,}|[A-Za-z][A-Za-z0-9\-_]{1,}', title)
        return ", ".join(words[:5])  # Max 5 keywords

    def _extract_title(self, filename: str) -> str:
        """Extract document title from filename.

        Root cause fix: no longer extract title from LLM-generated summary.
        LLM summary format is completely uncontrollable (often starts with chapter titles,
        numbered lists, bold markers); extracting title from it causes boilerplate phrases
        like "Core content summary as follows" or "I. Financial Performance" to become
        document titles. filename is the only reliable title source.
        """
        filename_title = self._clean_filename_as_title(filename)
        if filename_title and not self._is_uuid_like(filename_title):
            return filename_title
        return filename

    def _is_uuid_like(self, text: str) -> bool:
        """Check if text looks like a UUID (combination of hex + hyphens + spaces)"""
        if not text:
            return False
        import re
        cleaned = text.replace(' ', '').replace('-', '')
        # UUID is typically 32 hex chars after removing hyphens
        if len(cleaned) == 32 and re.match(r'^[a-f0-9]+$', cleaned):
            return True
        # Or entirely hex/hyphens/spaces
        if re.match(r'^[a-f0-9\s\-]+$', text) and len(text) >= 30:
            return True
        return False

    def _clean_filename_as_title(self, filename: str) -> str:
        import re
        # FIX: UUID format includes hyphens
        name = re.sub(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}_', '', filename)
        name = re.sub(r'\.(pdf|PDF|xlsx|xls|pptx|ppt|docx|jpg|jpeg|png|bmp|md|txt|html)$', '', name)
        name = name.replace('_', ' ').replace('-', ' ')
        return name.strip() if name.strip() else filename

    def release(self):
        self.preprocessor.release()
        self.formula_recognizer.release()
        logger.info("DocumentIndexBuilder resources released")
