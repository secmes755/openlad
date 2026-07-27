"""
PHASE-1: QueryPlanner three-tier routing task decomposition
"""
import json
import logging
import re
from datetime import datetime
from typing import Dict, List, Any, Optional

from ..models.client import get_model_client
from ..db.tenant_db import get_tenant_metadata_db
# corpus_taxonomy / corpus_overview not yet available; functionality temporarily simplified
# from ..ingestion.corpus_taxonomy import CorpusTaxonomyBuilder, get_taxonomy_text
# from ..ingestion.corpus_overview import get_candidate_details
from ..config import settings

logger = logging.getLogger(__name__)


class QueryPlanner:
    COARSE_TOPK = settings.CONTEXT_CONFIG.get("phase1_coarse_topk", 100)
    DOC_FILTER_CAP = settings.PLANNER_CONFIG.get("doc_filter_cap", 30)
    TITLE_DISPLAY_MAX = settings.PLANNER_CONFIG.get("title_display_max", 50)
    FILENAME_DISPLAY_MAX = settings.PLANNER_CONFIG.get("filename_display_max", 40)
    DISPLAY_STRING_MAX = settings.PLANNER_CONFIG.get("display_string_max", 80)
    ESTIMATED_CHARS_DEFAULT = settings.PLANNER_CONFIG.get("estimated_chars_default", 30000)
    ESTIMATED_CHARS_PROMPT = settings.PLANNER_CONFIG.get("estimated_chars_prompt", 50000)
    PRONOUN_QUERY_LENGTH = settings.PLANNER_CONFIG.get("pronoun_query_length_threshold", 15)
    SHORT_QUERY_LENGTH = settings.PLANNER_CONFIG.get("short_query_length_threshold", 10)

    AVAILABLE_TOOLS = """
Available retrieval tools:

1. single_retrieve(query, doc_filter=None)
   - Use for: simple, clear queries with a single target

2. decomposed_retrieve(sub_queries: list)
   - Use for: multi-entity comparison, multi-period comparison, multi-condition queries

3. fulltext_retrieve(doc_ids: list, query=None)
   - Use for: when you need to comprehensively understand a document's content

4. filtered_retrieve(query, entity_filters: list)
   - Use for: ambiguous queries where you need to identify target documents first
"""

    def __init__(self, tenant_id: str = None):
        self.model_client = get_model_client()
        self.metadata_db = get_tenant_metadata_db(tenant_id)
        self.tenant_id = tenant_id
        self.taxonomy_text = ""
        
        # FIX: admin tenant also loads the default tenant's database
        self.fallback_metadata_db = None
        if tenant_id == "admin":
            try:
                self.fallback_metadata_db = get_tenant_metadata_db("default")
                logger.info(f"[PLANNER] Admin tenant loaded default tenant database as fallback")
            except Exception as e:
                logger.warning(f"[PLANNER] Admin tenant failed to load default database: {e}")
        # taxonomy functionality temporarily simplified
        # self.taxonomy_builder = CorpusTaxonomyBuilder()
        # self._load_taxonomy()

    def _load_taxonomy(self):
        # taxonomy functionality temporarily simplified
        self.taxonomy_text = ""

    def plan(self, query: str, chat_history: str = None) -> Dict[str, Any]:
        logger.info(f"[PHASE-1] ===== Three-tier routing started =====")
        raw_query = query
        
        # Step 0: Basic rewrite (e.g. "数据库" -> "知识库")
        query = self._rewrite_query(query)
        if query != raw_query:
            logger.info(f"[PHASE-1] Query rewrite: '{raw_query}' -> '{query}'")
        
        # Step 0.5: Conversation-aware rewrite (resolve pronouns to concrete entities).
        # The rewritten text is used to enrich context sent to downstream LLMs
        # (coarse filter, fine plan), but the ORIGINAL query is never replaced — the
        # rewrite LLM can lose or swap entities, and a single bad rewrite poisons
        # every downstream step.
        if chat_history:
            history_rewritten = self._rewrite_query_with_history(query, chat_history)
            if history_rewritten and history_rewritten != query:
                logger.info(f"[PHASE-1] Conversation-aware enrichment: '{history_rewritten}'")
                chat_history = (chat_history or "") + f"\nAssistant query interpretation: {history_rewritten}\n"

        routed_category = self._route_category(query, chat_history)
        if not routed_category:
            logger.warning("[PHASE-1] Category routing failed, falling back to full-document retrieval")
            return self._fallback_plan(query)

        coarse_result = self._coarse_filter(query, routed_category, chat_history)
        candidate_ids = coarse_result[0] if isinstance(coarse_result, tuple) else coarse_result
        time_range = coarse_result[1] if isinstance(coarse_result, tuple) and len(coarse_result) > 1 else {}

        if not candidate_ids:
            # FIX: Even if coarse filter yields no candidates, don't return no_matching_docs directly.
            # The tenant may have relevant documents that the LLM failed to identify from titles.
            # Fall back to fallback_plan with keyword matching to try to find documents.
            if time_range and time_range.get("start"):
                logger.warning(f"[PHASE-1] Coarse filter found no candidates (time range {time_range.get('label', '')}), falling back to fallback retrieval")
            else:
                logger.warning("[PHASE-1] Coarse filter found no candidates, falling back to single retrieval")
            fallback = self._fallback_plan(query)
            fallback["time_range"] = time_range or {}
            return fallback

        plan = self._fine_plan(query, candidate_ids, chat_history, time_range)
        plan["routed_category"] = routed_category
        # FIX: Merge all coarse-filter candidate documents into each step's doc_filter
        # to prevent the fine_plan LLM from missing important documents (e.g., User Manual)
        for step in plan.get("steps", []):
            step_df = step.get("doc_filter") or []
            # Merge candidate_ids, deduplicate while preserving order
            merged = list(dict.fromkeys(step_df + candidate_ids))
            if len(merged) > len(step_df):
                step["doc_filter"] = merged[:self.DOC_FILTER_CAP]  # Cap to avoid oversized filters
                logger.info(f"[PHASE-1] Merged doc_filter: {len(step_df)} -> {len(merged)} documents")
        self._log_plan(plan)
        return plan

    def _route_category(self, query: str, chat_history: str = None) -> Optional[str]:
        """Simplified category routing: infer category from document metadata distribution.

        Avoid returning None directly (which triggers fallback); instead use simple
        rule-based matching on existing category_level1 and industry_package_id values.
        """
        try:
            # FIX: Admin tenant merges documents from primary and fallback tenants
            all_docs = []
            docs = self.metadata_db.get_all_documents(status="verified") or []
            if not docs:
                docs = self.metadata_db.get_all_documents(status="completed") or []
            all_docs.extend(docs)
            
            if self.tenant_id == "admin" and self.fallback_metadata_db:
                try:
                    fallback_docs = self.fallback_metadata_db.get_all_documents(status="verified") or []
                    if not fallback_docs:
                        fallback_docs = self.fallback_metadata_db.get_all_documents(status="completed") or []
                    all_docs.extend(fallback_docs)
                except Exception as e:
                    logger.warning(f"[PLANNER] Admin tenant failed to load fallback documents: {e}")

            # Count category distribution
            categories = {}
            for doc in all_docs:
                cat = doc.get("category_level1") or doc.get("industry_package_id") or "general"
                categories[cat] = categories.get(cat, 0) + 1

            if not categories:
                return "general"

            # Simple keyword matching of category names in the query
            query_lower = query.lower()
            for cat in sorted(categories.keys(), key=lambda c: categories[c], reverse=True):
                if cat.lower() in query_lower:
                    return cat

            # Return the category with the most documents
            return max(categories, key=categories.get)
        except Exception as e:
            logger.warning(f"[PHASE-1] Category routing failed: {e}")
            return "general"

    def _coarse_filter(self, query: str, category: str, chat_history: str = None):
        today = datetime.now().strftime("%Y-%m-%d")
        history_section = f"\n## Conversation History\n{chat_history}\n" if chat_history else ""
        # Get document list directly from tenant database, independent of taxonomy
        # FIX: Admin tenant merges documents from primary and fallback tenants
        all_docs = []
        docs = self.metadata_db.get_all_documents(status="verified") or []
        if not docs:
            docs = self.metadata_db.get_all_documents(status="completed") or []
        all_docs.extend(docs)
        
        if self.tenant_id == "admin" and self.fallback_metadata_db:
            try:
                fallback_docs = self.fallback_metadata_db.get_all_documents(status="verified") or []
                if not fallback_docs:
                    fallback_docs = self.fallback_metadata_db.get_all_documents(status="completed") or []
                all_docs.extend(fallback_docs)
            except Exception as e:
                logger.warning(f"[PLANNER] Admin tenant failed to load fallback documents: {e}")
        
        if not all_docs:
            return [], {}
        doc_list_lines = []
        for doc in all_docs:
            short_id = doc["id"][:8]
            title = doc.get("title") or ""
            filename = doc.get("filename") or ""
            # FIX: Also show the original filename to help the LLM identify document content
            # (especially when the extracted title is a summary)
            display = title[:self.TITLE_DISPLAY_MAX] if title else ""
            if filename and filename != title:
                # Extract meaningful part from filename (remove UUID prefix)
                import re as _re
                fname_clean = _re.sub(r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}_?', '', filename)
                if display:
                    display += f" (file: {fname_clean[:self.FILENAME_DISPLAY_MAX]})"
                else:
                    display = f"file: {fname_clean[:self.TITLE_DISPLAY_MAX]}"
            doc_list_lines.append(f"  [{short_id}] {display[:self.DISPLAY_STRING_MAX]}")
        doc_list_text = "\n".join(doc_list_lines)
        prompt = f"""You are a document filtering expert. Select the most relevant documents from the list below.

Current date: {today}
User query: {query}
Document list ({len(all_docs)} total):
{doc_list_text}
{history_section}
Filtering requirements:
- Pay attention to the short IDs in square brackets
- Select at most {self.COARSE_TOPK} documents
- If the user query contains relative time references, infer absolute date ranges
- Strictly verify that candidate document time attributes fall within the range
- Judge document relevance based on titles and filenames

Output JSON: {{"analysis":"","candidate_short_ids":["shortID1",...],"reasoning":"","time_range":{{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","label":"time range description"}}}}"""
        try:
            result = self.model_client.generate_json(prompt, temperature=0.1, max_tokens=2048)
            short_ids = result.get("candidate_short_ids", [])
            time_range = result.get("time_range")
            if time_range and time_range.get("start"):
                logger.info(f"[PHASE-1] Coarse filter time range: {time_range.get('label')} ({time_range['start']} ~ {time_range['end']})")
            candidate_ids = self._resolve_short_ids(short_ids)
            # FIX: Defensive completion — ensure entities explicitly mentioned in the query
            # have their corresponding documents included (not missed by the LLM)
            candidate_ids = self._ensure_entity_coverage(query, candidate_ids, all_docs, chat_history)
            return candidate_ids, time_range
        except Exception as e:
            logger.error(f"[PHASE-1] Coarse filter failed: {e}")
            return [], {}

    def _ensure_entity_coverage(self, query: str, candidate_ids: List[str], docs: List[Dict], chat_history: str = None) -> List[str]:
        """
        Check whether product model names / entities mentioned in the query
        are covered by candidate_ids. If an entity appears explicitly in the query
        but its corresponding document is missing from candidates, auto-complete it.
        """
        import re
        entities = []
        # Extract model/product names from query AND conversation history.
        # History extraction ensures that documents named in earlier turns of
        # a follow-up session are not lost when the rewrite LLM produces an
        # incomplete rewritten query.
        source_texts = [query]
        if chat_history:
            source_texts.append(chat_history)

        for source_text in source_texts:
            model_pattern = re.findall(r'(?<![A-Za-z0-9])[A-Za-z]{1,}[-]?[A-Za-z0-9]+(?![A-Za-z0-9])', source_text)
            for m in model_pattern:
                clean = m.replace('-', '').replace(' ', '').upper()
                # Filter out noise words that are too short (e.g., "A1"), keep valid model names
                # Rule: >=3 chars keep directly; 2 chars must contain both letters and digits (e.g., K7, T3)
                if clean and clean not in entities:
                    has_letter = any(c.isalpha() for c in clean)
                    has_digit = any(c.isdigit() for c in clean)
                    if len(clean) >= 3 or (len(clean) >= 2 and has_letter and has_digit):
                        entities.append(clean)

            # FIX: Also extract Chinese entities (company names, product names, etc.)
            cn_pattern = re.findall(r'[\u4e00-\u9fff]{2,12}', source_text)
            for w in cn_pattern:
                if w not in entities:
                    entities.append(w)

        if not entities:
            return candidate_ids

        candidate_set = set(candidate_ids)
        added = []
        
        for entity in entities:
            entity_upper = entity.upper() if isinstance(entity, str) else entity
            # Collect all documents containing this entity (match by title and filename)
            matching_docs = []
            for doc in docs:
                # Match against BOTH title and filename, not just one-or-other.
                # A poor title extraction ("Datasheet V1.0") must not be the sole
                # barrier when the original filename clearly identifies the product.
                searchable = ((doc.get("title") or "") + " " + (doc.get("filename") or "")).upper()
                if entity_upper in searchable:
                    matching_docs.append(doc)
            
            if not matching_docs:
                continue
                
            # Check if any candidate already contains this entity
            covered = any(doc["id"] in candidate_set for doc in matching_docs)
            
            if not covered:
                # Add at least one document
                for doc in matching_docs:
                    if doc["id"] not in candidate_set:
                        candidate_set.add(doc["id"])
                        added.append((entity, doc["id"][:8]))
                        break

        if added:
            logger.info(f"[PHASE-1] Entity coverage: added missing documents for entities {added}")
        return list(candidate_set)

    def _resolve_short_ids(self, short_ids: List[str]) -> List[str]:
        if not short_ids:
            return []
        # FIX: Admin tenant merges documents from primary and fallback tenants
        all_docs = []
        docs = self.metadata_db.get_all_documents(status="verified") or []
        if not docs:
            docs = self.metadata_db.get_all_documents(status="completed") or []
        all_docs.extend(docs)
        
        if self.tenant_id == "admin" and self.fallback_metadata_db:
            try:
                fallback_docs = self.fallback_metadata_db.get_all_documents(status="verified") or []
                if not fallback_docs:
                    fallback_docs = self.fallback_metadata_db.get_all_documents(status="completed") or []
                all_docs.extend(fallback_docs)
            except Exception:
                pass
        
        full_ids = []
        for short_id in short_ids:
            for doc in all_docs:
                if doc["id"].lower().startswith(short_id.strip().lower()):
                    if doc["id"] not in full_ids:
                        full_ids.append(doc["id"])
                    break
        return full_ids

    def _fine_plan(self, query: str, candidate_ids: List[str], chat_history: str = None, time_range: Dict = None) -> Dict[str, Any]:
        today = datetime.now().strftime("%Y-%m-%d")
        history_section = f"\n## Conversation History\n{chat_history}\n" if chat_history else ""
        tr_label = time_range.get('label', '') if time_range else ''
        tr_start = time_range.get('start', '') if time_range else ''
        tr_end = time_range.get('end', '') if time_range else ''
        time_range_section = f"""
## User Query Time Range
- Range: {tr_label} ({tr_start} ~ {tr_end})
- Today: {today}
Note: Strictly filter documents according to this time range.""" if tr_start else f"""
## Current date: {today}"""
        # corpus_overview not available; assemble candidate details from metadata_db directly
        candidate_details = self._build_candidate_details(candidate_ids)
        prompt = f"""You are a professional information retrieval planning expert. Based on the user query and candidate documents, formulate the optimal retrieval execution plan.

Current date: {today}
{time_range_section}
Candidate document details: {candidate_details}
{history_section}
{self.AVAILABLE_TOOLS}
User query: {query}

Analysis and planning requirements:
1. Analyze the core needs of the user's question
2. Do NOT assume relationships between entities in the query and documents based on candidate documents
3. Strictly validate time ranges
4. Determine the question type (simple / multi-entity / multi-period / ambiguous / full context)
5. Comparison queries (containing "compare", "difference", "vs", etc.) MUST use decomposed_retrieve
6. Sub-queries MUST use the same language as the original documents (Chinese documents → Chinese queries, English documents → English queries)

Output JSON:
{{
  "analysis":"Problem analysis",
  "rewritten_query":"Optimized retrieval query",
  "strategy":"single_retrieve/decomposed_retrieve/fulltext_retrieve/filtered_retrieve",
  "steps":[{{"tool":"tool name","query":"specific query text","doc_filter":["document ID or filename match"],"purpose":"purpose of this step"}}],
  "estimated_chars":{self.ESTIMATED_CHARS_PROMPT},
  "reasoning":"Reason for choosing this strategy",
  "time_range":{{"start":"YYYY-MM-DD","end":"YYYY-MM-DD","label":"time range description"}}
}}"""
        try:
            result = self.model_client.generate_json(prompt, temperature=0.1, max_tokens=2048)
            return self._validate_plan(result, query)
        except Exception as e:
            logger.error(f"[PHASE-1] Fine planning failed: {e}")
            return self._fallback_plan(query)

    def _build_candidate_details(self, candidate_ids: List[str]) -> str:
        """Simplified replacement for corpus_overview.get_candidate_details"""
        docs = []
        for doc_id in candidate_ids:
            doc = self.metadata_db.get_document(doc_id) if hasattr(self.metadata_db, "get_document") else None
            if doc:
                docs.append(doc)
        if not docs:
            return "No detailed document info available"
        lines = []
        for doc in docs:
            title = doc.get("title") or doc.get("filename") or "Unknown document"
            doc_type = doc.get("doc_type", "Unknown type")
            lines.append(f"- {doc['id']}: [{doc_type}] {title}")
        return "\n".join(lines)

    def _validate_plan(self, result: Dict, query: str) -> Dict:
        plan = {
            "analysis": result.get("analysis", ""),
            "rewritten_query": result.get("rewritten_query", query),
            "strategy": result.get("strategy", "single_retrieve"),
            "steps": [],
            "estimated_chars": self.ESTIMATED_CHARS_DEFAULT,
            "reasoning": result.get("reasoning", ""),
            "time_range": result.get("time_range") or {},
        }
        steps = result.get("steps", [])
        if not steps and result.get("reasoning", "").find("知识库中没有") >= 0:
            logger.info("[PHASE-1] Fine planning returned empty steps, preserving empty result")
        elif not steps:
            steps = [{"tool": "single_retrieve", "query": plan["rewritten_query"], "doc_filter": [], "purpose": "Direct retrieval"}]
        for step in steps:
            step.setdefault("tool", "single_retrieve")
            step.setdefault("query", plan.get("rewritten_query", query))
            step.setdefault("doc_filter", [])
            step.setdefault("purpose", "Retrieve relevant content")
            # FIX: Filter out invalid doc_filter values returned by the LLM
            # (e.g., time range strings, date intervals)
            raw_filter = step.get("doc_filter", []) or []
            valid_filter = []
            for f in raw_filter:
                if not f or not isinstance(f, str):
                    continue
                # Skip obvious time range / date strings
                if re.match(r'^\d{4}-\d{2}-\d{2}\s*~\s*\d{4}-\d{2}-\d{2}$', f):
                    continue
                if re.match(r'^\d{4}-\d{2}-\d{2}$', f):
                    continue
                valid_filter.append(f)
            step["doc_filter"] = valid_filter
        plan["steps"] = steps

        # FIX: Downgrade fulltext_retrieve -> single_retrieve for single-document queries
        # fulltext_retrieve would recall all pages, causing context explosion and triggering Map-Reduce
        all_doc_filters = []
        for step in plan.get("steps", []):
            all_doc_filters.extend(step.get("doc_filter", []))
        unique_docs = [d for d in set(all_doc_filters) if d]
        if unique_docs and len(unique_docs) == 1:
            for step in plan.get("steps", []):
                if step.get("tool") == "fulltext_retrieve":
                    step["tool"] = "single_retrieve"
                    logger.info(f"[PHASE-1] Single-document query, downgrading fulltext_retrieve to single_retrieve: {unique_docs}")
            if plan.get("strategy") == "fulltext_retrieve":
                plan["strategy"] = "single_retrieve"

        return plan

    def _log_plan(self, plan: Dict):
        logger.info(f"[PHASE-1] ===== Query Plan =====")
        logger.info(f"[PHASE-1] Strategy: {plan['strategy']}")
        logger.info(f"[PHASE-1] Analysis: {plan['analysis']}")
        logger.info(f"[PHASE-1] Reasoning: {plan['reasoning']}")
        for i, step in enumerate(plan["steps"], 1):
            logger.info(f"[PHASE-1]   Step {i}: [{step['tool']}] {step['query']}")

    def _rewrite_query(self, query: str) -> str:
        # OpenLAD: No hardcoded language-specific rewrites in core.
        # Query normalization is handled by the industry pack's preprocess_query hook if needed.
        return query

    def _rewrite_query_with_history(self, query: str, chat_history: str) -> str:
        """
        Conversation-aware query rewrite: resolve pronouns to concrete entities.
        Example: "its memory config" → "AB1234 memory config"
        """
        if not chat_history or len(chat_history.strip()) < 10:
            return query
        
        # Detect whether the query contains pronouns (language-agnostic: check for short queries with history)
        # OpenLAD: No hardcoded language-specific pronoun lists in core.
        # Pronoun resolution is handled by the industry pack's rewrite_query hook if needed.
        has_pronoun = len(query) < self.PRONOUN_QUERY_LENGTH and len(chat_history.strip()) > 30
        
        # Even without obvious pronouns, if the query is very short and has history, attempt rewrite
        if not has_pronoun and len(query) >= self.SHORT_QUERY_LENGTH:
            return query
        
        prompt = f"""You are a conversation understanding assistant. Please rewrite the user's latest question into a **complete, standalone query** that does not depend on context.

Requirements:
- If the latest question contains pronouns (it, this, that, the former, etc.), you MUST find the **specific entity name** being referred to from the conversation history and replace the pronoun
- If the latest question is already complete (no references), return the original question as-is without adding extra information
- Output only the rewritten query, no explanations

Conversation history:
{chat_history}

User's latest question: {query}

Rewritten query:"""
        try:
            result = self.model_client.generate(prompt, temperature=0.1, max_tokens=1024)
            rewritten = result.strip().strip('"').strip("'")
            if rewritten and len(rewritten) > 0 and rewritten != query:
                return rewritten
        except Exception as e:
            logger.warning(f"[PHASE-1] Conversation-aware rewrite failed: {e}")
        return query

    def _fallback_plan(self, query: str, entities: List[str] = None) -> Dict:
        """FIX: fallback_plan also attempts to identify entities in the query to populate doc_filter"""
        doc_filter = []
        if entities:
            # Try to match entities to documents
            try:
                # FIX: Admin tenant merges documents from primary and fallback tenants
                all_docs = []
                docs = self.metadata_db.get_all_documents(status="verified") or []
                if not docs:
                    docs = self.metadata_db.get_all_documents(status="completed") or []
                all_docs.extend(docs)
                
                if self.tenant_id == "admin" and self.fallback_metadata_db:
                    try:
                        fallback_docs = self.fallback_metadata_db.get_all_documents(status="verified") or []
                        if not fallback_docs:
                            fallback_docs = self.fallback_metadata_db.get_all_documents(status="completed") or []
                        all_docs.extend(fallback_docs)
                    except Exception:
                        pass
                
                for entity in (entities or []):
                    entity_upper = entity.upper()
                    for doc in all_docs:
                        title = (doc.get("title") or doc.get("filename") or "").upper()
                        if entity_upper in title and doc["id"] not in doc_filter:
                            doc_filter.append(doc["id"])
                            break
            except Exception:
                pass
        
        return {
            "analysis": "Analysis failed, degraded to single retrieval",
            "strategy": "single_retrieve",
            "steps": [{"tool": "single_retrieve", "query": query, "doc_filter": doc_filter, "purpose": "Direct retrieval"}],
            "estimated_chars": self.ESTIMATED_CHARS_DEFAULT,
            "reasoning": "Planner failed, using default single retrieval",
            "routed_category": self._route_category(query, None) or "",
        }

    def reload_overview(self):
        # overview/taxonomy functionality temporarily simplified
        pass
