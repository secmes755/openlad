"""
PHASE-2: Retrieval Executor
"""
import json
import logging
import os
import re
from typing import Any

from ..config import settings
from ..db.tenant_db import get_tenant_metadata_db
from ..models.client import get_model_client
from .retriever import HierarchicalRetriever, SearchResult, SegmentMerger
from .router import IntentType, QueryPlan

logger = logging.getLogger(__name__)


class RetrievalExecutor:
    def __init__(self, tenant_id: str = None):
        self.tenant_id = tenant_id
        self.retriever = HierarchicalRetriever(tenant_id)
        self.merger = SegmentMerger(tenant_id)
        self.metadata_db = get_tenant_metadata_db(tenant_id) if tenant_id else None
        self.industry_hint = None  # Initialized in execute()
        self.max_chars = settings.CONTEXT_CONFIG.get("phase2_max_chars", 160000)
        env_max = os.environ.get("OPENLAD_MAX_CHARS")
        if env_max:
            self.max_chars = int(env_max)
            logger.info(f"[PHASE-2] Environment variable overrides context quota: {self.max_chars}")

    def _get_query_expansion_keywords(self) -> list[str]:
        """Load query expansion keywords from industry packs. If industry_hint is not specified, iterate all industry packs to collect."""
        try:
            from ..plugins import get_plugin_registry
            registry = get_plugin_registry()
            # If industry hint is specified, prioritize loading the corresponding industry pack
            if self.industry_hint and self.industry_hint != "auto":
                plugin = registry.get_plugin(self.industry_hint)
                if not plugin:
                    plugin = registry.get_plugin_by_category(self.industry_hint)
                if plugin and hasattr(plugin.retrieval, 'get_query_expansion_keywords'):
                    return plugin.retrieval.get_query_expansion_keywords()
            # Otherwise iterate all industry packs, merge expansion keywords
            all_kws = []
            for pack_id in registry.list_plugins() if hasattr(registry, 'list_plugins') else []:
                plugin = registry.get_plugin(pack_id)
                if plugin and hasattr(plugin.retrieval, 'get_query_expansion_keywords'):
                    kws = plugin.retrieval.get_query_expansion_keywords()
                    all_kws.extend(kws)
            return list(dict.fromkeys(all_kws))  # Deduplicate while preserving order
        except Exception:
            pass
        return []

    def execute(self, plan: dict[str, Any], tenant_id: str = None,
                industry_hint: str = None, original_query: str = None) -> dict[str, Any]:
        if tenant_id and tenant_id != self.tenant_id:
            self.tenant_id = tenant_id
            self.retriever = HierarchicalRetriever(tenant_id)
            self.merger = SegmentMerger(tenant_id)
            self.metadata_db = get_tenant_metadata_db(tenant_id)

        self.industry_hint = industry_hint
        strategy = plan.get("strategy", "single_retrieve")
        steps = plan.get("steps", [])
        logger.info("[PHASE-2] ===== Retrieval Execution =====")
        logger.info(f"[PHASE-2] Strategy: {strategy}, Steps: {len(steps)}, Industry: {industry_hint or 'auto'}")

        if strategy == "decomposed_retrieve":
            return self._execute_decomposed(steps, original_query=original_query)
        return self._execute_standard(steps, strategy_label=strategy, original_query=original_query)

    def _execute_standard(self, steps: list[dict], strategy_label: str = "single_retrieve", original_query: str = None) -> dict[str, Any]:
        step_quotas = self._calculate_step_quotas(steps)
        all_results: list[SearchResult] = []
        trace: list[dict] = []
        total_step_chars = 0
        step_contexts: list[str] = []
        step_sources_all: list[dict] = []


        for i, step in enumerate(steps, 1):
            tool = step.get("tool", "single_retrieve")
            query = step.get("query", "")
            doc_filter = step.get("doc_filter", [])
            purpose = step.get("purpose", "")
            step_quota = step_quotas[i - 1]

            # FIX: Ensure doc_filter only matches entities from the current step
            resolved_filter = self._resolve_doc_filter(doc_filter)
            if not resolved_filter and doc_filter:
                logger.warning(f"[PHASE-2] Step {i} doc_filter {doc_filter} did not match any documents, using original query for retrieval")

            step_results = self._execute_step(tool, query, resolved_filter, purpose, step_quota, original_query=original_query)
            step_trace = {"step": i, "tool": tool, "query": query, "doc_filter": doc_filter, "purpose": purpose, "quota": step_quota, "results_count": len(step_results)}
            merge_quota = min(step_quota, self.max_chars - total_step_chars)
            step_context, step_sources = self.merger.merge(step_results, max_context_chars=merge_quota, query=query, industry_hint=self.industry_hint)
            # FIX: Safety truncation, ensure context does not exceed quota (merger.merge may have imprecise truncation issues)
            if len(step_context) > step_quota:
                step_context = step_context[:step_quota]
                logger.warning(f"[PHASE-2] Step {i} context safety truncation: {len(step_context)} -> {step_quota}")
            step_trace["context_chars"] = len(step_context)
            step_trace["sources"] = step_sources
            trace.append(step_trace)
            total_step_chars += len(step_context)
            step_contexts.append(step_context)
            step_sources_all.extend(step_sources)
            all_results.extend(step_results)

            if total_step_chars >= self.max_chars:
                logger.warning(f"[PHASE-2] Context reached maximum {self.max_chars}, skipping remaining steps")
                break

        # FIX: For single-step queries (single_retrieve, etc.), directly use the truncated step_context,
        # avoiding re-merge that could bloat the context and trigger Map-Reduce.
        # For multi-step queries (decomposed_retrieve), concatenate each step's context.
        if step_contexts:
            if len(steps) == 1:
                final_context = step_contexts[0]
                final_sources = step_sources_all
            else:
                final_context = "".join(step_contexts)
                final_sources = step_sources_all
        else:
            final_context = ""
            final_sources = []

        # Proportional truncation: when context exceeds synthesis budget,
        # allocate space proportionally across steps rather than blindly
        # discarding the tail (which was the old behavior and could silently
        # drop an entire document in multi-document comparisons).
        cfg = settings.CONTEXT_CONFIG
        context_budget = cfg.get("synthesis_context_budget", 39000)
        if len(final_context) > context_budget:
            if len(step_contexts) > 1:
                # Multi-step: proportional allocation per step, min 500 chars each
                ratio = context_budget / len(final_context)
                truncated = []
                for sc in step_contexts:
                    keep = max(int(len(sc) * ratio), 500)
                    truncated.append(sc[:keep])
                final_context = "".join(truncated)
                logger.info(
                    f"[PHASE-2] Proportional truncation: {sum(len(sc) for sc in step_contexts)} "
                    f"-> {len(final_context)} (ratio={ratio:.2f}, {len(step_contexts)} steps)"
                )
            else:
                # Single-step: tail truncation is lossless (only one source)
                final_context = final_context[:context_budget]
                logger.warning(f"[PHASE-2] Single-step context truncation: -> {context_budget}")

        logger.info("[PHASE-2] ===== Retrieval Complete =====")
        logger.info(f"[PHASE-2] Total results: {len(all_results)}, Context: {len(final_context)} chars")
        return {"context": final_context, "sources": final_sources, "trace": trace, "total_results": len(all_results), "total_chars": len(final_context), "strategy": strategy_label}

    def _execute_decomposed(self, steps: list[dict], original_query: str = None) -> dict[str, Any]:
        """
        Step-by-step retrieval + structured extraction. After each step's retrieval, use LLM to distill into JSON.
        The final Synthesizer only processes the structured summary, not raw page text.
        """
        step_quotas = self._calculate_step_quotas(steps)
        all_results: list[SearchResult] = []
        trace: list[dict] = []
        structured_parts = []
        get_model_client()

        for i, step in enumerate(steps, 1):
            tool = step.get("tool", "single_retrieve")
            query = step.get("query", "")
            doc_filter = step.get("doc_filter", [])
            purpose = step.get("purpose", "")
            step_quota = step_quotas[i - 1]

            # FIX: Ensure doc_filter only matches entities from the current step, avoiding cross-step contamination
            resolved_filter = self._resolve_doc_filter(doc_filter)
            if not resolved_filter and doc_filter:
                logger.warning(f"[PHASE-2] Step {i} doc_filter {doc_filter} did not match any documents, using original query for retrieval")

            # 1. Retrieve
            # If query is a list (sub-queries from decomposed_retrieve), retrieve, merge, and extract each sub-query separately
            # to avoid data loss for an entity due to truncation after merging
            sub_queries = query if isinstance(query, list) else [query]
            step_extracted_parts = []
            step_sources = []
            step_results_all = []  # FIX: Isolate results for this step
            for sub_i, sub_q in enumerate(sub_queries, 1):
                # FIX: If industry pack has query expansion keywords configured, enhance overly simple sub-queries
                original_sub_q = sub_q
                if len(sub_q.split()) <= 3:
                    expansion_kws = self._get_query_expansion_keywords()
                    if expansion_kws:
                        expanded = f"{sub_q} {' '.join(expansion_kws)}"
                        logger.info(f"[PHASE-2] Step {i} sub-query {sub_i} expanded: '{original_sub_q}' -> '{expanded}'")
                        sub_q = expanded

                # Match sub-query to specific documents by year keywords in the sub-query
                sub_filter = self._match_subquery_to_docs(sub_q, resolved_filter)
                if sub_filter:
                    logger.info(f"[PHASE-2] Step {i} sub-query {sub_i} matched trusted documents: {[d[:8] for d in sub_filter]}")
                sub_results = self._execute_step("single_retrieve", sub_q, sub_filter, purpose, step_quota, original_query=original_query)

                # Supplementary retrieval: use the same sub_filter
                # No longer hardcode financial keywords to trigger supplementary retrieval; let original sub-query naturally recall related content

                cfg = settings.CONTEXT_CONFIG
                sub_merge_cap = cfg.get("decomposed_sub_merge_cap", 30000)
                sub_context, sub_sources = self.merger.merge(sub_results, max_context_chars=min(step_quota, sub_merge_cap), query=sub_q, industry_hint=self.industry_hint)
                # FIX: Safety truncation, ensure sub-query context does not exceed quota
                if len(sub_context) > step_quota:
                    sub_context = sub_context[:step_quota]
                    logger.warning(f"[PHASE-2] Step {i} sub-query {sub_i} context safety truncation: {len(sub_context)} -> {step_quota}")
                step_results_all.extend(sub_results)  # FIX: Only collect results for this step
                all_results.extend(sub_results)      # FIX: Fix total_results reporting
                # Merge sub-query sources into step_trace
                for s in sub_sources:
                    existing = next((x for x in step_sources if x.get("doc_id") == s.get("doc_id")), None)
                    if existing:
                        existing["pages"] = list(set(existing.get("pages", []) + s.get("pages", [])))
                    else:
                        step_sources.append(s)

                if sub_context:
                    # Use raw context directly, not via LLM extraction (avoid losing critical data like revenue/profit figures)
                    sub_extracted = sub_context
                    logger.info(f"[PHASE-2] Step {i} sub-query {sub_i} using raw context ({len(sub_context)} chars)")
                    step_extracted_parts.append(f"### Sub-query {sub_i}: {sub_q}\n{sub_extracted}\n")
                else:
                    step_extracted_parts.append(f"### Sub-query {sub_i}: {sub_q}\n(No relevant content retrieved)\n")

            step_trace = {"step": i, "tool": tool, "query": query, "doc_filter": doc_filter,
                          "purpose": purpose, "quota": step_quota, "results_count": len(step_results_all)}
            step_trace["context_chars"] = sum(len(p) for p in step_extracted_parts)
            step_trace["sources"] = step_sources
            trace.append(step_trace)

            structured_parts.append(f"## Step {i}: {purpose or (query[0] if isinstance(query, list) else query)}\n" + "\n".join(step_extracted_parts))

        # 4. Aggregate structured data
        final_context = "\n".join(structured_parts)
        # Proportional truncation: decomposed path content has been LLM-extracted,
        # so it's higher-density than raw context. Use same budget as standard path.
        cfg = settings.CONTEXT_CONFIG
        context_budget = cfg.get("synthesis_context_budget", 39000)
        if len(final_context) > context_budget:
            # Proportional allocation across steps, min 300 chars each
            # (decomposed steps are already summarized, can be more compact)
            ratio = context_budget / len(final_context)
            truncated_parts = []
            for sp in structured_parts:
                keep = max(int(len(sp) * ratio), 300)
                truncated_parts.append(sp[:keep])
            final_context = "\n".join(truncated_parts)
            logger.info(
                f"[PHASE-2] Decomposed proportional truncation: "
                f"{sum(len(sp) for sp in structured_parts)} -> {len(final_context)} "
                f"(ratio={ratio:.2f}, {len(structured_parts)} steps)"
            )

        # 5. Source aggregation (FIX: Only aggregate each step's own sources, no cross-step merging)
        seen_sources = {}
        for t in trace:
            for s in t.get("sources", []):
                doc_id = s.get("doc_id")
                if doc_id and doc_id not in seen_sources:
                    seen_sources[doc_id] = s
                elif doc_id:
                    existing = seen_sources[doc_id]
                    existing["pages"] = list(set(existing.get("pages", []) + s.get("pages", [])))
        final_sources = list(seen_sources.values())

        logger.info("[PHASE-2] ===== Decomposed Retrieval Complete =====")
        logger.info(f"[PHASE-2] Total results: {len(all_results)}, Structured context: {len(final_context)} chars")
        return {"context": final_context, "sources": final_sources, "trace": trace,
                "total_results": len(all_results), "total_chars": len(final_context),
                "strategy": "decomposed_retrieve"}

    def _extract_step_data(self, context: str, query: str, model_client) -> str:
        """
        Extract key data from retrieval context. Let LLM autonomously decide what to extract based on query objectives and document content.
        No predefined fields or keywords in the code.
        """
        if not context or len(context) < 200:
            return context

        ctx_len = len(context)
        cfg = settings.CONTEXT_CONFIG
        sample_size = cfg.get("extraction_sample_size", 12000)
        fragment_size = cfg.get("extraction_fragment_size", 3000)
        if ctx_len <= sample_size:
            sample = context
        else:
            # Uniform sampling: start, middle, end
            parts = []
            parts.append(f"=== Document Start ===\n{context[:fragment_size]}")

            # Uniform sampling of the middle portion
            mid_start = ctx_len // 3
            mid_end = 2 * ctx_len // 3
            parts.append(f"=== Document Middle ===\n{context[mid_start:mid_start+fragment_size]}")
            parts.append(f"=== Document Later ===\n{context[mid_end:mid_end+fragment_size]}")

            # Tail
            parts.append(f"=== Document End ===\n{context[-fragment_size:]}")
            sample = "\n".join(parts)

        prompt = f"""Extract key data related to the query objective from the following document fragments. Only output structured results, no explanations.

Query objective: {query}

Extraction principles (general, no preset fields):
1. Read the document fragments and determine what type of data they contain
2. For each relevant entity/subject, extract its key attributes and values
3. Annotate each data point with: value, unit (if any), condition/version (if any), source page
4. If a piece of data that should exist is not found, explicitly mark as "Not found"
5. Do not analyze or compare; only extract raw data

Document fragments:
{sample}

Output structure (JSON):
{{
  "entities": [
    {{
      "name": "Entity name/subject identifier",
      "attributes": [
        {{
          "attribute": "Attribute name",
          "value": "Value or content",
          "unit": "Unit (if any)",
          "condition": "Condition/version/year (if any)",
          "page": "Page x"
        }}
      ]
    }}
  ],
  "notes": "Other key information"
}}
"""
        try:
            extraction_max_tokens = cfg.get("extraction_max_tokens", 4096)
            result = model_client.generate(prompt, max_tokens=extraction_max_tokens, temperature=0.1)
            if result and result.strip():
                if result.strip().startswith("{"):
                    try:
                        parsed = json.loads(result)
                        return json.dumps(parsed, ensure_ascii=False, indent=2)
                    except json.JSONDecodeError:
                        pass
                return result.strip()
        except Exception as e:
            logger.warning(f"[PHASE-2] Structured extraction failed: {e}")

        cfg = settings.CONTEXT_CONFIG
        fallback_limit = cfg.get("extraction_fallback_limit", 3000)
        return context[:fallback_limit]

    def _match_subquery_to_docs(self, sub_query: str, doc_filter: list[str]) -> list[str]:
        """
        Match sub-query to specific documents.
        Simple implementation: extract year and model name from sub-query, match against document title/filename.
        """
        import re
        if not doc_filter or len(doc_filter) <= 1:
            return doc_filter

        keywords = []

        year_match = re.search(r'(20\d{2})', sub_query)
        if year_match:
            keywords.append(year_match.group(1))

        model_match = re.findall(r'\b[A-Z]{1,}[\s-]?\d{2,}[A-Z]*\b', sub_query)
        for m in model_match:
            clean = m.replace(' ', '').replace('-', '')
            if clean not in keywords and len(clean) >= 3:
                keywords.append(clean)

        if not keywords:
            return doc_filter

        matched = []
        for doc_id in doc_filter:
            doc = self.metadata_db.get_document(doc_id)
            if not doc:
                continue
            filename = (doc.get('filename') or '').lower()
            title = (doc.get('title') or '').lower()
            matched_count = 0
            for kw in keywords:
                kw_lower = kw.lower()
                if kw_lower in filename or kw_lower in title:
                    matched_count += 1
            need_matches = min(2, len(keywords))
            if matched_count >= need_matches:
                matched.append(doc_id)

        if matched:
            logger.info(f"[PHASE-2] sub-query '{sub_query[:40]}...' matched {len(matched)}/{len(doc_filter)} documents")
            return matched

        return doc_filter

    def _calculate_step_quotas(self, steps: list[dict]) -> list[int]:
        if not steps:
            return []
        doc_counts = []
        no_filter_indices = []
        for i, step in enumerate(steps):
            doc_filter = step.get("doc_filter", [])
            doc_id_filter = self._resolve_doc_filter(doc_filter, silent=True)
            count = len(doc_id_filter) if doc_id_filter else 0
            doc_counts.append(count)
            if count == 0:
                no_filter_indices.append(i)
        total_docs = sum(doc_counts)
        quotas = [0] * len(steps)
        cfg = settings.CONTEXT_CONFIG
        filtered_ratio = cfg.get("filtered_steps_quota_ratio", 0.8)
        if total_docs > 0:
            filtered_quota = int(self.max_chars * filtered_ratio)
            for i, count in enumerate(doc_counts):
                if count > 0:
                    quotas[i] = int(filtered_quota * count / total_docs)
        if no_filter_indices:
            remaining = self.max_chars - sum(quotas)
            base = remaining // len(no_filter_indices)
            for i in no_filter_indices:
                quotas[i] = base
        cfg = settings.CONTEXT_CONFIG
        min_step_quota = cfg.get("min_step_quota", 5000)
        min_single_step_quota = cfg.get("min_single_step_quota", 20000)
        min_quota = min(min_step_quota, self.max_chars // max(len(steps), 1))
        # For decomposed retrieval, if there's only one step, allow more quota
        if len(steps) == 1:
            min_quota = min(min_single_step_quota, self.max_chars)
        for i in range(len(quotas)):
            if quotas[i] < min_quota:
                quotas[i] = min_quota
        # FIX: Boost single-step query quota cap (configurable, default 80K for 128K context models)
        # Cross-document comparison needs enough space for complete chapters from multiple documents simultaneously
        # Previous limit of 32000 resulted in only 16K per document for cross-document queries, causing chapter content truncation
        cfg = settings.CONTEXT_CONFIG
        single_step_max = cfg.get("single_step_quota_max", 80000)
        if len(steps) == 1:
            for i in range(len(quotas)):
                if quotas[i] > single_step_max:
                    quotas[i] = single_step_max
        # FIX: Multi-step query total quota cap (configurable)
        # Step-by-step retrieval merges each step independently, total context is controllable
        multi_step_max = cfg.get("multi_step_quota_max", 80000)
        total_quota = sum(quotas)
        if total_quota > multi_step_max:
            scale = multi_step_max / total_quota
            for i in range(len(quotas)):
                quotas[i] = int(quotas[i] * scale)
            logger.info(f"[PHASE-2] Multi-step query total quota {total_quota} exceeds {multi_step_max}, proportionally scaled to {quotas}")
        return quotas

    @staticmethod
    def _clean_query_for_retrieval(query: str) -> str:
        """
        Simplified query cleaning — preserves original query, only does basic cleanup.
        No longer hardcodes removal of intent phrases/stop words; let LLM understand full query semantics.
        """
        import re
        if not query:
            return query

        # Only clean up extra spaces and punctuation, preserve all semantic words
        cleaned = re.sub(r'[^\w\s\u4e00-\u9fff]', ' ', query)
        cleaned = re.sub(r'\s+', ' ', cleaned).strip()

        if cleaned != query:
            logger.info(f"[QUERY-CLEAN] '{query[:60]}...' -> '{cleaned[:60]}...'")
        return cleaned

    def _execute_step(self, tool: str, query: str, doc_filter: list[str],
                      purpose: str,
                      max_context_quota: int = None,
                      original_query: str = None) -> list[SearchResult]:
        if tool == "single_retrieve":
            return self._single_retrieve(query, doc_filter, max_context_quota, original_query=original_query)
        elif tool == "decomposed_retrieve":
            if isinstance(query, list):
                all_results = []
                for q in query:
                    all_results.extend(self._single_retrieve(q, doc_filter, max_context_quota, original_query=original_query))
                return all_results
            return self._single_retrieve(query, doc_filter, max_context_quota, original_query=original_query)
        elif tool == "fulltext_retrieve":
            return self._fulltext_retrieve(doc_filter, query, max_context_quota, original_query=original_query)
        elif tool == "filtered_retrieve":
            return self._filtered_retrieve(query, doc_filter, max_context_quota, original_query=original_query)
        else:
            return self._single_retrieve(query, doc_filter, max_context_quota, original_query=original_query)

    @staticmethod
    def _build_exact_match_excerpt(raw_text: str, tokens: list[str],
                                   window_chars: int, max_windows: int):
        """Build an excerpt of windows around exact keyword hits in a page.

        Returns (excerpt, used_table_rows). Generic presentation aid: rows/lines
        containing the exact query identifier are lifted to the top of the
        delivered content so they survive context truncation and stay salient
        to the LLM. Hits inside markdown table rows are preferred because
        recovered tables carry structured facts, whereas broken plain-text/OCR
        regions often pair values incorrectly and mislead the LLM.
        """
        if not raw_text or not tokens:
            return "", False
        raw_lower = raw_text.lower()
        hits = []
        for tok in tokens:
            t = tok.lower()
            if not t:
                continue
            start = 0
            while True:
                i = raw_lower.find(t, start)
                if i < 0:
                    break
                hits.append(i)
                start = i + len(t)
        if not hits:
            return "", False
        hits = sorted(set(hits))

        def _is_table_hit(pos: int) -> bool:
            ls = raw_text.rfind("\n", 0, pos)
            le = raw_text.find("\n", pos)
            if le < 0:
                le = len(raw_text)
            return "|" in raw_text[ls + 1:le]

        table_hits = [p for p in hits if _is_table_hit(p)]
        used_table = bool(table_hits)
        if table_hits:
            hits = table_hits

        # Merge hit positions into windows
        windows = []
        cur = None
        for pos in hits:
            a = max(0, pos - window_chars // 2)
            b = min(len(raw_text), pos + window_chars // 2)
            if cur is None or a > cur[1]:
                if cur is not None:
                    windows.append(tuple(cur))
                cur = [a, b]
            else:
                cur[1] = max(cur[1], b)
        if cur is not None:
            windows.append(tuple(cur))
        parts = [raw_text[a:b] for a, b in windows[:max_windows]]
        return ("[Exact keyword match: " + ", ".join(tokens[:5]) + "]\n"
                + "\n...\n".join(parts) + "\n"), used_table

    def _apply_rare_token_rescue(self, results: list[SearchResult], query: str,
                                 doc_id_filter) -> list[SearchResult]:
        """Guarantee pages containing rare query identifiers are present and prominent.

        Generic safety net for exact-lookup questions (pin names, register names,
        part numbers, codes): a token that is rare in the structure index is
        highly discriminating, so any page containing it verbatim is strong
        evidence. Such pages are (a) appended when missing from the results,
        (b) score-boosted when present, and (c) prefixed with an excerpt around
        the exact keyword hits. Applies to every retrieval path that funnels
        through _single_retrieve (chapter retrieval, FTS retrieval, fulltext
        and decomposed sub-queries).
        """
        cfg = settings.CONTEXT_CONFIG
        if not cfg.get("rare_token_rescue_enabled", True):
            return results
        if not self.metadata_db or not query:
            return results

        try:
            query_keywords = self.retriever._tokenize_query(query.lower())
        except Exception:
            return results
        if not query_keywords:
            return results

        rare_max_df = cfg.get("rare_token_max_structure_df", 2)
        rare_max_tokens = cfg.get("rare_token_max_tokens", 5)
        rare_max_pages = cfg.get("rare_token_max_pages", 16)
        rare_max_rescued = cfg.get("rare_token_max_rescued_pages", 12)
        rescue_score = cfg.get("rare_token_rescue_score", 45.0)
        window_chars = cfg.get("exact_match_window_chars", 1500)
        max_windows = cfg.get("exact_match_max_windows", 4)

        def _identifier_rank(tok: str):
            # Identifier-like: contains '_' or mixes letters and digits
            has_mix = ("_" in tok) or (
                any(c.isdigit() for c in tok) and any(c.isalpha() for c in tok))
            return (0 if has_mix else 1, -len(tok))

        # Determine target documents
        if doc_id_filter and "__ALL__" not in doc_id_filter:
            target_docs = list(doc_id_filter)
        else:
            target_docs = list(dict.fromkeys(r.doc_id for r in results))
        if not target_docs:
            return results

        # Collect pages containing discriminating tokens, per document.
        # Token policy:
        # - identifier-like tokens (contain '_' or mix letters+digits, e.g. pin
        #   names, register names, part numbers) are ALWAYS scanned; the page-hit
        #   flood guard alone decides selectivity. This avoids false negatives
        #   where a token appears in several section summaries (structure df > 2)
        #   but still hits only a few pages in the full text.
        # - other tokens are scanned only when rare in the structure index
        #   (df <= rare_max_df) AND long enough to be meaningful (>=5 chars);
        #   short generic tokens ("ball", "pin", "clk", "tx") are noisy
        #   substrings that rescue large amounts of irrelevant pages.
        per_doc_pages = {}   # doc_id -> set(page_num)
        page_tokens = {}     # (doc_id, page_num) -> [tokens]
        token_hit_counts = {}  # (doc_id, token) -> page-hit count (selectivity)
        for doc_id in target_docs:
            tokens_to_scan = []
            for kw in query_keywords:
                if not kw:
                    continue
                is_identifier = _identifier_rank(kw)[0] == 0
                if not is_identifier:
                    if len(kw) < 5:
                        continue
                    try:
                        df = len(self.metadata_db.search_structure_index(doc_id, kw))
                    except Exception:
                        df = 0
                    if df > rare_max_df:
                        continue
                tokens_to_scan.append(kw)
            tokens_to_scan = sorted(set(tokens_to_scan), key=_identifier_rank)[:rare_max_tokens]
            for kw in tokens_to_scan:
                try:
                    hit_pages = self.metadata_db.find_pages_containing(
                        doc_id, kw, limit=rare_max_pages + 1)
                except Exception:
                    hit_pages = []
                if not hit_pages or len(hit_pages) > rare_max_pages:
                    continue  # no hits, or too common in page text to discriminate
                token_hit_counts[(doc_id, kw)] = len(hit_pages)
                for pn in hit_pages:
                    per_doc_pages.setdefault(doc_id, set()).add(pn)
                    page_tokens.setdefault((doc_id, pn), []).append(kw)

        if not per_doc_pages:
            return results

        # Build excerpts and rank rescued pages by evidence strength:
        # more matched tokens first, then table-row excerpts (structured facts)
        # before plain-text excerpts, then token selectivity, then page order.
        doc_pages_cache = {}
        evidence = []  # (doc_id, pn, tokens, excerpt, used_table, selectivity)
        for (doc_id, pn), tokens in page_tokens.items():
            if doc_id not in doc_pages_cache:
                try:
                    doc_pages_cache[doc_id] = {
                        p.get("page_num"): p
                        for p in (self.metadata_db.get_document_pages(doc_id) or [])}
                except Exception:
                    doc_pages_cache[doc_id] = {}
            raw = doc_pages_cache[doc_id].get(pn, {}).get("raw_text", "") or ""
            excerpt, used_table = self._build_exact_match_excerpt(
                raw, tokens, window_chars, max_windows)
            if not excerpt:
                continue
            selectivity = sum(1.0 / token_hit_counts.get((doc_id, t), 1) for t in tokens)
            evidence.append((doc_id, pn, tokens, excerpt, used_table, selectivity))

        evidence.sort(key=lambda e: (-len(e[2]), not e[4], -e[5], e[0], e[1]))
        evidence = evidence[:rare_max_rescued]
        if not evidence:
            return results

        existing = {(r.doc_id, r.page_num): r for r in results}
        appended = boosted = 0
        docs_touched = set()
        for doc_id, pn, tokens, excerpt, used_table, selectivity in evidence:
            docs_touched.add(doc_id)
            key = (doc_id, pn)
            if key in existing:
                r = existing[key]
                if r.score < rescue_score:
                    r.score = rescue_score
                    boosted += 1
                if excerpt and not (r.content or "").startswith("[Exact keyword match"):
                    r.content = excerpt + (r.content or "")
            else:
                p = doc_pages_cache.get(doc_id, {}).get(pn)
                if not p:
                    continue
                doc = self.metadata_db.get_document(doc_id)
                # Excerpt-only content: compact, high-signal, budget-safe.
                # Full-page content would flood the merger budget when
                # several pages are rescued at once.
                results.append(SearchResult(
                    doc_id=doc_id, page_id=p.get("id"), page_num=pn,
                    score=rescue_score + len(tokens),
                    content=excerpt,
                    section_title=p.get("section_title", ""),
                    filename=doc.get("filename", "") if doc else "",
                    title=doc.get("title", "") if doc else "",
                    text_source=p.get("text_source", "direct_extract"),
                    page_image_path=p.get("page_image_path"),
                    extra_data={"exact_match_rescue": True},
                ))
                appended += 1
        if appended or boosted:
            logger.info(
                f"[EXECUTOR] rare-token guarantee: appended={appended} boosted={boosted} "
                f"docs={len(docs_touched)}")
        return results

    def _single_retrieve(self, query: str, doc_filter: list[str],
                         max_context_quota: int = None, original_query: str = None) -> list[SearchResult]:
        doc_id_filter = self._resolve_doc_filter(doc_filter)
        cfg = settings.CONTEXT_CONFIG
        max_per_doc = cfg.get("max_results_per_doc", 15)
        multi_doc_multiplier = cfg.get("max_results_multi_doc_multiplier", 1)


        # NEW: Coarse recall + full delivery based on structure index
        # When query explicitly specifies documents (doc_id_filter),
        # first use structure index to locate relevant chapters, then send all pages in the chapter to LLM
        if doc_id_filter and len(doc_id_filter) == 1:
            # Use larger quota to ensure complete chapter content is delivered to LLM
            cfg = settings.CONTEXT_CONFIG
            chapter_fallback = cfg.get("chapter_fallback_quota", 32000)
            chapter_quota = max_context_quota if max_context_quota else chapter_fallback
            chapter_results = self._chapter_retrieve(query, doc_id_filter[0], chapter_quota)
            if chapter_results:
                logger.info(f"[SINGLE-RETRIEVE] Structure index coarse recall: query '{query}' -> recalled {len(chapter_results)} pages, total chars {sum(len(r.content) for r in chapter_results)}")
                return self._apply_rare_token_rescue(chapter_results, query, {doc_id_filter[0]})

        # FIX: Clean query, strip intent words to make embedding/FTS retrieval more precise
        search_query = self._clean_query_for_retrieval(query)

        plan = QueryPlan(intent=IntentType.EXACT_LOOKUP, raw_query=search_query, entities=[], deep_explore=False, industry_hint=self.industry_hint)
        cfg = settings.CONTEXT_CONFIG
        avg_page_chars = cfg.get("avg_page_chars", 1200)
        min_pages = cfg.get("min_pages_per_doc", 5)
        max_pages = cfg.get("max_pages_per_doc", 60)
        if doc_id_filter and max_context_quota:
            num_docs = len(doc_id_filter)
            quota_per_doc = max_context_quota // num_docs
            calculated_pages = quota_per_doc // avg_page_chars
            max_results = max(min(calculated_pages, max_pages), min_pages)
            # FIX: For single-document queries, even with large quota, limit max_results to avoid noise
            if num_docs == 1 and max_results > max_per_doc:
                max_results = max_per_doc
            # FIX: Multi-document queries also limit max_results to prevent context overload causing garbled LLM output
            elif num_docs > 1 and max_results > (max_per_doc * num_docs * multi_doc_multiplier):
                max_results = max_per_doc * num_docs * multi_doc_multiplier
        elif doc_id_filter:
            max_results = 20 if len(doc_id_filter) <= 2 else max_per_doc  # OPT: Lower cap, improve quality
            # FIX: Uniformly limit single-document queries to max_per_doc, avoid recalling too many pages and triggering Map-Reduce
            if len(doc_id_filter) == 1 and max_results > max_per_doc:
                max_results = max_per_doc
        else:
            max_results = max_per_doc  # OPT: Global cap lowered

        # Multi-document query: retrieve per document to avoid one document's high-score results occupying all quota and excluding other documents
        if doc_id_filter and len(doc_id_filter) > 1:
            all_results = []
            per_doc_max = max(8, max_results // len(doc_id_filter))  # Per-document cap, ensure fair distribution
            for doc_id in doc_id_filter:
                doc_results = self.retriever.retrieve(
                    query=search_query, plan=plan, max_results=per_doc_max,
                    explicit_doc_filter={doc_id},
                    max_context_quota=max_context_quota
                )
                all_results.extend(doc_results)
            # Deduplicate and sort by score
            seen = {}
            for r in all_results:
                key = (r.doc_id, r.page_id)
                if key not in seen or r.score > seen[key].score:
                    seen[key] = r
            merged = sorted(seen.values(), key=lambda x: x.score, reverse=True)
            merged = self._apply_rare_token_rescue(merged, search_query, set(doc_id_filter))
            return sorted(merged, key=lambda x: x.score, reverse=True)

        results = self.retriever.retrieve(query=search_query, plan=plan, max_results=max_results,
                                        explicit_doc_filter=set(doc_id_filter) if doc_id_filter else None,
                                        max_context_quota=max_context_quota)
        return self._apply_rare_token_rescue(
            results, search_query, set(doc_id_filter) if doc_id_filter else None)

    def _chapter_retrieve(self, query: str, doc_id: str, max_context_quota: int = None) -> list[SearchResult]:
        """
        LLM-based chapter selection + full delivery

        Design philosophy:
        - No content-related hardcoded rules in the code
        - Let LLM make all judgments: which chapters to select, what information to find
        - Retrieval layer only responsible for: delivering the right content to LLM

        Flow:
        1. Get document chapter structure
        2. Send query + chapter structure to LLM, let LLM select relevant chapters
        3. Return complete content of selected chapters
        """
        if not self.metadata_db:
            return []

        # 1. Get document info and chapter structure
        doc = self.metadata_db.get_document(doc_id)
        if not doc:
            return []

        chapters = self.metadata_db.get_structure_index(doc_id)
        if not chapters:
            # No structure index, fallback to FTS
            return self._fallback_to_fts(query, doc_id)

        # 2. Build chapter list (send all to LLM, let LLM filter)
        chapter_list = []
        for ch in chapters:  # All chapters, no quantity limit
            title = ch.get("section_title", "")
            summary = ch.get("summary", "") or ""
            start = ch.get("start_page", 0)
            end = ch.get("end_page", 0)
            # 128K context, can provide full summaries (avg 426 chars)
            chapter_list.append({
                "index": len(chapter_list),
                "title": title,
                "pages": f"{start}-{end}" if end > start else str(start),
                "summary": summary,
                "keywords": ch.get("keywords", "") or "",
                "entities": ch.get("entities", "") or ""
            })

        if not chapter_list:
            return self._fallback_to_fts(query, doc_id)

        # FIX: Ensure structure-index semantic matches are not missed by LLM title-only selection.
        # A chapter whose summary, keywords or harvested entities explicitly mentions the query
        # topic (e.g. "NPU", "UART") should be included even if its title does not contain the
        # query keyword. This is a generic, content-agnostic recall mechanism.
        def _chapter_match_score(ch: dict, q: str) -> int:
            """Return number of query tokens matched in title/keywords/summary/entities (0 = no match)."""
            if not q:
                return 0
            q_lower = q.lower()
            tokens = []
            for m in re.finditer(r'[\u4e00-\u9fff]{2,}', q_lower):
                tokens.append(m.group())
            for m in re.finditer(r'[A-Za-z0-9]{2,}', q_lower):
                token = m.group()
                if not re.match(r'^\d+$', token):
                    tokens.append(token)
            text = f"{ch.get('title', '')} {ch.get('keywords', '')} {ch.get('summary', '')} {ch.get('entities', '')}".lower()
            hit = 0
            for t in tokens:
                if re.fullmatch(r'[a-z0-9]+', t):
                    # ASCII tokens use word-boundary matching to avoid false hits
                    # (e.g. query token "ai" must not match "SAI" or "available")
                    if re.search(r'\b' + re.escape(t) + r'\b', text):
                        hit += 1
                elif t in text:
                    hit += 1
            return hit

        scored = [(ch["index"], _chapter_match_score(ch, query)) for ch in chapter_list]
        scored = [(idx, s) for idx, s in scored if s > 0]
        # Keep ALL semantically matching chapters (not just the top few), so a
        # low-score but directly-on-topic chapter (e.g. query token matched only
        # in its title) cannot be dropped. Cap only when noise explodes.
        scored.sort(key=lambda x: x[1], reverse=True)
        if len(scored) > 30:
            scored = scored[:30]
        preselected_indices = {idx for idx, _ in scored}
        if preselected_indices:
            logger.info(f"[CHAPTER-RETRIEVE] Pre-selected {len(preselected_indices)} chapters via structure-index semantic match: "
                        f"{[chapter_list[i]['title'] for i in sorted(preselected_indices)][:5]}")

        # 3. Let LLM select relevant chapters
        model_client = get_model_client()

        prompt = f"""You are a document retrieval assistant. The user wants to find specific information in a document.

User query: "{query}"

Document: "{doc.get('title', '')}"

The document contains the following chapters (title + page numbers):
"""
        # Send ALL chapter titles (cheap) plus full previews only for the
        # semantically pre-selected chapters. Large documents (e.g. 800+
        # chapters in an annual report) would otherwise blow past the model
        # context window when every summary is included, truncating the list
        # and hiding the exact chapter the query needs.
        for ch in chapter_list:
            prompt += f"\n[{ch['index']}] {ch['title']} (pages {ch['pages']})"
            if ch["index"] in preselected_indices and ch["summary"]:
                prompt += f"\n    Preview: {ch['summary']}"

        cfg = settings.CONTEXT_CONFIG
        chapter_select_max = cfg.get("chapter_select_max", 20)

        prompt += f"""

From the above {len(chapter_list)} chapters, select **up to {chapter_select_max} chapters most likely to contain the answer**.
Only return chapter indices in the following format:
{{"relevant_chapters": [0, 1, 2]}}

If there are no relevant chapters, return:
{{"relevant_chapters": []}}

Output only JSON, no explanation."""

        try:
            cfg = settings.CONTEXT_CONFIG
            chapter_select_max_tokens = cfg.get("chapter_select_max_tokens", 1024)
            result = model_client.generate_json(prompt, max_tokens=chapter_select_max_tokens, temperature=0.1)
            selected_indices = result.get("relevant_chapters", []) if result else []
        except Exception as e:
            logger.warning(f"[CHAPTER-RETRIEVE] LLM chapter selection failed: {e}")
            selected_indices = []

        # Merge LLM selection with semantic pre-selection. LLM picks take
        # priority; semantically pre-selected chapters the LLM missed are added
        # back. Small/medium documents keep ALL of them so datasheet detail
        # chapters (electrical specs, pin definitions) survive; only very large
        # documents (800+ chapters, e.g. annual reports) cap the supplement to
        # protect the synthesis context budget.
        llm_selected = set(selected_indices)
        missed = [idx for idx, _ in scored if idx not in llm_selected]
        if len(chapter_list) <= cfg.get("chapter_preselect_full_merge_max", 100):
            merge_cap = len(missed)
        else:
            merge_cap = cfg.get("chapter_preselect_merge_cap", 5)
        selected_indices = llm_selected | set(missed[:merge_cap])
        if not selected_indices:
            logger.info("[CHAPTER-RETRIEVE] LLM selected no chapters, falling back to FTS")
            return self._fallback_to_fts(query, doc_id)

        logger.info(f"[CHAPTER-RETRIEVE] Combined selection: {len(selected_indices)} chapters (LLM + semantic pre-match)")

        # Sub-chapter window expansion — datasheet "definitions first, data later" pattern
        # Each selected sub-chapter automatically includes the adjacent next sub-chapter to avoid data tables falling just outside the window
        # Example: LLM selects "Temperature Definitions" but values are in "Recommended Operating Conditions"
        expanded = set(selected_indices)
        for idx in selected_indices:
            next_idx = idx + 1
            if next_idx < len(chapter_list):
                expanded.add(next_idx)
        expanded_count = len(expanded) - len(selected_indices)
        if expanded_count > 0:
            selected_indices = sorted(expanded)
            logger.info(f"[CHAPTER-RETRIEVE] Sub-chapter window expansion: +{expanded_count} sub-chapters, total {len(selected_indices)}")

        # 4. Collect pages from selected chapters
        results = []
        selected_chapters = []

        for idx in selected_indices:
            if idx < 0 or idx >= len(chapter_list):
                continue
            ch_info = chapter_list[idx]
            # Find the corresponding full chapter data
            for ch in chapters:
                if ch.get("section_title") == ch_info["title"]:
                    start_page = ch.get("start_page", 0)
                    end_page = ch.get("end_page", start_page)
                    section_title = ch.get("section_title", "")
                    selected_chapters.append(section_title)

                    # Get all pages within the chapter range
                    pages = self.metadata_db.get_document_pages(doc_id)
                    chapter_pages = []
                    for page in pages:
                        pn = page.get("page_num", 0)
                        if start_page <= pn <= end_page:
                            chapter_pages.append(page)

                    # Don't limit pages per chapter, let merger handle truncation
                    # LLM already selected this chapter, indicating it considers the entire chapter relevant
                    # Merger will intelligently truncate, preserving keyword paragraphs

                    for page in chapter_pages:
                        pn = page.get("page_num", 0)
                        results.append(SearchResult(
                            doc_id=doc_id,
                            page_id=page.get("id"),
                            page_num=pn,
                            score=10.0,  # LLM-selected chapters get high scores
                            content=page.get("raw_text", ""),
                            section_title=section_title,
                            filename=doc.get("filename", ""),
                            title=doc.get("title", ""),
                            text_source=page.get("text_source", "direct_extract"),
                            page_image_path=page.get("page_image_path")
                        ))
                    break

        if not results:
            return self._fallback_to_fts(query, doc_id)

        # Sort by relevance of chapter titles to query, ensuring key data pages are not truncated
        # E.g., when querying "temperature range", "Temperature and Thermal Characteristics" should come before "Overview"
        # FIX: Also consider the chapter's level. High-level sections (overview/introduction/features) are often the
        # most direct answer to "does X have Y?" questions. We add a level-based base score so they are not pushed out.
        if query and results:
            query_lower = query.lower()
            query_terms = [t for t in re.findall(r'\w+', query_lower) if len(t) > 2]
            # Map each result to its chapter level from the original chapter info
            level_map = {}
            for ch in chapters:
                title = ch.get("section_title", "")
                level_map[title] = ch.get("section_level", 2)
            def _section_relevance(r: SearchResult) -> int:
                if not r.section_title:
                    return 0
                title_lower = r.section_title.lower()
                term_hits = sum(1 for term in query_terms if term in title_lower)
                # Level bonus: chapter=1 (highest) +3, section=2 +2, subsection=3 +1, deeper=0
                level = level_map.get(r.section_title, 2)
                level_bonus = max(0, 4 - level)
                return term_hits + level_bonus
            results.sort(key=_section_relevance, reverse=True)
            # Deduplicate by page_num after sorting to ensure stable ordering
            seen_pages = set()
            unique_results = []
            for r in results:
                if r.page_num not in seen_pages:
                    seen_pages.add(r.page_num)
                    unique_results.append(r)
            results = unique_results

        # FIX: Ensure the first page of each top-level chapter is included before truncation.
        # High-level overview pages may be short but contain conclusive answers.
        if query:
            query_lower = query.lower()
            query_terms = [t for t in re.findall(r'\w+', query_lower) if len(t) > 2]
            # Identify chapters whose title or summary directly matches query terms
            direct_match_pages = []
            for ch in chapters:
                text = f"{ch.get('section_title', '')} {ch.get('summary', '')}".lower()
                if any(term in text for term in query_terms):
                    start = ch.get('start_page', 0)
                    for r in results:
                        if r.page_num == start and r not in direct_match_pages:
                            direct_match_pages.append(r)
            # Move direct-match first pages to the front while preserving their relative order
            if direct_match_pages:
                direct_keys = {(r.doc_id, r.page_num) for r in direct_match_pages}
                others = [r for r in results if (r.doc_id, r.page_num) not in direct_keys]
                results = direct_match_pages + others
                logger.info(f"[CHAPTER-RETRIEVE] Prioritized {len(direct_match_pages)} direct-match chapter start pages")

        # 5. Limit total character count
        if max_context_quota:
            total_chars = 0
            filtered_results = []
            for r in results:
                if total_chars + len(r.content) > max_context_quota:
                    break
                filtered_results.append(r)
                total_chars += len(r.content)
            results = filtered_results

        logger.info(f"[CHAPTER-RETRIEVE] LLM selected chapters: {selected_chapters} -> {len(results)} pages")
        return results

    def _fallback_to_fts(self, query: str, doc_id: str) -> list[SearchResult]:
        """Fallback to FTS search"""
        from .retriever import HierarchicalRetriever
        retriever = HierarchicalRetriever(self.tenant_id)
        fts_results = retriever._search_fts(query, limit=10, doc_id_filter={doc_id})

        results = []
        doc = self.metadata_db.get_document(doc_id)

        for fts_r in fts_results:
            page = self.metadata_db.get_page(fts_r["page_id"])
            if page:
                results.append(SearchResult(
                    doc_id=doc_id,
                    page_id=fts_r["page_id"],
                    page_num=page.get("page_num"),
                    score=5.0 + fts_r.get("score", 0),
                    content=page.get("raw_text", ""),
                    section_title=page.get("section_title", ""),
                    filename=doc.get("filename", "") if doc else "",
                    title=doc.get("title", "") if doc else "",
                    text_source=page.get("text_source", "direct_extract"),
                    page_image_path=page.get("page_image_path")
                ))

        return results

    def _fulltext_retrieve(self, doc_filter: list[str], query: str = None,
                            max_context_quota: int = None, original_query: str = None) -> list[SearchResult]:
        doc_ids = self._resolve_doc_filter(doc_filter)
        if not doc_ids:
            return self._single_retrieve(query, [], original_query=original_query) if query else []

        # FIX: When there is a query, can't blindly truncate first N pages by page number. Should first use keyword retrieval for relevant pages,
        # to avoid truncation when the answer is in the later half of the document. If no query, keep original behavior.
        if query and query.strip():
            all_results = []
            for doc_id in doc_ids:
                all_results.extend(self._single_retrieve(query, [doc_id], max_context_quota, original_query=original_query))
            return all_results

        results = []
        for doc_id in doc_ids:
            pages = self.metadata_db.get_document_pages(doc_id)
            doc = self.metadata_db.get_document(doc_id)
            if not doc:
                continue
            # FIX: Truncate pages based on quota
            if max_context_quota:
                cfg = settings.CONTEXT_CONFIG
                avg_page_chars = cfg.get("avg_page_chars", 1200)
                quota_per_doc = max_context_quota // len(doc_ids)
                max_p = max(quota_per_doc // avg_page_chars, 5)
                pages = pages[:max_p]
            for page in pages:
                results.append(SearchResult(doc_id=doc_id, page_id=page.get("id"), page_num=page.get("page_num"),
                                           score=1.0, content=page.get("raw_text", ""),
                                           section_title=page.get("section_title", ""),
                                           filename=doc.get("filename", ""), title=doc.get("title", ""),
                                           text_source=page.get("text_source", "direct_extract")))
        return results

    def _filtered_retrieve(self, query: str, doc_filter: list[str],
                            max_context_quota: int = None, original_query: str = None) -> list[SearchResult]:
        if not doc_filter:
            return self._single_retrieve(query, [], max_context_quota, original_query=original_query)
        results = []
        for doc_id in self._resolve_doc_filter(doc_filter):
            results.extend(self._single_retrieve(query, [doc_id], max_context_quota, original_query=original_query))
        return results

    def _resolve_doc_filter(self, doc_filter: list[str], silent: bool = False) -> list[str]:
        if not doc_filter:
            return []

        # FIX: admin tenant also searches default tenant's documents
        cfg = settings.CONTEXT_CONFIG
        doc_list_limit = cfg.get("doc_filter_list_limit", 10000)
        all_docs = self.metadata_db.list_documents(limit=doc_list_limit)

        matched_ids = set()
        skipped = []
        for filter_term in doc_filter:
            filter_term = filter_term.strip().lower()
            if not filter_term:
                continue
            # FIX: Skip single characters directly; for 2-character terms, keep if pure Chinese (e.g., "封装"), otherwise skip
            if len(filter_term) < 2:
                skipped.append(filter_term)
                continue
            if len(filter_term) == 2:
                is_all_cn = '\u4e00' <= filter_term[0] <= '\u9fff' and '\u4e00' <= filter_term[1] <= '\u9fff'
                if not is_all_cn:
                    skipped.append(filter_term)
                    continue
            filter_with_space = filter_term.replace('_', ' ')
            filter_with_underscore = filter_term.replace(' ', '_')
            for doc in all_docs:
                doc_id = doc.get("id", "")
                title = (doc.get("title", "") or "").lower()
                filename = (doc.get("filename", "") or "").lower()
                if filter_term == doc_id.lower():
                    matched_ids.add(doc_id)
                elif self._is_precise_match(filter_term, title):
                    matched_ids.add(doc_id)
                elif self._is_precise_match(filter_term, filename):
                    matched_ids.add(doc_id)
                elif self._is_precise_match(filter_with_space, title):
                    matched_ids.add(doc_id)
                elif self._is_precise_match(filter_with_space, filename):
                    matched_ids.add(doc_id)
                elif self._is_precise_match(filter_with_underscore, title):
                    matched_ids.add(doc_id)
                elif self._is_precise_match(filter_with_underscore, filename):
                    matched_ids.add(doc_id)
        if skipped and not silent:
            logger.warning(f"[DOC_FILTER] Skipping too-short/invalid filter terms: {skipped}")
        if matched_ids and not silent:
            logger.info(f"[DOC_FILTER] Filter terms {doc_filter} -> matched {len(matched_ids)} documents: {list(matched_ids)[:3]}")
        return list(matched_ids)

    def _is_precise_match(self, term: str, text: str) -> bool:
        """
        Precise match check
        1. Whole-word match (word boundaries on both sides) — 2-character terms must pass this check
        2. Or term is a consecutive substring in text with length >= 3
        3. Reject single-character fuzzy matches
        """
        if not term or not text:
            return False
        if len(term) < 2:
            return False
        if term in text:
            idx = text.find(term)
            while idx != -1:
                before = text[idx - 1] if idx > 0 else ' '
                after = text[idx + len(term)] if idx + len(term) < len(text) else ' '
                # Word boundary: not preceded/followed by letter/digit/Chinese char
                is_word_boundary = not (before.isalnum() or '\u4e00' <= before <= '\u9fff')
                is_word_boundary_after = not (after.isalnum() or '\u4e00' <= after <= '\u9fff')
                if is_word_boundary and is_word_boundary_after:
                    return True
                # Non-whole-word but length >= 3, also accept (e.g., "StoneTech" in "Beijing Stone Century Technology")
                if len(term) >= 3:
                    return True
                idx = text.find(term, idx + 1)
        return False

    def reload_overview(self):
        pass
