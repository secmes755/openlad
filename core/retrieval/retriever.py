"""
Hierarchical Retriever - Thread-safe
FTS (TenantMetadataDB) + Vector (TenantVectorDB) + Structure Index
"""
import json
import logging
import os
import re
from typing import List, Dict, Any, Optional, Set, Tuple

from ..config import settings
from ..db.tenant_db import get_tenant_metadata_db, get_tenant_vector_db
from ..models.client import get_model_client, EmbeddingError
from .router import QueryPlan, IntentType

logger = logging.getLogger(__name__)


class SearchResult:
    def __init__(self, doc_id: str, page_id: int = None,
                 page_num: int = None, score: float = 0.0,
                 content: str = "", section_title: str = "",
                 filename: str = "", title: str = "",
                 fragment_id: str = None,
                 text_source: str = "direct_extract",
                 page_image_path: str = None,
                 extra_data: Dict = None):
        self.doc_id = doc_id
        self.page_id = page_id
        self.page_num = page_num
        self.score = score
        self.content = content
        self.section_title = section_title
        self.filename = filename
        self.title = title
        self.fragment_id = fragment_id
        self.text_source = text_source
        self.page_image_path = page_image_path
        self.extra_data = extra_data  # generic structured data (backward-compatible with old schematic_data)

    def to_dict(self) -> Dict:
        return {
            "doc_id": self.doc_id, "page_id": self.page_id, "page_num": self.page_num,
            "score": self.score, "content": self.content, "section_title": self.section_title,
            "filename": self.filename, "title": self.title, "fragment_id": self.fragment_id,
            "text_source": self.text_source, "page_image_path": self.page_image_path,
            "extra_data": self.extra_data,
        }


