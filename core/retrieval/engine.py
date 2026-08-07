"""
OpenLAD Query Engine - Three-Phase Retrieval + Agent Exploration
"""
import hashlib
import logging
import time
from typing import Any

from ..config import settings
from ..db.tenant_db import get_tenant_metadata_db, get_tenant_vector_db
from .agentic_retriever import AgenticRetriever
from .decomposer import QueryDecomposer
from .executor import RetrievalExecutor
from .planner import QueryPlanner
from .retriever import HierarchicalRetriever, SegmentMerger
from .router import IntentRouter
from .synthesizer import AnswerSynthesizer

logger = logging.getLogger(__name__)


class QueryEngine:
    def __init__(self):
        self.router = IntentRouter()
        self.decomposer = QueryDecomposer()
        self._agents = {}
        # Simple in-memory cache: {cache_key: (timestamp, result)}
        self._cache = {}
        self._cache_ttl_seconds = 0  # Cache disabled to avoid stale results during testing
        self._cache_max_size = 200

    def _get_components(self, tenant_id: str):
        """Get or create tenant-level retrieval components.

        DESIGN NOTE: The admin tenant is a super-administrator role with cross-tenant
        read access. It loads the "default" tenant database as a fallback so that
        admin users can query data across all tenants for management and debugging
        purposes. This is intentional — regular tenant users are strictly isolated.
        See core/tenant/auth.py for the role-based access control that enforces
        this: admin role bypasses resource checks, but only for the admin user.
        """
        if tenant_id not in self._agents:
            metadata_db = get_tenant_metadata_db(tenant_id)
            planner = QueryPlanner(tenant_id=tenant_id)
            executor = RetrievalExecutor(tenant_id=tenant_id)
            synthesizer = AnswerSynthesizer()
            retriever = HierarchicalRetriever(tenant_id=tenant_id)
            merger = SegmentMerger(tenant_id=tenant_id)

            # DESIGN: admin tenant loads the default tenant database as fallback
            # for cross-tenant management queries. Regular tenants are isolated.
            fallback_metadata_db = None
            fallback_vector_db = None
            if tenant_id == "admin":
                try:
                    fallback_metadata_db = get_tenant_metadata_db("default")
                    fallback_vector_db = get_tenant_vector_db("default")
                    logger.info("[ENGINE] admin tenant loaded default tenant database as fallback")
                except Exception as e:
                    logger.warning(f"[ENGINE] admin tenant failed to load default database: {e}")

            self._agents[tenant_id] = {
                "planner": planner,
                "executor": executor,
                "synthesizer": synthesizer,
                "retriever": retriever,
                "merger": merger,
                "metadata_db": metadata_db,
                "fallback_metadata_db": fallback_metadata_db,
                "fallback_vector_db": fallback_vector_db,
            }
        return self._agents[tenant_id]

    def _make_cache_key(self, query_text: str, tenant_id: str, industry_hint: str = None, chat_history: str = None) -> str:
        """Generate a cache key"""
        # Include chat_history hash to prevent cache sharing across sessions
        history_hash = hashlib.md5(chat_history.encode()).hexdigest()[:8] if chat_history else "no_hist"
        raw = f"{tenant_id}:{industry_hint or 'auto'}:{history_hash}:{query_text}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _get_cached(self, cache_key: str) -> dict | None:
        """Retrieve cached result"""
        if cache_key not in self._cache:
            return None
        timestamp, result = self._cache[cache_key]
        if time.time() - timestamp > self._cache_ttl_seconds:
            del self._cache[cache_key]
            return None
        logger.info(f"[ENGINE] cache hit: {cache_key[:8]}")
        return result

    def _set_cached(self, cache_key: str, result: dict):
        """Write to cache"""
        # LRU eviction
        if len(self._cache) >= self._cache_max_size:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            del self._cache[oldest]
        self._cache[cache_key] = (time.time(), result)

    def _execute_agentic(self, query_text: str, tenant_id: str) -> dict[str, Any] | None:
        """Execute Agentic retrieval"""
        try:
            agent = AgenticRetriever(tenant_id)
            result = agent.retrieve(query_text)
            agent.release()
            return result
        except Exception as e:
            logger.error(f"[ENGINE] Agentic retrieval failed: {e}")
            return None

    def _classify_query(self, query: str) -> str:
        """OpenLAD: Detect query type: traditional / deep_research

        Uses LLM for language-agnostic classification instead of hardcoded keywords.
        """
        system_prompt = """You are a query classification assistant. Classify the user query into one of two types:

- "deep_research": The query asks for comparison between multiple entities, enumeration of multiple items, or comprehensive analysis across multiple sources.
- "traditional": The query asks for a single fact, lookup, or simple answer from one document.

Output ONLY a JSON object: {"type": "deep_research"} or {"type": "traditional"}"""

        prompt = f"Query: {query}"
        # Use router's model_client (shared via get_model_client singleton)
        from ..models.client import get_model_client
        model_client = get_model_client()
        try:
            result = model_client.generate_json(prompt, system_prompt=system_prompt, max_tokens=256, temperature=0.1)
            if result and result.get("type") in ("deep_research", "traditional"):
                return result["type"]
        except Exception as e:
            logger.warning(f"[ENGINE] LLM query classification failed: {e}, falling back to heuristic")

        # Fallback: simple entity count heuristic (language-agnostic)
        import re as _re
        # Extract alphanumeric model numbers / entity names (generic pattern, not chip-specific)
        entities = _re.findall(r'(?<![A-Za-z0-9])[A-Z]{1,}[a-z]*\d+[A-Z0-9]*(?![A-Za-z0-9])', query)
        unique_entities = set(entities)
        if len(unique_entities) >= 2:
            return "deep_research"

        return "traditional"

    def _execute_deep_research(self, query_text: str, tenant_id: str,
                                components: dict, chat_history: str = None,
                                industry_hint: str = None) -> dict[str, Any]:
        """Execute deep research retrieval (decompose query + retrieve separately + merge & synthesize)

        FIX: Prefer Agentic retrieval; fall back to traditional decomposition when Agentic fails.
        """
        planner = components["planner"]
        executor = components["executor"]
        synthesizer = components["synthesizer"]
        components["metadata_db"]
        router_plan = self.router.route(query_text)

        # FIX: Try Agentic retrieval first (better for comparison queries)
        logger.info("[ENGINE] deep_research: trying Agentic retrieval first")
        agentic_result = self._execute_agentic(query_text, tenant_id)

        # Before accepting agentic result, verify it covers all entities in the query.
        # The agentic retriever may find only one document even when the user asks
        # about two or more — e.g., "compare SSU9383CM and SSD2386" but only
        # SSD2386 was retrieved.
        import re as _re
        chip_models = _re.findall(r'(?<![A-Za-z0-9])[A-Z]{1,}\d{2,}[A-Z]*(?![A-Za-z0-9])', query_text)
        unique_models = list(dict.fromkeys(chip_models))
        entities = unique_models if len(unique_models) >= 1 else None

        if agentic_result and agentic_result.get("total_results", 0) > 0:
            # Check whether agentic result covers all detected entities
            if entities and len(entities) >= 2:
                agentic_sources = agentic_result.get("sources", [])
                source_text = " ".join(
                    (s.get("title", "") + " " + s.get("filename", "")).upper()
                    for s in agentic_sources
                )
                covered = [e for e in entities if e.upper() in source_text]
                if len(covered) < len(entities):
                    logger.info(
                        f"[ENGINE] Agentic retrieval missed entities: "
                        f"found {covered}, missing {[e for e in entities if e not in covered]}. "
                        f"Falling back to traditional decomposition."
                    )
                else:
                    logger.info("[ENGINE] Agentic retrieval succeeded, using Agentic result")
                    return agentic_result, {
                        "answer": agentic_result.get("answer", ""),
                        "sources": agentic_result.get("sources", [])
                    }, router_plan
            else:
                logger.info("[ENGINE] Agentic retrieval succeeded, using Agentic result")
                return agentic_result, {
                    "answer": agentic_result.get("answer", ""),
                    "sources": agentic_result.get("sources", [])
                }, router_plan

        # Agentic failed or incomplete — fall back to traditional decomposition
        logger.info("[ENGINE] Agentic retrieval failed or incomplete, falling back to traditional decomposition")

        # FIX: Extract entities from query (e.g., chip model numbers), pass to decomposer for true entity decomposition
        import re as _re
        chip_models = _re.findall(r'(?<![A-Za-z0-9])[A-Z]{1,}\d{2,}[A-Z]*(?![A-Za-z0-9])', query_text)
        unique_models = list(dict.fromkeys(chip_models))  # deduplicate while preserving order
        entities = unique_models if len(unique_models) >= 1 else None  # FIX: >=1 instead of >=2

        # Attempt to decompose query
        sub_queries = self.decomposer.decompose(query_text, entities=entities)
        plan_routed_category = ""
        if len(sub_queries) <= 1:
            # Decomposition failed, fall back to fallback_plan
            logger.info("[ENGINE] deep_research decomposition failed, falling back to fallback_plan")
            plan = planner._fallback_plan(query_text, entities=entities)
            plan_routed_category = plan.get("routed_category", "")
            retrieval_result = executor.execute(plan, tenant_id=tenant_id, industry_hint=industry_hint, original_query=query_text)
        else:
            logger.info(f"[ENGINE] deep_research decomposed into {len(sub_queries)} sub-queries")
            all_contexts = []
            all_sources = []
            total_results = 0
            # Per-subquery context quota: use config budget split across sub-queries
            cfg = settings.CONTEXT_CONFIG
            per_subquery_max = cfg.get("synthesis_context_budget", 39000) // max(len(sub_queries), 1)
            for i, sq in enumerate(sub_queries, 1):
                sub_plan = planner.plan(sq, chat_history)
                if i == 1:
                    plan_routed_category = sub_plan.get("routed_category", "")
                sub_result = executor.execute(sub_plan, tenant_id=tenant_id, industry_hint=industry_hint, original_query=query_text)
                sub_context = sub_result.get('context', '')
                if len(sub_context) > per_subquery_max:
                    sub_context = sub_context[:per_subquery_max]
                    logger.warning(f"[ENGINE] sub-query {i} context truncated: {len(sub_result.get('context', ''))} -> {per_subquery_max}")
                all_contexts.append(f"\n===== Sub-query {i}: {sq} =====\n{sub_context}")
                all_sources.extend(sub_result.get("sources", []))
                total_results += sub_result.get("total_results", 0)

            final_context = "\n".join(all_contexts)
            # Proportional truncation: use config budget, proportional across sub-queries
            context_budget = cfg.get("synthesis_context_budget", 39000)
            if len(final_context) > context_budget:
                if len(all_contexts) > 1:
                    ratio = context_budget / len(final_context)
                    truncated = []
                    for ac in all_contexts:
                        keep = max(int(len(ac) * ratio), 500)
                        truncated.append(ac[:keep])
                    final_context = "\n".join(truncated)
                else:
                    final_context = final_context[:context_budget]
                logger.info(
                    f"[ENGINE] deep_research context truncated: "
                    f"{sum(len(ac) for ac in all_contexts)} -> {len(final_context)}"
                )

            retrieval_result = {
                "context": final_context,
                "sources": all_sources,
                "total_results": total_results,
                "total_chars": len(final_context),
                "strategy": "decomposed_retrieve"
            }

        # Empty retrieval guard for deep_research path
        total_results = retrieval_result.get("total_results", 0)
        total_chars = retrieval_result.get("total_chars", 0)
        if total_results == 0 or total_chars < 50:
            empty_answer = "No relevant information found in the knowledge base."
            synthesis_result = {"answer": empty_answer, "sources": [], "structured": False}
            return retrieval_result, synthesis_result, router_plan

        # Compute routed_category: prefer planner result over user-enforced industry
        routed_category = plan_routed_category
        if industry_hint and industry_hint != "auto":
            from ..plugins import get_plugin_registry
            registry = get_plugin_registry()
            plugin = registry.get_plugin(industry_hint)
            if plugin and plugin.manifest.category_mapping:
                routed_category = plugin.manifest.category_mapping[0]
                logger.info(f"[ENGINE] deep_research user-enforced industry: {industry_hint} -> category={routed_category}")

        synthesis_result = synthesizer.synthesize(
            query=query_text,
            plan=router_plan,
            context=retrieval_result.get("context", ""),
            sources=retrieval_result.get("sources", []),
            chat_history=chat_history,
            routed_category=routed_category,
            original_query=query_text
        )
        return retrieval_result, synthesis_result, router_plan

    @staticmethod
    def _format_chat_history(chat_history: list[dict]) -> str:
        """Format frontend-provided List[Dict] chat history into a readable string"""
        if not chat_history:
            return ""
        lines = []
        for msg in chat_history:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "user":
                lines.append(f"User: {content}")
            elif role == "assistant":
                lines.append(f"Assistant: {content}")
            else:
                lines.append(f"{role}: {content}")
        return "\n".join(lines)

    def _lookup_spec_facts(self, query_text: str, tenant_id: str,
                           metadata_db, plan: dict) -> list[dict]:
        """Look up the assertion-level spec_facts index for this query.

        Builds the keyword set from English tokens in the query plus the
        configurable Chinese->English expansion (spec_query_terms). Only facts
        with >= spec_facts_min_hits keyword hits qualify. Returns [] when the
        feature is off, the table is empty, or nothing matches — the caller
        then proceeds with the normal page-level context unchanged.
        """
        from ..config import settings
        cfg = settings.CONTEXT_CONFIG
        if not cfg.get("spec_facts_enabled", False):
            return []
        if not hasattr(metadata_db, "search_spec_facts"):
            return []

        import re as _re
        keywords: list[str] = []
        # English / model tokens from the query itself (e.g. RK3568, H.264, UART).
        for tok in _re.findall(r'[A-Za-z][\w.\-]{1,20}', query_text):
            keywords.append(tok)
        # Chinese term expansion (query-understanding synonym layer), plus
        # English synonym expansion (case-insensitive, e.g. GPU -> graphics).
        query_lower = query_text.lower()
        for zh, en_words in (cfg.get("spec_query_terms") or {}).items():
            if zh in query_text or zh.lower() in query_lower:
                keywords.extend(en_words)
        # Planner-harvested entities (e.g. RK3568) improve entity scoping.
        for ent in (plan.get("entities") or []):
            if isinstance(ent, str) and ent:
                keywords.append(ent)
        # Deduplicate, keep order.
        seen, uniq = set(), []
        for kw in keywords:
            k = kw.lower()
            if k and k not in seen:
                seen.add(k)
                uniq.append(kw)
        if not uniq:
            return []

        doc_filter = None
        try:
            df = plan.get("doc_id_filter") or plan.get("doc_filter")
            if df:
                doc_filter = set(df)
        except Exception:
            doc_filter = None

        try:
            hits = metadata_db.search_spec_facts(
                uniq, doc_id_filter=doc_filter,
                limit=cfg.get("spec_facts_max_inject", 6) * 3)
        except Exception as e:
            logger.warning(f"[ENGINE] spec-fact lookup failed (non-fatal): {e}")
            return []

        min_hits = cfg.get("spec_facts_min_hits", 2)
        qualified = []
        for h in hits:
            hay = f"{h.get('entity','')} {h.get('attribute','')} {h.get('value','')} {h.get('source_text','')}".lower()
            n = sum(1 for kw in uniq if kw.lower() in hay)
            if n >= min_hits:
                h["_hits"] = n
                qualified.append(h)
        qualified.sort(key=lambda x: -x["_hits"])
        return qualified[: cfg.get("spec_facts_max_inject", 6)]

    @staticmethod
    def _format_spec_facts(facts: list[dict]) -> str:
        """Render spec facts as an authoritative evidence block for the context."""
        lines = ["【权威规格事实 / Authoritative Spec Facts】(extracted from original page text, verbatim-verified)"]
        for f in facts:
            lines.append(
                f"- {f.get('entity','')} | {f.get('attribute','')}: {f.get('value','')}"
                f"  (page {f.get('page_num','?')}; 原文: \"{f.get('source_text','')[:120]}\")")
        return "\n".join(lines)

    def query(self, query_text: str, tenant_id: str,
              industry_hint: str = None,
              chat_history: list[dict] = None) -> dict[str, Any]:
        start_time = time.time()
        # OpenLAD: No hardcoded language-specific rewrites in core.
        # Query normalization is handled by the industry pack's preprocess_query hook if needed.
        logger.info(f"[ENGINE] tenant: {tenant_id}, query: {query_text}, industry: {industry_hint}")

        chat_history_str = self._format_chat_history(chat_history) if chat_history else None

        # Cache check
        cache_key = self._make_cache_key(query_text, tenant_id, industry_hint, chat_history_str)
        cached = self._get_cached(cache_key)
        if cached is not None:
            cached["cached"] = True
            cached["elapsed_ms"] = 0
            return cached

        components = self._get_components(tenant_id)
        planner = components["planner"]
        executor = components["executor"]
        synthesizer = components["synthesizer"]
        metadata_db = components["metadata_db"]

        # Query classification & routing
        query_type = self._classify_query(query_text)
        logger.info(f"[ENGINE] query classification: {query_type}")

        # For follow-up queries with chat history, prefer the traditional planner because
        # it is context-aware (_rewrite_query_with_history + _ensure_entity_coverage), while
        # the AgenticRetriever only sees the current query in isolation. Without this guard,
        # a pronoun-only follow-up like "他们主要差别在哪里？" gets classified as deep_research
        # and loses documents from the prior turn (e.g., RK3562).
        if query_type == "deep_research" and chat_history_str:
            logger.info("[ENGINE] deep_research query with chat history; switching to traditional planner to preserve context")
            query_type = "traditional"

        if query_type == "deep_research":
            # Deep research path
            retrieval_result, synthesis_result, router_plan = self._execute_deep_research(
                query_text, tenant_id, components, chat_history_str, industry_hint
            )
        else:
            # Traditional path
            # Phase 1: Plan
            plan = planner.plan(query_text, chat_history_str)

            # Phase 2: Retrieve
            retrieval_result = executor.execute(plan, tenant_id=tenant_id, industry_hint=industry_hint, original_query=query_text)

            # Phase 2.5: Spec-fact bypass — assertion-level (entity, attribute,
            # value) index built from authoritative page text at ingest time.
            # If the query matches spec facts, prepend them to the context as
            # authoritative evidence. This is the missing assertion abstraction
            # that page/chapter-level patches (vector-hybrid, VLM penalty,
            # chapter-scope widening) were compensating for.
            spec_facts = self._lookup_spec_facts(query_text, tenant_id, metadata_db, plan)
            if spec_facts:
                spec_block = self._format_spec_facts(spec_facts)
                retrieval_result["context"] = spec_block + "\n\n" + retrieval_result.get("context", "")
                retrieval_result["spec_facts"] = spec_facts
                logger.info(f"[ENGINE] spec-fact bypass: injected {len(spec_facts)} authoritative facts")

            # Phase 3: Synthesize
            router_plan = self.router.route(query_text)
            rewritten_query = plan.get("rewritten_query", query_text)
            # decomposed_retrieve produces a LIST of sub-queries; the synthesizer
            # expects a string. Join sub-queries (fall back to the original query)
            # so follow-up comparison queries routed to the traditional path do not
            # crash with "expected string or bytes-like object, got 'list'".
            if isinstance(rewritten_query, list):
                rewritten_query = "; ".join(str(q) for q in rewritten_query) if rewritten_query else query_text
            routed_category = plan.get("routed_category", "")

            if industry_hint and industry_hint != "auto":
                from ..plugins import get_plugin_registry
                registry = get_plugin_registry()
                plugin = registry.get_plugin(industry_hint)
                if plugin and plugin.manifest.category_mapping:
                    routed_category = plugin.manifest.category_mapping[0]
                    logger.info(f"[ENGINE] user-enforced industry: {industry_hint}")

            synthesis_result = synthesizer.synthesize(
                query=rewritten_query,
                plan=router_plan,
                context=retrieval_result.get("context", ""),
                sources=retrieval_result.get("sources", []),
                chat_history=chat_history_str,
                routed_category=routed_category,
                original_query=query_text
            )

        # Empty retrieval guard: if context is empty or very short, refuse to let LLM fabricate
        total_results = retrieval_result.get("total_results", 0)
        total_chars = retrieval_result.get("total_chars", 0)
        if total_results == 0 or total_chars < 50:
            answer = "No relevant information found in the knowledge base."
            elapsed_ms = int((time.time() - start_time) * 1000)
            metadata_db.log_query(query=query_text, intent="empty_retrieval",
                                  elapsed_ms=elapsed_ms, results_count=0, answer_length=len(answer))
            return {"query": query_text, "answer": answer, "sources": [],
                    "confidence": "none", "elapsed_ms": elapsed_ms}

        answer = synthesis_result.get("answer", "")
        elapsed_ms = int((time.time() - start_time) * 1000)

        metadata_db.log_query(
            query=query_text,
            intent=router_plan.intent.value,
            industry_package_id=industry_hint,
            elapsed_ms=elapsed_ms,
            results_count=retrieval_result.get("total_results", 0),
            answer_length=len(answer),
            trace={"plan": retrieval_result.get("strategy", ""), "sources_count": len(retrieval_result.get("sources", []))}
        )

        result = {
            "query": query_text,
            "answer": answer,
            "sources": synthesis_result.get("sources", []),
            "confidence": synthesis_result.get("confidence", "none"),
            "elapsed_ms": elapsed_ms,
            "plan": {"intent": router_plan.intent.value, "strategy": retrieval_result.get("strategy", "")},
            "phases": {
                "phase1_plan": {"query_type": query_type, "strategy": retrieval_result.get("strategy", "")},
                "phase2_retrieval": {
                    "total_results": retrieval_result.get("total_results", 0),
                    "total_chars": retrieval_result.get("total_chars", 0),
                },
                "phase3_synthesis": {
                    "answer": answer,
                    "sources": synthesis_result.get("sources", [])
                }
            }
        }

        # Write to cache
        self._set_cached(cache_key, result)
        return result

    def release(self):
        logger.info("QueryEngine resources released")
