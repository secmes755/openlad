"""
OpenLAD Query Engine - Three-Phase Retrieval + Agent Exploration
"""
import logging
import time
from typing import Any

from ..config import settings
from ..db.tenant_db import get_tenant_metadata_db
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

    def _get_components(self, tenant_id: str):
        """Get or create tenant-level retrieval components.

        Tenants are strictly isolated: the admin tenant has no cross-tenant
        read access (the former "default" database fallback was removed).
        Cross-tenant data access is only possible with that tenant's own key.
        See core/tenant/auth.py for the role-based access control.
        """
        if tenant_id not in self._agents:
            metadata_db = get_tenant_metadata_db(tenant_id)
            planner = QueryPlanner(tenant_id=tenant_id)
            executor = RetrievalExecutor(tenant_id=tenant_id)
            synthesizer = AnswerSynthesizer()
            retriever = HierarchicalRetriever(tenant_id=tenant_id)
            merger = SegmentMerger(tenant_id=tenant_id)

            self._agents[tenant_id] = {
                "planner": planner,
                "executor": executor,
                "synthesizer": synthesizer,
                "retriever": retriever,
                "merger": merger,
                "metadata_db": metadata_db,
            }
        return self._agents[tenant_id]

    def _execute_agentic(self, query_text: str, tenant_id: str,
                         spec_facts_plan: dict = None) -> dict[str, Any] | None:
        """Execute Agentic retrieval. spec_facts_plan carries the planner output
        (routed_category / entities / rewritten_query) so the per-document
        spec-fact lookups resolve the same industry pack terms as other paths."""
        try:
            agent = AgenticRetriever(tenant_id, spec_facts_plan=spec_facts_plan)
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
        metadata_db = components["metadata_db"]
        router_plan = self.router.route(query_text)

        # Plan the main query once up front: the planner's routed_category /
        # entities / rewritten_query drive the spec-fact assertion layer for
        # BOTH deep_research sub-paths (agentic and decomposed fallback).
        try:
            main_plan = planner.plan(query_text, chat_history)
        except Exception as e:
            logger.warning(f"[ENGINE] deep_research main planning failed (non-fatal): {e}")
            main_plan = {}

        # FIX: Try Agentic retrieval first (better for comparison queries)
        logger.info("[ENGINE] deep_research: trying Agentic retrieval first")
        agentic_result = self._execute_agentic(query_text, tenant_id,
                                               spec_facts_plan=main_plan)

        # Before accepting agentic result, verify it covers all entities in the query.
        # The agentic retriever may find only one document even when the user asks
        # about two or more — e.g., comparing two product models but only one was
        # retrieved.
        from .spec_facts import extract_model_tokens
        unique_models = extract_model_tokens(query_text)
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

        # FIX: Extract entities from query (model-number tokens), pass to
        # decomposer for true entity decomposition
        unique_models = extract_model_tokens(query_text)
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
            per_subquery_max = cfg.get("synthesis_context_budget", 35500) // max(len(sub_queries), 1)
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
            context_budget = cfg.get("synthesis_context_budget", 35500)
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

        # Spec-fact bypass for the deep_research path: the assertion layer must
        # cover comparison/enumeration queries too, not only the traditional
        # path — page-level retrieval is asymmetric across entities ("same
        # attribute, two products"), which is exactly what this index fixes.
        # Run BEFORE the empty-retrieval guard so a fully-failed page retrieval
        # can still be rescued by authoritative facts (same semantics as the
        # traditional path).
        facts_plan = dict(main_plan or {})
        facts_plan["rewritten_query"] = sub_queries
        facts_plan["routed_category"] = plan_routed_category or facts_plan.get("routed_category", "")
        spec_facts = self._lookup_spec_facts(query_text, tenant_id, metadata_db, facts_plan,
                                             industry_hint=industry_hint)
        if spec_facts:
            spec_block = self._format_spec_facts(spec_facts)
            retrieval_result["context"] = spec_block + "\n\n" + retrieval_result.get("context", "")
            retrieval_result["spec_facts"] = spec_facts
            retrieval_result["total_chars"] = retrieval_result.get("total_chars", 0) + len(spec_block)
            retrieval_result["total_results"] = retrieval_result.get("total_results", 0) + len(spec_facts)
            logger.info(f"[ENGINE] deep_research spec-fact bypass: injected {len(spec_facts)} authoritative facts")

        # Empty retrieval guard for deep_research path (spec-fact hits exempt)
        total_results = retrieval_result.get("total_results", 0)
        total_chars = retrieval_result.get("total_chars", 0)
        if (total_results == 0 or total_chars < 50) and not spec_facts:
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
            original_query=query_text,
            explicit_pack_id=industry_hint if industry_hint and industry_hint != "auto" else None
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
                           metadata_db, plan: dict,
                           industry_hint: "str | None" = None) -> list[dict]:
        """Delegate to the shared spec-fact assertion layer (core.retrieval.spec_facts)
        so every retrieval path consults the same index with the same rules."""
        from .spec_facts import lookup_spec_facts
        return lookup_spec_facts(query_text, metadata_db, plan=plan,
                                 industry_hint=industry_hint)

    @staticmethod
    def _format_spec_facts(facts: list[dict]) -> str:
        """Render spec facts as an authoritative evidence block for the context."""
        from .spec_facts import format_spec_facts
        return format_spec_facts(facts)

    def query(self, query_text: str, tenant_id: str,
              industry_hint: str = None,
              chat_history: list[dict] = None,
              progress_cb=None) -> dict[str, Any]:
        start_time = time.time()

        def emit(stage: str, **meta):
            # Optional coarse-grained progress hook for streaming transports
            # (SSE). Failures in the consumer must never break the pipeline.
            if progress_cb:
                try:
                    progress_cb(stage, **meta)
                except Exception:
                    pass

        # OpenLAD: No hardcoded language-specific rewrites in core.
        # Query normalization is handled by the industry pack's preprocess_query hook if needed.
        logger.info(f"[ENGINE] tenant: {tenant_id}, query: {query_text}, industry: {industry_hint}")
        emit("planning")

        chat_history_str = self._format_chat_history(chat_history) if chat_history else None

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
            # Deep research path (retrieval rounds dominate; synthesis inside)
            emit("retrieving")
            retrieval_result, synthesis_result, router_plan = self._execute_deep_research(
                query_text, tenant_id, components, chat_history_str, industry_hint
            )
        else:
            # Traditional path
            # Phase 1: Plan
            plan = planner.plan(query_text, chat_history_str)

            # Phase 2: Retrieve
            emit("retrieving")
            retrieval_result = executor.execute(plan, tenant_id=tenant_id, industry_hint=industry_hint, original_query=query_text)

            # Phase 2.5: Spec-fact bypass — assertion-level (entity, attribute,
            # value) index built from authoritative page text at ingest time.
            # If the query matches spec facts, prepend them to the context as
            # authoritative evidence. This is the missing assertion abstraction
            # that page/chapter-level patches (vector-hybrid, VLM penalty,
            # chapter-scope widening) were compensating for.
            spec_facts = self._lookup_spec_facts(query_text, tenant_id, metadata_db, plan,
                                                 industry_hint=industry_hint)
            if spec_facts:
                spec_block = self._format_spec_facts(spec_facts)
                retrieval_result["context"] = spec_block + "\n\n" + retrieval_result.get("context", "")
                retrieval_result["spec_facts"] = spec_facts
                # Spec-fact hits are retrieval results too: update the counters
                # so the empty-retrieval guard below does not discard an answer
                # that has authoritative (verbatim) evidence purely because page
                # retrieval came up short.
                retrieval_result["total_chars"] = retrieval_result.get("total_chars", 0) + len(spec_block)
                retrieval_result["total_results"] = retrieval_result.get("total_results", 0) + len(spec_facts)
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
            # Rewrite-collapse guard: the planner LLM may rewrite a comparison
            # query into a single-entity sub-query ("对比A和B的X" -> "A X 配置参数"),
            # which frames the synthesis around one side only. If the rewrite
            # drops any entity present in the ORIGINAL query, synthesize from
            # the original query instead. Entities are harvested from the
            # planner AND from the query text itself (shared model-token
            # pattern) because the planner's entity list is itself sometimes
            # incomplete. Switchable via rewrite_collapse_guard.
            if settings.CONTEXT_CONFIG.get("rewrite_collapse_guard", True):
                from .spec_facts import entity_mentioned, extract_model_tokens
                plan_entities = {str(e) for e in (plan.get("entities") or [])}
                query_entities = set(extract_model_tokens(query_text))
                known_entities = plan_entities | query_entities
                if rewritten_query and known_entities:
                    missing = [e for e in known_entities
                               if not entity_mentioned(e, rewritten_query)]
                    if missing and all(entity_mentioned(e, query_text) for e in missing):
                        logger.info(f"[ENGINE] rewritten query drops entities {missing}; using original query for synthesis framing")
                        rewritten_query = query_text
            routed_category = plan.get("routed_category", "")

            if industry_hint and industry_hint != "auto":
                from ..plugins import get_plugin_registry
                registry = get_plugin_registry()
                plugin = registry.get_plugin(industry_hint)
                if plugin and plugin.manifest.category_mapping:
                    routed_category = plugin.manifest.category_mapping[0]
                    logger.info(f"[ENGINE] user-enforced industry: {industry_hint}")

            # Phase 3: Synthesize
            emit("generating")
            synthesis_result = synthesizer.synthesize(
                query=rewritten_query,
                plan=router_plan,
                context=retrieval_result.get("context", ""),
                sources=retrieval_result.get("sources", []),
                chat_history=chat_history_str,
                routed_category=routed_category,
                original_query=query_text,
                explicit_pack_id=industry_hint if industry_hint and industry_hint != "auto" else None
            )

        # Empty retrieval guard: if context is empty or very short, refuse to let LLM fabricate
        total_results = retrieval_result.get("total_results", 0)
        total_chars = retrieval_result.get("total_chars", 0)
        if total_results == 0 or total_chars < 50:
            answer = "No relevant information found in the knowledge base."
            elapsed_ms = int((time.time() - start_time) * 1000)
            # NOTE: query audit logging happens in the API route layer
            # (routes/query.py), which always runs after engine.query returns.
            # Engine-level log_query here would double-write the audit trail.
            return {"query": query_text, "answer": answer, "sources": [],
                    "confidence": "none", "elapsed_ms": elapsed_ms}

        answer = synthesis_result.get("answer", "")
        # Evidence appendix: when authoritative spec facts were injected, attach
        # their verbatim source sentences so the reader can audit the narrative
        # against the original page text (deterministic, never LLM-written).
        spec_facts = retrieval_result.get("spec_facts") or []
        if spec_facts:
            from .spec_facts import build_evidence_appendix
            sources = retrieval_result.get("sources") or []
            doc_titles: dict = {}
            for s in sources:
                if isinstance(s, dict) and s.get("doc_id"):
                    doc_titles[s["doc_id"]] = s.get("title") or s.get("filename") or ""
            answer += build_evidence_appendix(spec_facts, doc_titles=doc_titles,
                                              query_text=query_text)
        elapsed_ms = int((time.time() - start_time) * 1000)

        # NOTE: query audit logging happens in the API route layer
        # (routes/query.py), which always runs after engine.query returns and
        # carries the richer audit fields (user_id, session_id). Engine-level
        # log_query here would double-write the audit trail. The `intent`
        # value the route needs is exposed on the result below.

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

        return result

    def release(self):
        logger.info("QueryEngine resources released")