class HierarchicalRetriever:
    def __init__(self, tenant_id: str = None):
        self.tenant_id = tenant_id
        self.metadata_db = get_tenant_metadata_db(tenant_id) if tenant_id else None
        self.vector_db = get_tenant_vector_db(tenant_id) if tenant_id else None
        self.model_client = get_model_client()
        
        # FIX: admin tenant also loads the default tenant database
        self.fallback_metadata_db = None
        self.fallback_vector_db = None
        if tenant_id == "admin":
            try:
                self.fallback_metadata_db = get_tenant_metadata_db("default")
                self.fallback_vector_db = get_tenant_vector_db("default")
                logger.info(f"[RETRIEVER] admin tenant loaded default tenant database as fallback")
            except Exception as e:
                logger.warning(f"[RETRIEVER] admin tenant failed to load default database: {e}")

    def _load_industry_boost_rules_for_retrieval(self) -> Dict[str, Any]:
        """Load retrieval-phase rules from all registered industry packages (low-value section penalty, spec section boost, etc.)"""
        try:
            from ..plugins import get_plugin_registry
            registry = get_plugin_registry()
            all_rules = {"low_value_sections": [], "spec_sections": [], "package_model_pages": []}
            for pack_id in registry.list_plugins() if hasattr(registry, 'list_plugins') else []:
                plugin = registry.get_plugin(pack_id)
                if not plugin or not hasattr(plugin, 'retrieval'):
                    continue
                if hasattr(plugin.retrieval, 'get_low_value_sections'):
                    all_rules["low_value_sections"].extend(plugin.retrieval.get_low_value_sections())
                if hasattr(plugin.retrieval, 'get_spec_sections'):
                    all_rules["spec_sections"].extend(plugin.retrieval.get_spec_sections())
                try:
                    raw_rules = plugin.retrieval.get_retrieval_rules()
                    if "package_model_pages" in raw_rules:
                        all_rules["package_model_pages"].append(raw_rules["package_model_pages"])
                except Exception:
                    pass
            return all_rules
        except Exception as e:
            logger.debug(f"[RETRIEVER] failed to load industry retrieval rules: {e}")
            return {}

    def retrieve(self, query: str, plan: QueryPlan, max_results: int = 20,
                 explicit_doc_filter: Set[str] = None,
                 max_context_quota: int = None) -> List[SearchResult]:
        doc_id_filter = explicit_doc_filter if explicit_doc_filter else {"__ALL__"}

        intent_map = {
            IntentType.EXACT_LOOKUP: self._retrieve_exact,
            IntentType.RELATION_QUERY: self._retrieve_relation,
            IntentType.FEATURE_SEARCH: self._retrieve_feature,
            IntentType.VERSION_COMPARE: self._retrieve_version_compare,
            IntentType.CROSS_REFERENCE: self._retrieve_cross_reference,
        }
        retrieve_fn = intent_map.get(plan.intent, self._retrieve_macro)
        results = retrieve_fn(query, plan, max_results, doc_id_filter,
                              max_context_quota if plan.intent == IntentType.EXACT_LOOKUP else None)
        return results

    def _search_fts(self, query: str, limit: int, doc_id_filter: Optional[Set[str]] = None,
                    page_filter: Optional[Set[int]] = None) -> List[Dict]:
        """Execute chunk-level FTS via TenantMetadataDB with doc_id post-filtering.
        Coarse recall + full delivery - chunks are only used to locate relevant pages,
        the final result returns complete page content.
        
        Phase 2 FIX: Added page_filter parameter to restrict search to specific page numbers.
        When structure index locates relevant chapter ranges, FTS only searches within
        those pages, dramatically reducing noise for broad queries like "performance".
        
        FIX: 
        1. When query consists entirely of 2-character CJK words, FTS trigram tokenizer
           cannot index them; fall back to bigram LIKE search.
        2. When FTS returns 0 results, also fall back to bigram LIKE search.
        3. Collect all matching chunks, aggregate scores by page, return full pages
           instead of individual chunks.
        4. admin tenant also searches default tenant documents.
        """
        import re
        cjk_chars = re.findall(r'[\u4e00-\u9fff]', query)
        has_trigram = len(cjk_chars) >= 3
        
        all_chunk_results = []
        
        # Primary tenant search
        # FIX: Always try FTS first — even for 2-char CJK queries. If the query
        # contains non-CJK tokens like "AB1234" or "MCU", those can still be matched
        # via FTS trigram tokenizer. Fallback to bigram only when FTS returns 0.
        chunk_results = self.metadata_db.search_fts_chunks(query, limit=limit * 10, page_filter=page_filter)
        if not chunk_results:
            logger.info(f"[RETRIEVER] query '{query}' FTS returned 0 results, falling back to bigram LIKE")
            chunk_results = self.metadata_db.search_fts_chunks(query, limit=limit * 5, force_bigram_only=True, page_filter=page_filter)
        
        # Tag primary tenant results
        for r in chunk_results:
            r["_tenant"] = self.tenant_id or "default"
        all_chunk_results.extend(chunk_results)
        
        # FIX: admin tenant also searches default tenant documents
        if self.tenant_id == "admin" and self.fallback_metadata_db:
            try:
                # Same fix: always try FTS first
                fallback_chunks = self.fallback_metadata_db.search_fts_chunks(query, limit=limit * 10, page_filter=page_filter)
                if not fallback_chunks:
                    fallback_chunks = self.fallback_metadata_db.search_fts_chunks(query, limit=limit * 5, force_bigram_only=True, page_filter=page_filter)
                
                # Tag fallback results
                for r in fallback_chunks:
                    r["_tenant"] = "default"
                    r["_fallback"] = True
                all_chunk_results.extend(fallback_chunks)
                logger.info(f"[RETRIEVER] admin tenant fallback search on default: {len(fallback_chunks)} chunks")
            except Exception as e:
                logger.warning(f"[RETRIEVER] admin tenant fallback search failed: {e}")
        
        if doc_id_filter and "__ALL__" not in doc_id_filter:
            all_chunk_results = [r for r in all_chunk_results if r["doc_id"] in doc_id_filter]

        return self._aggregate_chunks_to_pages(all_chunk_results, query, limit, doc_id_filter)
    
    def _aggregate_chunks_to_pages(self, chunk_results: List[Dict], query: str,
                                    limit: int, doc_id_filter: Optional[Set[str]] = None) -> List[Dict]:
        """
        Aggregate chunk-level match results to the page level.
        
        Principles:
        - Chunks are only used to identify which pages contain query-relevant information
        - Final output returns full page raw_text, letting the LLM find answers in full context
        - Page score = number of matched chunks + chunk quality score + keyword hit bonus
        - Control page count to avoid context explosion
        """
        import re
        
        # Extract query keywords (for bonus scoring)
        cjk_range = r'\u4e00-\u9fff'
        # 2+ character CJK words + 3+ character English/digit tokens
        query_keywords = []
        for m in re.finditer(rf'[{cjk_range}]{{2,}}', query):
            query_keywords.append(m.group())
        for m in re.finditer(r'[A-Za-z0-9]{3,}', query):
            if not re.match(r'^\d+$', m.group()):  # filter out pure digits
                query_keywords.append(m.group())
        
        # Aggregate by page
        page_stats = {}  # (doc_id, page_id) -> {chunks, score_sum, chunk_texts, page_num}
        for r in chunk_results:
            key = (r["doc_id"], r["page_id"])
            if key not in page_stats:
                page_stats[key] = {
                    "doc_id": r["doc_id"],
                    "page_id": r["page_id"],
                    "page_num": r["page_num"],
                    "chunks": 0,
                    "score_sum": 0.0,
                    "chunk_texts": []
                }
            page_stats[key]["chunks"] += 1
            page_stats[key]["score_sum"] += r.get("score", 0.1)
            page_stats[key]["chunk_texts"].append(r.get("chunk_text", "")[:200])
        
        # Fetch full page text and compute page-level score
        page_scores = []
        for key, stats in page_stats.items():
            doc_id, page_id = key
            if doc_id_filter and "__ALL__" not in doc_id_filter and doc_id not in doc_id_filter:
                continue
            
            # FIX: admin tenant tries primary tenant first for page fetch, then fallback
            page = self.metadata_db.get_page(page_id)
            if not page and self.tenant_id == "admin" and self.fallback_metadata_db:
                try:
                    page = self.fallback_metadata_db.get_page(page_id)
                except Exception:
                    pass
            if not page:
                continue
            
            raw_text = page.get("raw_text", "") or ""
            section_title = page.get("section_title", "") or ""
            
            # Base score: matched chunk count and quality
            base_score = stats["score_sum"] + stats["chunks"] * 0.2
            
            # Keyword hit bonus
            keyword_bonus = 0
            text_lower = raw_text.lower()
            title_lower = section_title.lower()
            for kw in query_keywords:
                kw_lower = kw.lower()
                if kw_lower in title_lower:
                    keyword_bonus += 0.5
                if kw_lower in text_lower:
                    keyword_bonus += 0.3
            
            # FIX: Exact phrase match bonus (e.g., "Cortex-A53" full match)
            exact_phrase_bonus = 0
            for kw in query_keywords:
                kw_lower = kw.lower()
                # Extra bonus for multiple occurrences
                count = text_lower.count(kw_lower)
                if count >= 1:
                    exact_phrase_bonus += min(count * 0.5, 2.0)  # capped at 2.0
            
            # Title containing core query keyword gets extra bonus
            title_match = any(kw in title_lower for kw in query_keywords)
            if title_match:
                base_score += 0.4
            
            total_score = base_score + keyword_bonus + exact_phrase_bonus
            
            page_scores.append({
                "doc_id": doc_id,
                "page_id": page_id,
                "page_num": stats["page_num"],
                "score": total_score,
                "raw_text": raw_text,
                "section_title": section_title,
                "matched_chunks": stats["chunks"]
            })
        
        # Sort by score, take top N pages
        page_scores.sort(key=lambda x: x["score"], reverse=True)
        selected = page_scores[:limit]
        
        if selected:
            logger.info(f"[RETRIEVER] coarse recall returned {len(selected)} pages: "
                       f"{[(p['page_num'], round(p['score'], 2)) for p in selected[:5]]}")
        
        return selected

    def _tokenize_query(self, query: str) -> List[str]:
        """Extract meaningful keywords from a query for structure index / FTS search.
        
        Handles CJK and ASCII consistently:
        - CJK: 2+ character sequences (e.g., "详细规格", "是什么")
        - ASCII: 2+ character tokens (e.g., "AB1234", "MCU", "NPU")
        - Pure digit tokens are excluded
        
        This replaces the naive .split() approach which fails on Chinese
        (no spaces = 1 giant keyword = 0 structure index matches).
        """
        keywords = []
        # CJK: 2+ character sequences
        for m in re.finditer(r'[\u4e00-\u9fff]{2,}', query):
            keywords.append(m.group())
        # ASCII: 2+ character tokens (exclude pure digits)
        for m in re.finditer(r'[A-Za-z0-9]{2,}', query):
            token = m.group()
            if not re.match(r'^\d+$', token):
                keywords.append(token)
        return keywords

    def _retrieve_exact(self, query: str, plan: QueryPlan,
                         max_results: int, doc_id_filter: Set[str] = None,
                         max_context_quota: int = None) -> List[SearchResult]:
        all_results = []
        query_lower = query.lower()
        query_keywords = self._tokenize_query(query_lower)

        # First use structure index for section-level routing
        structure_boosted_pages = set()

        # FIX: Structure-index as a standalone retrieval path.
        # We collect structure-matched chapters as independent evidence, not just a filter.
        structure_results = []  # List[SearchResult]

        # FIX: Two-tier retrieval - first use structure index to locate most relevant chapters,
        # collect chapter page ranges
        chapter_pages = {}  # doc_id -> set(page_nums)
        if doc_id_filter and "__ALL__" not in doc_id_filter:
            for doc_id in doc_id_filter:
                matched_chapters = []
                for kw in query_keywords[:3]:
                    struct_results = self.metadata_db.search_structure_index(doc_id, kw)
                    for sr in struct_results:
                        # Compute match priority: exact title match > keyword match > summary match
                        priority = 0
                        title_lower = sr.get("section_title", "").lower()
                        kw_lower = kw.lower()
                        if kw_lower in title_lower:
                            priority = 3
                        elif kw_lower in sr.get("keywords", "").lower():
                            priority = 2
                        elif kw_lower in sr.get("summary", "").lower():
                            priority = 1
                        matched_chapters.append((priority, sr))

                # Deduplicate and sort by priority
                seen = set()
                unique_chapters = []
                for priority, sr in sorted(matched_chapters, key=lambda x: x[0], reverse=True):
                    key = (sr["section_path"], sr["start_page"])
                    if key not in seen:
                        seen.add(key)
                        unique_chapters.append(sr)

                # Take top-3 most matching chapters, collect page ranges
                pages = set()
                for sr in unique_chapters[:3]:
                    start_page = sr.get("start_page", 0)
                    end_page = sr.get("end_page", start_page)
                    for pn in range(start_page, end_page + 1):
                        pages.add(pn)

                # ── Selectivity-weighted chapter widening (generic) ──
                # The top-3 selection above only considers the first 3 query keywords
                # with no IDF weighting, so a frequent token (e.g. a product/model name
                # appearing in every section summary) can flood chapter selection and
                # exclude the chapter that actually contains the answer. Here ALL query
                # keywords are weighted by 1/(1+structure-df) and the top-K chapters by
                # aggregated weighted score are UNIONED into the page filter. This only
                # ever widens the filter, never narrows it, so behaviour is unchanged
                # whenever the original selection was already correct.
                retr_cfg = settings.CONTEXT_CONFIG
                keyword_dfs = {}
                chapter_weighted_scores = {}
                chapter_lookup = {}
                for kw in query_keywords:
                    if not kw:
                        continue
                    try:
                        kw_struct_results = self.metadata_db.search_structure_index(doc_id, kw)
                    except Exception:
                        kw_struct_results = []
                    keyword_dfs[kw] = len(kw_struct_results)
                    kw_weight = 1.0 / (1.0 + len(kw_struct_results))
                    for sr in kw_struct_results:
                        title_lower = sr.get("section_title", "").lower()
                        kw_lower = kw.lower()
                        if kw_lower in title_lower:
                            kw_priority = 3
                        elif kw_lower in sr.get("keywords", "").lower():
                            kw_priority = 2
                        else:
                            kw_priority = 1
                        key = (sr.get("section_path", ""), sr.get("start_page", 0))
                        chapter_weighted_scores[key] = chapter_weighted_scores.get(key, 0.0) + kw_priority * kw_weight
                        chapter_lookup[key] = sr

                weighted_topk = retr_cfg.get("structure_chapter_weighted_topk", 5)
                if chapter_weighted_scores and weighted_topk > 0:
                    ranked = sorted(chapter_weighted_scores.items(),
                                    key=lambda x: x[1], reverse=True)[:weighted_topk]
                    widened = set()
                    for key, _wscore in ranked:
                        sr = chapter_lookup[key]
                        start_page = sr.get("start_page", 0)
                        end_page = sr.get("end_page", start_page)
                        for pn in range(start_page, end_page + 1):
                            widened.add(pn)
                    new_pages = widened - pages
                    if new_pages:
                        pages.update(new_pages)
                        logger.info(
                            f"[RETRIEVER] selectivity-weighted chapters widened filter: "
                            f"doc={doc_id[:8]} +{len(new_pages)} pages {sorted(new_pages)[:10]}"
                        )

                # ── Rare-token page rescue (generic) ──
                # Exact identifiers (pin names, register names, part numbers, codes)
                # are highly discriminating: when such a token is rare in the structure
                # index, any page containing it verbatim is strong evidence and must not
                # be excluded by the chapter filter. Identifier-looking tokens are
                # scanned first; tokens matching too many pages are non-discriminating
                # and skipped (this bounds both cost and noise).
                if retr_cfg.get("rare_token_rescue_enabled", True):
                    rare_max_df = retr_cfg.get("rare_token_max_structure_df", 2)
                    rare_max_tokens = retr_cfg.get("rare_token_max_tokens", 5)
                    rare_max_pages = retr_cfg.get("rare_token_max_pages", 16)

                    def _identifier_rank(tok: str):
                        # Identifier-like: contains '_' or mixes letters and digits
                        has_mix = ("_" in tok) or (
                            any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok))
                        return (0 if has_mix else 1, -len(tok))

                    rare_keywords = sorted(
                        [kw for kw in query_keywords
                         if keyword_dfs.get(kw, 0) <= rare_max_df and len(kw) >= 5],
                        key=_identifier_rank)
                    rescued_pages = set()
                    for kw in rare_keywords[:rare_max_tokens]:
                        try:
                            hit_pages = self.metadata_db.find_pages_containing(
                                doc_id, kw, limit=rare_max_pages + 1)
                        except Exception:
                            hit_pages = []
                        if not hit_pages or len(hit_pages) > rare_max_pages:
                            continue  # no hits, or too common in page text to discriminate
                        rescued_pages.update(hit_pages)
                    new_rescued = rescued_pages - pages
                    if new_rescued:
                        pages.update(new_rescued)
                        structure_boosted_pages.update((doc_id, pn) for pn in new_rescued)
                        logger.info(
                            f"[RETRIEVER] rare-token rescue: doc={doc_id[:8]} "
                            f"+{len(new_rescued)} pages {sorted(new_rescued)[:10]} "
                            f"(tokens={rare_keywords[:rare_max_tokens]})"
                        )

                # FIX: Structure-index as an independent retrieval path.
                # Collect all structure-matched chapters as direct SearchResult evidence
                # so that overview/spec chapters are not filtered out by FTS ranking.
                chapter_score_map = {}
                for priority, sr in matched_chapters:
                    if priority <= 0:
                        continue
                    start_page = sr.get("start_page", 0)
                    end_page = sr.get("end_page", start_page)
                    # Chapter score: title match (5.0) / keyword match (4.0) / summary match (3.0)
                    # Shallow sections receive a small bonus for high-level summaries.
                    # FIX: Use a high base score so structure-matched chapters survive the merge phase
                    # even after generic low-value penalties (e.g., pages containing "copyright").
                    level = sr.get("section_level", 2)
                    level_bonus = max(0.0, 0.3 - level * 0.05)
                    base_score = {3: 10.0, 2: 8.0, 1: 6.0}.get(priority, 0.0)
                    chapter_score = base_score + level_bonus

                    for pn in range(start_page, end_page + 1):
                        pages.add(pn)
                        structure_boosted_pages.add((doc_id, pn))
                        # Keep the highest score for this page
                        chapter_score_map[(doc_id, pn)] = max(
                            chapter_score_map.get((doc_id, pn), 0.0),
                            chapter_score
                        )

                # Load the chapter's pages as independent SearchResult evidence
                if chapter_score_map:
                    pages_for_doc = self.metadata_db.get_document_pages(doc_id)
                    if not pages_for_doc and self.tenant_id == "admin" and self.fallback_metadata_db:
                        try:
                            pages_for_doc = self.fallback_metadata_db.get_document_pages(doc_id)
                        except Exception:
                            pass
                    doc = self.metadata_db.get_document(doc_id)
                    if not doc and self.tenant_id == "admin" and self.fallback_metadata_db:
                        try:
                            doc = self.fallback_metadata_db.get_document(doc_id)
                        except Exception:
                            pass
                    for (d_id, pn), c_score in chapter_score_map.items():
                        if d_id != doc_id:
                            continue
                        for p in pages_for_doc or []:
                            if p.get("page_num") == pn:
                                structure_results.append(SearchResult(
                                    doc_id=doc_id,
                                    page_id=p.get("id"),
                                    page_num=pn,
                                    score=c_score,
                                    content=(p.get("raw_text") or "")[:3000],
                                    section_title=p.get("section_title", ""),
                                    filename=doc.get("filename", "") if doc else "",
                                    title=doc.get("title", "") if doc else "",
                                    extra_data={"structure_match": True}
                                ))
                                break

                if pages:
                    chapter_pages[doc_id] = pages

                # Original logic: normal keyword-based structure index matching (for boost)
                for kw in query_keywords[:3]:
                    struct_results = self.metadata_db.search_structure_index(doc_id, kw)
                    for sr in struct_results:
                        start_page = sr.get("start_page", 0)
                        end_page = sr.get("end_page", start_page)
                        range_limit = 10
                        for pn in range(start_page, min(end_page + 1, start_page + range_limit)):
                            structure_boosted_pages.add((doc_id, pn))

            if chapter_pages:
                logger.info(f"[RETRIEVER] structure index located chapter ranges: {[(d[:8], sorted(p)[:5]) for d, p in chapter_pages.items()]}")
            if structure_boosted_pages:
                logger.info(f"[RETRIEVER] structure index hit {len(structure_boosted_pages)} pages")

        # Phase 2 FIX: Determine page_filter from chapter_pages for ALL docs in filter
        # When structure index locates chapter ranges, FTS only searches within those pages
        page_filter = None
        if chapter_pages:
            # Union all chapter pages from all matched docs
            all_chapter_pages = set()
            for pages in chapter_pages.values():
                all_chapter_pages.update(pages)
            if all_chapter_pages:
                page_filter = all_chapter_pages
                logger.info(f"[RETRIEVER] Phase 2: FTS restricted to {len(page_filter)} pages from structure index chapters")

        fts_results = self._search_fts(query, limit=max_results, doc_id_filter=doc_id_filter,
                                       page_filter=page_filter)
        for result in fts_results:
            # FIX: admin tenant tries primary tenant first for page fetch, then fallback
            page = self.metadata_db.get_page(result["page_id"])
            doc = self.metadata_db.get_document(result["doc_id"])
            if not page and self.tenant_id == "admin" and self.fallback_metadata_db:
                try:
                    page = self.fallback_metadata_db.get_page(result["page_id"])
                    doc = self.fallback_metadata_db.get_document(result["doc_id"])
                except Exception:
                    pass
            if page and doc:
                sr = self._create_search_result(result, page, doc)
                # Structure-hit page bonus
                if (sr.doc_id, sr.page_num) in structure_boosted_pages:
                    sr.score += 0.5
                    logger.debug(f"[RETRIEVER] structure boost: doc={sr.doc_id} page={sr.page_num}")
                # FIX: Apply industry-package low-value section penalty / spec section boost at retrieval stage
                section_lower = (sr.section_title or "").lower()
                industry_rules = self._load_industry_boost_rules_for_retrieval()
                if industry_rules:
                    # Low-value section penalty
                    for rule in industry_rules.get("low_value_sections", []):
                        keywords = rule.get("keywords", [])
                        penalty = rule.get("penalty", -5.0)
                        if any(k in section_lower for k in keywords):
                            sr.score += penalty
                            logger.debug(f"[RETRIEVER] low-value section penalty: doc={sr.doc_id} page={sr.page_num} title={sr.section_title}")
                    # Package model page penalty
                    for rule in industry_rules.get("package_model_pages", []):
                        pattern = rule.get("pattern", "")
                        penalty = rule.get("penalty", -5.0)
                        if pattern and re.search(pattern, section_lower):
                            sr.score += penalty
                            logger.debug(f"[RETRIEVER] package model page penalty: doc={sr.doc_id} page={sr.page_num} title={sr.section_title}")
                    # Spec section boost
                    for rule in industry_rules.get("spec_sections", []):
                        keywords = rule.get("keywords", [])
                        boost = rule.get("boost", 1.0)
                        if any(k in section_lower for k in keywords):
                            sr.score += boost
                            logger.debug(f"[RETRIEVER] spec section boost: doc={sr.doc_id} page={sr.page_num} title={sr.section_title}")
                all_results.append(sr)

        # Merge structure-index results with FTS results; structure hits may bring in pages
        # that FTS missed (e.g. overview pages with few keyword occurrences).
        # We do this BEFORE supplementary/leading-page recalls so structure evidence gets
        # full priority in the merge phase.
        existing_keys = {(r.doc_id, r.page_num) for r in all_results}
        for sr in structure_results:
            if (sr.doc_id, sr.page_num) in existing_keys:
                # If already in FTS results, boost the score because the structure index
                # independently confirms relevance.
                for existing in all_results:
                    if existing.doc_id == sr.doc_id and existing.page_num == sr.page_num:
                        existing.score += sr.score * 0.3
                        break
            else:
                if len(all_results) < max_results * 2:
                    all_results.append(sr)
            existing_keys.add((sr.doc_id, sr.page_num))

        # Supplementary recall: pages hit by structure index
        if structure_boosted_pages:
            existing_keys = {(r.doc_id, r.page_num) for r in all_results}
            # Sort by page_num so earlier pages in the document are supplemented first
            sorted_pages = sorted(structure_boosted_pages, key=lambda x: (x[0], x[1]))
            for doc_id, page_num in sorted_pages:
                if (doc_id, page_num) in existing_keys:
                    continue
                if len(all_results) >= max_results:
                    break
                # FIX: admin tenant tries primary tenant first, then fallback
                pages = self.metadata_db.get_document_pages(doc_id)
                if not pages and self.tenant_id == "admin" and self.fallback_metadata_db:
                    try:
                        pages = self.fallback_metadata_db.get_document_pages(doc_id)
                    except Exception:
                        pass
                for p in pages:
                    if p.get("page_num") == page_num:
                        # FIX: admin tenant tries primary tenant for document fetch, then fallback
                        doc = self.metadata_db.get_document(doc_id)
                        if not doc and self.tenant_id == "admin" and self.fallback_metadata_db:
                            try:
                                doc = self.fallback_metadata_db.get_document(doc_id)
                            except Exception:
                                pass
                        all_results.append(SearchResult(
                            doc_id=doc_id, page_id=p.get("id"), page_num=page_num,
                            score=0.35,
                            content=(p.get("raw_text") or "")[:1500],  # FIX: increased supplementary page content length
                            section_title=p.get("section_title", ""),
                            filename=doc.get("filename", "") if doc else "",
                            title=doc.get("title", "") if doc else ""
                        ))
                        existing_keys.add((doc_id, page_num))
                        break

        # Additionally recall the first N pages of each document (Overview/Introduction)
        # to ensure core specs are not missed.
        # FIX: The first 3 pages may not be core spec pages (could be cover/TOC/copyright),
        # lower their base score to avoid drowning out high-value pages.
        leading_pages_limit = 3
        if doc_id_filter and "__ALL__" not in doc_id_filter:
            existing_keys = {(r.doc_id, r.page_num) for r in all_results}
            for doc_id in doc_id_filter:
                # FIX: admin tenant tries primary tenant first, then fallback
                pages = self.metadata_db.get_document_pages(doc_id)
                if not pages and self.tenant_id == "admin" and self.fallback_metadata_db:
                    try:
                        pages = self.fallback_metadata_db.get_document_pages(doc_id)
                    except Exception:
                        pass
                for p in pages:
                    pn = p.get("page_num", 0)
                    if pn <= leading_pages_limit and (doc_id, pn) not in existing_keys:
                        # FIX: admin tenant tries primary tenant for document fetch, then fallback
                        doc = self.metadata_db.get_document(doc_id)
                        if not doc and self.tenant_id == "admin" and self.fallback_metadata_db:
                            try:
                                doc = self.fallback_metadata_db.get_document(doc_id)
                            except Exception:
                                pass
                        # V5.0: Use configurable low-value section indicators + industry package rules
                        section_title = (p.get("section_title", "") or "").lower()
                        raw_text = (p.get("raw_text", "") or "")[:200].lower()
                        
                        # Check generic low-value indicators from config
                        generic_indicators = settings.CONTEXT_CONFIG.get("low_value_section_indicators", [])
                        is_low_value = any(ind in section_title or ind in raw_text for ind in generic_indicators)
                        
                        # Check industry-specific low-value sections from rules
                        if not is_low_value:
                            industry_rules = self._load_industry_boost_rules_for_retrieval()
                            if industry_rules:
                                for rule in industry_rules.get("low_value_sections", []):
                                    keywords = rule.get("keywords", [])
                                    if any(k in section_title or k in raw_text for k in keywords):
                                        is_low_value = True
                                        break
                        
                        # Low-value pages get low scores to avoid ranking above Features/Overview
                        page_score = 0.15 if is_low_value else 0.35
                        all_results.append(SearchResult(
                            doc_id=doc_id, page_id=p.get("id"), page_num=pn,
                            score=page_score,
                            content=(p.get("raw_text") or "")[:1500],
                            section_title=p.get("section_title", ""),
                            filename=doc.get("filename", "") if doc else "",
                            title=doc.get("title", "") if doc else ""
                        ))
                        existing_keys.add((doc_id, pn))

        # FIX: Vector fallback only triggers when FTS has partial results but not enough.
        # If FTS returns 0 results entirely, it means the query's core terms simply don't exist
        # in the documents. Falling back to vector search would only recall semantically
        # adjacent but irrelevant pages, wasting resources.
        if 0 < len(all_results) < max_results // 2:
            try:
                query_emb = self.model_client.embed(query)
                vec_results = self.vector_db.search_l2_chunks(query_emb, limit=max_results, doc_id_filter=doc_id_filter, min_score=0.3)
                existing_page_ids = {r.page_id for r in all_results}
                for result in vec_results:
                    if result["page_id"] not in existing_page_ids:
                        # FIX: admin tenant tries primary tenant first, then fallback
                        page = self.metadata_db.get_page(result["page_id"])
                        doc = self.metadata_db.get_document(result["doc_id"])
                        if not page and self.tenant_id == "admin" and self.fallback_metadata_db:
                            try:
                                page = self.fallback_metadata_db.get_page(result["page_id"])
                                doc = self.fallback_metadata_db.get_document(result["doc_id"])
                            except Exception:
                                pass
                        if page and doc:
                            sr = self._create_search_result(result, page, doc)
                            if (sr.doc_id, sr.page_num) in structure_boosted_pages:
                                sr.score += 0.3
                            all_results.append(sr)
            except EmbeddingError as e:
                logger.warning(f"Vector search fallback failed: {e}")


        initial_doc_ids = set(r.doc_id for r in all_results)
        docs_to_supplement = set(initial_doc_ids)
        if doc_id_filter and "__ALL__" not in doc_id_filter:
            for doc_id in doc_id_filter:
                if doc_id not in initial_doc_ids:
                    logger.info(f"Doc {doc_id} not found, will supplement pages")
                docs_to_supplement.add(doc_id)

        cfg = settings.CONTEXT_CONFIG
        avg_page_chars = cfg.get("avg_page_chars", 1200)
        min_pages = cfg.get("min_pages_per_doc", 3)  # OPT: lowered minimum pages per doc
        max_pages = cfg.get("max_pages_per_doc", 20)  # OPT: lowered maximum pages per doc
        num_target_docs = len(docs_to_supplement) if docs_to_supplement else 1

        if doc_id_filter and "__ALL__" not in doc_id_filter and num_target_docs <= 3:
            if max_context_quota:
                quota_per_doc = max_context_quota // num_target_docs
                calculated_pages = quota_per_doc // avg_page_chars
                max_pages_per_doc = max(min(calculated_pages, max_pages), min_pages)
                max_total_results = max_pages_per_doc * num_target_docs
            else:
                max_pages_per_doc = max(max_results, 8)  # OPT: lowered floor
                max_total_results = max_results * 2  # OPT: lowered multiplier
                # FIX: Single-document query: limit total results to avoid context bloat triggering Map-Reduce
                if num_target_docs == 1 and max_total_results > 15:
                    max_total_results = 15
                    max_pages_per_doc = 15
        else:
            max_pages_per_doc = min_pages
            max_total_results = max_results

        for doc_id in docs_to_supplement:
            if doc_id_filter and "__ALL__" not in doc_id_filter and doc_id not in doc_id_filter:
                continue
            # FIX: admin tenant tries primary tenant first, then fallback
            pages = self.metadata_db.get_document_pages(doc_id)
            doc = self.metadata_db.get_document(doc_id)
            if not pages and self.tenant_id == "admin" and self.fallback_metadata_db:
                try:
                    pages = self.fallback_metadata_db.get_document_pages(doc_id)
                    doc = self.fallback_metadata_db.get_document(doc_id)
                except Exception:
                    pass
            if not doc:
                continue

            pages_with_scores = []
            # FIX: Supplement documents only within chapter ranges (when structure index hits)
            doc_chapter_pages = chapter_pages.get(doc_id) if chapter_pages else None
            for page in pages:
                if any(r.page_id == page["id"] for r in all_results):
                    continue
                # Chapter range filter: only supplement within structure-index-hit chapter ranges
                if doc_chapter_pages is not None and page.get("page_num") not in doc_chapter_pages:
                    continue
                page_text = (page.get("raw_text", "") or "").lower()
                section_title = (page.get("section_title", "") or "").lower()
                keyword_matches = sum(1 for kw in query_keywords if kw in page_text or kw in section_title)
                title_bonus = 2.0 if any(kw in section_title for kw in query_keywords) else 0.0
                score = keyword_matches * 0.5 + title_bonus
                # No longer using hardcoded info_indicators list for boosting; let FTS and structure index results rank naturally
                pages_with_scores.append((page, score))

            pages_with_scores.sort(key=lambda x: x[1], reverse=True)
            added_count = 0
            for page, score in pages_with_scores:
                if len(all_results) >= max_total_results or added_count >= max_pages_per_doc:
                    break
                all_results.append(SearchResult(
                    doc_id=doc_id, page_id=page["id"], page_num=page.get("page_num"),
                    score=max(0.15, 0.15 + score * 0.05),
                    content=page.get("raw_text", ""), section_title=page.get("section_title", ""),
                    filename=doc.get("filename", ""), title=doc.get("title", ""),
                    text_source=page.get("text_source", "direct_extract"),
                    page_image_path=page.get("page_image_path")
                ))
                added_count += 1

            # FIX: If chapter range is located and it's a comprehensive scan query,
            # fallback supplement also stays within the range
            if added_count == 0 and len(pages) > 0 and doc_chapter_pages is None:
                for page in pages:
                    if not any(r.page_id == page["id"] for r in all_results) and len(all_results) < max_total_results and added_count < max_pages_per_doc:
                        all_results.append(SearchResult(
                            doc_id=doc_id, page_id=page["id"], page_num=page.get("page_num"),
                            score=0.15, content=page.get("raw_text", ""),
                            section_title=page.get("section_title", ""),
                            filename=doc.get("filename", ""), title=doc.get("title", ""),
                            text_source=page.get("text_source", "direct_extract"),
                            page_image_path=page.get("page_image_path")
                        ))
                        added_count += 1

        all_results.sort(key=lambda x: x.score, reverse=True)
        if doc_id_filter and "__ALL__" not in doc_id_filter:
            all_results = [r for r in all_results if r.doc_id in doc_id_filter]

        # FIX: Industry package supplementary recall must execute AFTER doc_id_filter filtering,
        # otherwise when the planner routes a query to specific documents,
        # the supplement results would be filtered out.
        if plan.industry_hint:
            try:
                from ..plugins import get_plugin_registry
                registry = get_plugin_registry()
                plugin = registry.get_plugin(plan.industry_hint)
                if plugin and hasattr(plugin.retrieval, 'supplement_results'):
                    extra_results = plugin.retrieval.supplement_results(
                        query, all_results, self.metadata_db
                    )
                    if extra_results:
                        existing_map = {(r.doc_id, r.page_num): r for r in all_results}
                        added_count = 0
                        updated_count = 0
                        for sr in extra_results:
                            key = (sr.doc_id, sr.page_num)
                            if key not in existing_map:
                                all_results.append(sr)
                                existing_map[key] = sr
                                added_count += 1
                            elif sr.score > existing_map[key].score:
                                # FIX: Supplement result has higher score, update existing result's score
                                existing_map[key].score = sr.score
                                updated_count += 1
                        if added_count or updated_count:
                            logger.info(f"[RETRIEVER] industry package supplement recall: +{added_count} new results, updated {updated_count} existing results")
                            # Re-sort
                            all_results.sort(key=lambda x: x.score, reverse=True)
            except Exception as e:
                logger.debug(f"[RETRIEVER] industry package supplement recall failed: {e}")

        return all_results[:max(max_total_results, max_results)]

    def _retrieve_relation(self, query: str, plan: QueryPlan,
                            max_results: int, doc_id_filter: Set[str] = None) -> List[SearchResult]:
        all_results = []
        entities = plan.entities or []
        if not entities:
            return self._retrieve_macro(query, plan, max_results, doc_id_filter)
        per_entity = max(max_results // len(entities), 20) if plan.deep_explore else max(max_results // len(entities), 5)
        for entity in entities:
            all_results.extend(self._retrieve_exact(entity, plan, per_entity, doc_id_filter))
        seen = set()
        unique = []
        for r in all_results:
            key = (r.doc_id, r.page_id)
            if key not in seen:
                seen.add(key)
                unique.append(r)
        unique.sort(key=lambda x: x.score, reverse=True)
        return unique[:max_results]

    def _retrieve_feature(self, query: str, plan: QueryPlan,
                           max_results: int, doc_id_filter: Set[str] = None) -> List[SearchResult]:
        try:
            query_emb = self.model_client.embed(query)
            if query_emb:
                # FIX: admin tenant also searches fallback vector database
                all_vec_results = []
                
                # Primary tenant vector search
                vec_results = self.vector_db.search_l2_chunks(query_emb, limit=max_results, doc_id_filter=doc_id_filter)
                for r in vec_results:
                    r["_tenant"] = self.tenant_id or "default"
                all_vec_results.extend(vec_results)
                
                # Fallback tenant vector search
                if self.tenant_id == "admin" and self.fallback_vector_db:
                    try:
                        fallback_vec_results = self.fallback_vector_db.search_l2_chunks(query_emb, limit=max_results, doc_id_filter=doc_id_filter)
                        for r in fallback_vec_results:
                            r["_tenant"] = "default"
                            r["_fallback"] = True
                        all_vec_results.extend(fallback_vec_results)
                        logger.info(f"[RETRIEVER] admin tenant fallback vector search: {len(fallback_vec_results)} results")
                    except Exception as e:
                        logger.warning(f"[RETRIEVER] admin tenant fallback vector search failed: {e}")
                
                results = []
                for result in all_vec_results:
                    # FIX: admin tenant tries primary tenant first, then fallback
                    page = self.metadata_db.get_page(result["page_id"])
                    doc = self.metadata_db.get_document(result["doc_id"])
                    if not page and self.tenant_id == "admin" and self.fallback_metadata_db:
                        try:
                            page = self.fallback_metadata_db.get_page(result["page_id"])
                            doc = self.fallback_metadata_db.get_document(result["doc_id"])
                        except Exception:
                            pass
                    if page and doc:
                        results.append(self._create_search_result(result, page, doc))
                return results
        except EmbeddingError:
            pass
        return self._retrieve_macro(query, plan, max_results, doc_id_filter)

    def _retrieve_macro(self, query: str, plan: QueryPlan,
                         max_results: int, doc_id_filter: Set[str] = None) -> List[SearchResult]:
        all_results = []
        fts_results = self._search_fts(query, limit=max_results * 2, doc_id_filter=doc_id_filter)
        for result in fts_results:
            # FIX: admin tenant tries primary tenant first, then fallback
            page = self.metadata_db.get_page(result["page_id"])
            doc = self.metadata_db.get_document(result["doc_id"])
            if not page and self.tenant_id == "admin" and self.fallback_metadata_db:
                try:
                    page = self.fallback_metadata_db.get_page(result["page_id"])
                    doc = self.fallback_metadata_db.get_document(result["doc_id"])
                except Exception:
                    pass
            if page and doc:
                all_results.append(self._create_search_result(result, page, doc))
        if len(all_results) < max_results:
            try:
                query_emb = self.model_client.embed(query)
                if query_emb:
                    # FIX: admin tenant also searches fallback vector database
                    all_vec_results = []
                    
                    vec_results = self.vector_db.search_l2_chunks(query_emb, limit=max_results, doc_id_filter=doc_id_filter, min_score=0.35)
                    for r in vec_results:
                        r["_tenant"] = self.tenant_id or "default"
                    all_vec_results.extend(vec_results)
                    
                    if self.tenant_id == "admin" and self.fallback_vector_db:
                        try:
                            fallback_vec_results = self.fallback_vector_db.search_l2_chunks(query_emb, limit=max_results, doc_id_filter=doc_id_filter, min_score=0.35)
                            for r in fallback_vec_results:
                                r["_tenant"] = "default"
                                r["_fallback"] = True
                            all_vec_results.extend(fallback_vec_results)
                        except Exception:
                            pass
                    
                    for result in all_vec_results:
                        if not any(r.page_id == result["page_id"] for r in all_results) and len(all_results) < max_results * 2:
                            # FIX: admin tenant tries primary tenant first, then fallback
                            page = self.metadata_db.get_page(result["page_id"])
                            doc = self.metadata_db.get_document(result["doc_id"])
                            if not page and self.tenant_id == "admin" and self.fallback_metadata_db:
                                try:
                                    page = self.fallback_metadata_db.get_page(result["page_id"])
                                    doc = self.fallback_metadata_db.get_document(result["doc_id"])
                                except Exception:
                                    pass
                            if page and doc:
                                vec_score = result.get("score", 0.0) * 0.8
                                if vec_score < 0.25:
                                    continue
                                # FIX: Vector results also apply chunk_text vs raw_text selection logic
                                raw_text = page.get("raw_text", "")
                                chunk_text = result.get("chunk_text", "")
                                if chunk_text and raw_text and chunk_text not in raw_text:
                                    content = chunk_text
                                else:
                                    content = raw_text or chunk_text
                                all_results.append(SearchResult(
                                    doc_id=result["doc_id"], page_id=result["page_id"],
                                    page_num=page.get("page_num"), score=vec_score,
                                    content=content, section_title=page.get("section_title", ""),
                                    filename=doc.get("filename", ""), title=doc.get("title", ""),
                                    text_source=page.get("text_source", "direct_extract"),
                                    page_image_path=page.get("page_image_path")
                                ))
            except EmbeddingError:
                pass
        all_results.sort(key=lambda x: x.score, reverse=True)
        # DEBUG: log top pages
        logger.warning(f"[RETRIEVER-DEBUG] _retrieve_macro all_results top 30:")
        for i, r in enumerate(all_results[:30]):
            logger.warning(f"  [{i+1}] score={r.score:.2f} doc={r.doc_id[:16] if r.doc_id else 'None'} page={r.page_num} title={r.section_title[:60]}")
        return all_results[:max_results]

    def _retrieve_version_compare(self, query: str, plan: QueryPlan,
                                   max_results: int, doc_id_filter: Set[str] = None) -> List[SearchResult]:
        all_results = []
        targets = plan.compare_targets or plan.entities[:2]
        for target in targets:
            all_results.extend(self._retrieve_exact(target, plan, max_results // len(targets) + 5, doc_id_filter))
        seen = set()
        unique = [r for r in all_results if (r.doc_id, r.page_id) not in seen and not seen.add((r.doc_id, r.page_id))]
        unique.sort(key=lambda x: x.score, reverse=True)
        return unique[:max_results]

    def _retrieve_cross_reference(self, query: str, plan: QueryPlan,
                                   max_results: int, doc_id_filter: Set[str] = None) -> List[SearchResult]:
        all_results = list(self._retrieve_macro(query, plan, max_results, doc_id_filter))
        for ref in (plan.reference_marks or []):
            all_results.extend(self._retrieve_exact(ref, plan, 5, doc_id_filter))
        seen = set()
        unique = [r for r in all_results if (r.doc_id, r.page_id) not in seen and not seen.add((r.doc_id, r.page_id))]
        unique.sort(key=lambda x: x.score, reverse=True)
        return unique[:max_results]

    def _create_search_result(self, result: Dict, page: Dict, doc: Dict) -> SearchResult:
        # Support chunk-level results (using chunk_text) and page-level results (using raw_text)
        # FIX: Prefer full page raw_text to avoid chunk truncation causing critical info loss.
        # Chunk retrieval may only match partial page content, but answer synthesis needs full context.
        # 
        # FIX2: When chunks span multiple pages (e.g., legal docs aggregated by chapter),
        # the chunk is linked to the chapter's first page, but that page's raw_text doesn't
        # include the second half of the chunk. In this case, if chunk_text is not a sub-string
        # of page.raw_text, use chunk_text instead of raw_text.
        raw_text = page.get("raw_text", "")
        chunk_text = result.get("chunk_text", "")
        if chunk_text and raw_text and chunk_text not in raw_text:
            # Chunk content not in current page's raw_text (cross-page chunk), use chunk_text
            content = chunk_text
        else:
            content = raw_text or chunk_text
        # Compatible with extra_data (new) and schematic_data (old)
        extra_data = page.get("extra_data") or page.get("schematic_data")
        return SearchResult(
            doc_id=result["doc_id"], page_id=result.get("page_id", page.get("id")),
            page_num=page.get("page_num"), score=result.get("score", 0.0),
            content=content, section_title=page.get("section_title", result.get("section_title", "")),
            filename=doc.get("filename", ""), title=doc.get("title", ""),
            text_source=page.get("text_source", "direct_extract"),
            page_image_path=page.get("page_image_path"),
            extra_data=extra_data,
        )


class SegmentMerger:
    def __init__(self, tenant_id: str = None):
        self.tenant_id = tenant_id
        self.metadata_db = get_tenant_metadata_db(tenant_id) if tenant_id else None
        
        # FIX: admin tenant also loads the default tenant database
        self.fallback_metadata_db = None
        if tenant_id == "admin":
            try:
                self.fallback_metadata_db = get_tenant_metadata_db("default")
                logger.info(f"[MERGER] admin tenant loaded default tenant database as fallback")
            except Exception as e:
                logger.warning(f"[MERGER] admin tenant failed to load default database: {e}")

    def _path_to_url(self, path: str) -> str:
        if not path:
            return ""
        return f"/images/{os.path.basename(path)}"

    def _load_industry_boost_rules(self, industry_hint: str) -> Dict[str, Any]:
        """Load industry package chapter boost rules (for merge phase)"""
        if not industry_hint or industry_hint == "auto":
            return {}
        try:
            from ..plugins import get_plugin_registry
            registry = get_plugin_registry()
            plugin = registry.get_plugin(industry_hint)
            if not plugin:
                plugin = registry.get_plugin_by_category(industry_hint)
            if plugin and hasattr(plugin.retrieval, 'get_section_boost_rules'):
                return plugin.retrieval.get_section_boost_rules()
        except Exception as e:
            logger.debug(f"[MERGER] failed to load industry boost rules: {e}")
        return {}

    def _enhance_list_content(self, content: str, section_title: str) -> str:
        """No longer using hardcoded list enhancement; return original content directly"""
        return content

    def _expand_section_pages(self, doc_id: str, existing_pages: set,
                               metadata_db) -> Dict[int, Dict]:
        """Section-level recall: use structure_index to expand the full page range of a section,
        including adjacent context pages.
        
        Returns {page_num: {'section_title': str, 'raw_text': str}} mapping for missing pages.
        """
        if not metadata_db:
            return {}
        try:
            # Get all section info for this doc from doc_structure_index
            sections = metadata_db.execute(
                "SELECT section_path, section_title, section_level, "
                "start_page, end_page, parent_path FROM doc_structure_index "
                "WHERE doc_id = ? ORDER BY start_page",
                (doc_id,)
            ).fetchall() if hasattr(metadata_db, 'execute') else []
            
            if not sections:
                # Try with cursor
                with metadata_db.get_connection() as conn:
                    cur = conn.cursor()
                    sections = cur.execute(
                        "SELECT section_path, section_title, section_level, "
                        "start_page, end_page, parent_path FROM doc_structure_index "
                        "WHERE doc_id = ? ORDER BY start_page",
                        (doc_id,)
                    ).fetchall()
            
            # Build a map: section_path -> (start, end, level, parent_path)
            section_map = {}
            for row in sections:
                section_map[row[0]] = {
                    'start': row[3], 'end': row[4],
                    'level': row[2], 'parent': row[5],
                    'title': row[1]
                }
            
            # For each existing page, find all sections that cover it
            pages_to_add = set()
            parent_ranges = {}  # parent_path -> (min_page, max_page)
            
            # Build parent section ranges
            for sp, info in section_map.items():
                parent = info['parent']
                if parent and parent in section_map:
                    ps = section_map[parent]
                    if parent not in parent_ranges:
                        parent_ranges[parent] = [ps['start'], ps['end']]
            
            for page_num in existing_pages:
                # Find which sections contain this page
                for sp, info in section_map.items():
                    if info['start'] <= page_num <= info['end']:
                        # Add the full section range
                        for p in range(info['start'], info['end'] + 1):
                            pages_to_add.add(p)
                        # Also add ±1 adjacent pages within parent section
                        parent = info.get('parent', '')
                        if parent in parent_ranges:
                            parent_range = parent_ranges[parent]
                            for p in [page_num - 1, page_num + 1]:
                                if parent_range[0] <= p <= parent_range[1]:
                                    pages_to_add.add(p)
            
            # Find which pages are missing
            missing = pages_to_add - existing_pages
            if not missing:
                return {}
            
            # Fetch missing pages from doc_pages
            placeholders = ','.join('?' * len(missing))
            with metadata_db.get_connection() as conn:
                rows = conn.execute(
                    f"SELECT page_num, section_title, raw_text "
                    f"FROM doc_pages WHERE doc_id = ? AND page_num IN ({placeholders}) "
                    f"ORDER BY page_num",
                    (doc_id,) + tuple(sorted(missing))
                ).fetchall()
            
            logger.info(f"[MERGER] section expansion: doc={doc_id}, retrieved={len(existing_pages)} pages, "
                        f"expanded +{len(rows)} pages (missing={len(missing)})")
            
            return {r[0]: {'section_title': r[1], 'raw_text': r[2] or ''} for r in rows}
        except Exception as e:
            logger.warning(f"[MERGER] section expansion failed: doc={doc_id}, err={e}")
            return {}

    def _smart_truncate(self, page_text: str, query: str, max_len: int) -> str:
        """
        Intelligently truncate page content, prioritizing paragraphs containing query keywords.
        
        Strategy:
        1. Extract sentences containing query keywords (priority preservation)
        2. Preserve content beginning (usually has title/overview)
        3. Preserve content ending (usually has summary/tables)
        4. Truncate middle sections as needed
        """
        import re
        
        if len(page_text) <= max_len:
            return page_text
        
        # Extract query keywords (2+ character CJK, 3+ character English)
        keywords = []
        for m in re.finditer(r'[\u4e00-\u9fff]{2,}', query):
            keywords.append(m.group())
        for m in re.finditer(r'[A-Za-z0-9]{3,}', query):
            if not re.match(r'^\d+$', m.group()):
                keywords.append(m.group())
        
        # Find sentences containing keywords
        sentences = re.split(r'(?<=[。！？\.\!\?])\s+', page_text)
        keyword_sentences = []
        other_sentences = []
        
        for s in sentences:
            s = s.strip()
            if not s:
                continue
            has_kw = any(kw.lower() in s.lower() for kw in keywords if kw)
            if has_kw:
                keyword_sentences.append(s)
            else:
                other_sentences.append(s)
        
        # Build result: keyword sentences + head + tail
        parts = []
        parts.append("...[excerpt]...")
        
        # Add keyword sentences
        if keyword_sentences:
            parts.append("\n[Relevant paragraphs]")
            parts.extend(keyword_sentences[:10])  # at most 10 sentences
        
        # Calculate remaining space
        used = len("\n".join(parts))
        remaining = max_len - used - 100
        
        if remaining > 500:
            # Preserve head and tail
            half = remaining // 2
            head = page_text[:half]
            tail = page_text[-half:] if len(page_text) > half * 2 else ""
            parts.append(f"\n{head}")
            if tail and tail not in head:
                parts.append(f"\n...\n{tail}")
        
        result = "\n".join(parts)
        if len(result) > max_len:
            result = result[:max_len] + "\n...[truncated]"
        return result

    def _segment_page_content(self, content: str, section_title: str) -> str:
        """
        Simple content segmentation - split by blank lines, number each segment.
        
        No dependency on heading format; works for any document type.
        Helps the LLM notice multiple paragraphs within a page, but does not infer topics.
        """
        if not content or len(content) < 1000:
            return content
        
        # Split by blank lines (2+ consecutive newlines)
        paragraphs = content.split('\n\n')
        
        # If too few paragraphs, no segmentation needed
        if len(paragraphs) <= 3:
            return content
        
        # Simple labeling of each paragraph to help LLM locate
        result_lines = []
        for i, para in enumerate(paragraphs):
            if len(para.strip()) > 50:  # Only label meaningful paragraphs
                result_lines.append(f"[Section {i+1}]")
            result_lines.append(para)
            result_lines.append("")  # Preserve blank lines
        
        return '\n'.join(result_lines)

    def _match_boost_rule(self, query: str, section_title: str, rules: Dict[str, Any]) -> float:
        """Match boost rules based on query and section title, return boost score"""
        if not rules:
            return 0.0
        query_lower = query.lower()
        section_lower = (section_title or "").lower()
        total_boost = 0.0
        for rule_name, rule in rules.items():
            if not isinstance(rule, dict):
                continue
            # Check if query matches this rule's intent
            query_keywords = rule.get("query_keywords", [])
            if not any(kw.lower() in query_lower for kw in query_keywords):
                continue
            # Check if section matches boost_sections
            for boost_item in rule.get("boost_sections", []):
                keywords = boost_item.get("keywords", [])
                boost = boost_item.get("boost", 0.0)
                if any(kw.lower() in section_lower for kw in keywords):
                    total_boost += boost
            # Check if section matches penalize_sections
            for penalize_item in rule.get("penalize_sections", []):
                keywords = penalize_item.get("keywords", [])
                penalty = penalize_item.get("penalty", 0.0)
                unless = penalize_item.get("unless_query_contains", [])
                if any(kw.lower() in section_lower for kw in keywords):
                    # If query contains 'unless' keywords, do not penalize
                    if not any(u.lower() in query_lower for u in unless):
                        total_boost += penalty  # penalty is negative
        return total_boost

    @staticmethod
    def _get_content_cap(score: float, cfg: dict) -> int:
        """Return content length cap based on page score tier."""
        if score >= 5.0:
            return cfg.get("merger_content_cap_top", 16000)
        elif score >= 2.0:
            return cfg.get("merger_content_cap_high", 10000)
        elif score >= 0.5:
            return cfg.get("merger_content_cap_medium", 6000)
        elif score >= 0.15:
            return cfg.get("merger_content_cap_low", 3000)
        else:
            return cfg.get("merger_content_cap_floor", 3000)

    def merge(self, results: List[SearchResult],
              max_context_chars: int = None,
              query: str = "",
              industry_hint: str = None) -> Tuple[str, List[Dict]]:
        cfg = settings.CONTEXT_CONFIG
        if max_context_chars is None:
            max_context_chars = cfg.get("merger_default_max_chars", 32000)
        if not results:
            return "", []

        # Load industry boost rules
        industry_rules = self._load_industry_boost_rules(industry_hint)

        merged_parts = []
        sources = []
        current_chars = 0

        doc_groups = {}
        for result in results:
            doc_groups.setdefault(result.doc_id, []).append(result)

        num_docs = len(doc_groups)
        min_chars_per_doc = cfg.get("merger_min_chars_per_doc", 5000)
        max_per_doc = max(max_context_chars // num_docs, min_chars_per_doc) if num_docs > 1 else max_context_chars

        # FIX: Section-level recall — for each document, use structure_index to expand the full page range of sections
        for doc_id, doc_results in list(doc_groups.items()):
            existing_pages = set(r.page_num for r in doc_results if r.page_num)
            if existing_pages and self.metadata_db:
                expanded = self._expand_section_pages(doc_id, existing_pages, self.metadata_db)
                # If fallback_db is available and primary db has no data, also try
                if not expanded and self.tenant_id == "admin" and self.fallback_metadata_db:
                    expanded = self._expand_section_pages(doc_id, existing_pages, self.fallback_metadata_db)
                for page_num, info in expanded.items():
                    if not info.get('raw_text'):
                        continue
                    expanded_r = SearchResult(
                        doc_id=doc_id, page_num=page_num,
                        content=info.get('raw_text', ''),
                        section_title=info.get('section_title', ''),
                        score=0.1,  # Low score to avoid crowding out high-relevance pages
                        text_source='section_expand'
                    )
                    doc_results.append(expanded_r)

        for doc_id, doc_results in doc_groups.items():
            # FIX: Apply penalty/boost FIRST, then filter, to avoid high-value pages
            # being filtered early due to low original score
            for r in doc_results:
                # FIX: Apply industry package boost rules (generic, supports both Chinese and English)
                if industry_rules:
                    industry_boost = self._match_boost_rule(query, r.section_title, industry_rules)
                    if industry_boost != 0.0:
                        r.score += industry_boost
                        logger.debug(f"[MERGER] industry boost: {r.section_title} +{industry_boost:.1f}")

            doc_results.sort(key=lambda x: x.score, reverse=True)

            # Filter low-score results (after boost/penalty)
            min_score_threshold = cfg.get("merger_min_score_threshold", 0.15)
            filtered_doc_results = [r for r in doc_results if r.score >= min_score_threshold]
            if not filtered_doc_results:
                filtered_doc_results = doc_results[:5]
            doc_results = filtered_doc_results

            # FIX: Hardcoded penalty for low-value pages (cover/TOC/copyright/package info)
            # to prevent them from ranking above spec pages.
            # These pages often have high FTS scores (contain many product names) but irrelevant content.
            for r in doc_results:
                section_lower = (r.section_title or "").lower()
                content_preview = (r.content or "")[:300].lower()
                # V5.0: Use configurable low-value section indicators + industry package rules
                # Generic indicators from config (cross-language document metadata pages)
                generic_indicators = settings.CONTEXT_CONFIG.get("low_value_section_indicators", [])
                low_value_keywords = list(generic_indicators)
                
                # Add industry-specific low-value sections from rules
                industry_rules = self._load_industry_boost_rules(industry_hint) if hasattr(self, '_load_industry_boost_rules') else {}
                if industry_rules and "low_value_sections" in industry_rules:
                    for rule in industry_rules.get("low_value_sections", []):
                        low_value_keywords.extend(rule.get("keywords", []))
                
                low_value_penalty = cfg.get("merger_low_value_penalty", 3.0)
                if any(kw in section_lower or kw in content_preview for kw in low_value_keywords):
                    old_score = r.score
                    r.score -= low_value_penalty  # Significantly lower low-value page priority
                    logger.warning(f"[MERGER] low-value page penalty: doc={r.doc_id} page={r.page_num} title={r.section_title} score={old_score:.2f} -> {r.score:.2f}")
            
            # Re-sort (after penalty)
            doc_results.sort(key=lambda x: x.score, reverse=True)

            if not doc_results:
                continue

            # Industry package merge phase enhancement (via enhance_context Hook)
            if industry_hint:
                try:
                    from ..plugins import get_plugin_registry
                    registry = get_plugin_registry()
                    plugin = registry.get_plugin(industry_hint)
                    if plugin and hasattr(plugin.retrieval, 'enhance_context'):
                        for r in doc_results:
                            r.content = plugin.retrieval.enhance_context(
                                query, r.content, []
                            )
                except Exception as e:
                    logger.debug(f"[MERGER] industry package enhance_context failed: {e}")

            # Sort by score; no longer force intro/info pages to the front.
            # Annual reports etc. have financial data on later pages; highest-score pages are the most relevant.

            # FIX: admin tenant tries primary tenant for document fetch, then fallback
            doc = self.metadata_db.get_document(doc_id)
            if not doc and self.tenant_id == "admin" and self.fallback_metadata_db:
                try:
                    doc = self.fallback_metadata_db.get_document(doc_id)
                except Exception:
                    pass
            doc_title = doc.get("title", doc_results[0].filename) if doc else doc_results[0].filename
            doc_header = f"\n\n===== Document: {doc_title} =====\n"
            if current_chars + len(doc_header) > max_context_chars:
                break
            merged_parts.append(doc_header)
            current_chars += len(doc_header)

            included_pages = []
            doc_chars_used = 0
            for result in doc_results:
                # FIX: Format-enhance plain-text lists in chapters like Block Diagram / System Bus Tree
                # Guard: some pages may have no extracted text (e.g., scanned/image-only pages),
                # downstream segmentation and truncation assume a string.
                content = self._enhance_list_content(result.content or "", result.section_title)
                
                # NEW: Smart content segmentation - identify multiple topic regions within a page.
                # When a page contains distinct sub-section headings, segment and label them
                # to help the LLM notice different topics.
                segmented_content = self._segment_page_content(content, result.section_title)
                if segmented_content != content:
                    content = segmented_content
                
                # FIX: Limit single-page content length to avoid one long page consuming the entire quota.
                # FIX: After context budget increased to 80K, proportionally relax single-page content cap.
                # FIX2: Dynamically allocate quota based on page score; higher-score pages get more content.
                max_content_len = self._get_content_cap(result.score, cfg)
                
                if len(content) > max_content_len:
                    # FIX: When truncating, prioritize preserving paragraphs containing query keywords
                    content = self._smart_truncate(content, query, max_content_len)
                section = f"\n--- {result.section_title} (Page {result.page_num}) ---\n" if result.section_title else f"\n--- Page {result.page_num} ---\n"
                total_len = len(section) + len(content)
                if current_chars + total_len > max_context_chars or doc_chars_used + total_len > max_per_doc:
                    if current_chars + total_len <= max_context_chars and doc_chars_used < max_per_doc:
                        remaining = min(max_per_doc - doc_chars_used - len(section) - 10,
                                       max_context_chars - current_chars - len(section) - 10)
                        if remaining > 100:
                            merged_parts.append(section)
                            merged_parts.append(content[:remaining] + "...")
                            included_pages.append(result.page_num)
                    break
                merged_parts.append(section)
                merged_parts.append(content)
                current_chars += total_len
                doc_chars_used += total_len
                included_pages.append(result.page_num)

            # Build source content summary (containing the text snippets actually sent to the LLM)
            source_content_parts = []
            for result in doc_results:
                if result.page_num in included_pages:
                    section_header = f"--- {result.section_title} (Page {result.page_num}) ---\n" if result.section_title else f"--- Page {result.page_num} ---\n"
                    # Use truncated content (consistent with what was sent to the LLM)
                    # Guard: treat missing page text as empty string to avoid downstream NoneType errors.
                    content = self._enhance_list_content(result.content or "", result.section_title)
                    segmented_content = self._segment_page_content(content, result.section_title)
                    if segmented_content != content:
                        content = segmented_content
                    # Limit single-page content length (consistent with merge logic)
                    max_content_len = self._get_content_cap(result.score, cfg)
                    if len(content) > max_content_len:
                        content = self._smart_truncate(content, query, max_content_len)
                    source_content_parts.append(section_header + content)
            
            source_content = "\n".join(source_content_parts)
            # Limit total content length returned to client to avoid oversized response
            max_source_content = cfg.get("merger_max_source_content", 8000)
            if len(source_content) > max_source_content:
                source_content = source_content[:max_source_content] + "\n...[content truncated]"

            sources.append({
                "doc_id": doc_id, "title": doc_title,
                "filename": doc_results[0].filename,
                "pages": list(set(p for p in included_pages if p)),
                "content": source_content,
            })

        result = "".join(merged_parts)
        # FIX: Final safety truncation to ensure returned context does not exceed max_context_chars
        if len(result) > max_context_chars:
            result = result[:max_context_chars]
            logger.warning(f"[MERGER] final truncation: {len(result)} -> {max_context_chars}")
        return result, sources
