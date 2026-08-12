"""
Answer Synthesizer
Supports industry-specific Prompt routing: automatically loads synthesis rules for the corresponding industry based on document classification
"""
import logging
import re
from typing import Any

from ..config import settings
from ..models.client import get_model_client
from .router import IntentType, QueryPlan

logger = logging.getLogger(__name__)


class AnswerSynthesizer:
    def __init__(self):
        self.model_client = get_model_client()
        self.plugin_registry = self._init_plugin_registry()

    def _init_plugin_registry(self):
        from ..plugins import get_plugin_registry
        registry = get_plugin_registry()
        packs_info = (
            registry.list_plugins()
            if hasattr(registry, "list_plugins")
            else "N/A"
        )
        logger.info(f"[SYNTHESIZER] Industry Prompt packs: {packs_info}")
        return registry

    def synthesize(self, query: str, plan: QueryPlan,
                   context: str, sources: list[dict],
                   chat_history: str = None,
                   routed_category: str = None,
                   original_query: str = None) -> dict[str, Any]:
        # Last line of defense: if context is empty or very short, directly return a no-info message
        if not context or len(context.strip()) < 50:
            logger.warning("[SYNTHESIZER] Retrieval context is empty, refusing to generate answer")
            return {"answer": "Based on the documents in the current knowledge base, no relevant information was found.", "sources": sources, "structured": False, "confidence": "none"}

        if plan.intent == IntentType.VERSION_COMPARE or plan.requires_comparison:
            return self._synthesize_version_compare(query, plan, context, sources, chat_history, routed_category)
        elif plan.intent == IntentType.CROSS_REFERENCE:
            return self._synthesize_cross_reference(query, plan, context, sources, chat_history, routed_category)
        else:
            return self._synthesize_standard(query, plan, context, sources, chat_history, routed_category, original_query)

    def _synthesize_standard(self, query: str, plan: QueryPlan,
                             context: str, sources: list[dict],
                             chat_history: str = None,
                             routed_category: str = None,
                             original_query: str = None) -> dict[str, Any]:
        history_section = f"\n\nChat history (for background reference only, absolutely do not repeat answers to these questions):\n{chat_history}" if chat_history else ""

        # corpus_taxonomy has no equivalent in OpenLAD, leave catalog empty
        catalog = ""

        # SCHEMATIC_ENGINE related code removed in OpenLAD
        schematic_context = ""

        industry_pack = None
        if hasattr(self.plugin_registry, "get_plugin_by_category"):
            industry_pack = self.plugin_registry.get_plugin_by_category(routed_category)
        if industry_pack is None and hasattr(self.plugin_registry, "detect_plugin_for_text"):
            # Category routing is modal (tenant-wide dominant category), so
            # fall back to content-grounded detection: a pack applies only
            # when its own entity patterns match the query. Detection uses
            # the query texts only — retrieved context is contaminated by
            # merged doc_filters pulling unrelated documents in. Follow-up
            # queries reach the synthesizer already rewritten with the
            # conversation entity, so query-side detection covers them.
            industry_pack = self.plugin_registry.detect_plugin_for_text(
                f"{original_query or ''}\n{query or ''}"
            )

        logger.info(f"[SYNTHESIZER] original_query='{original_query}', query='{query}'")

        # Use Map-Reduce chunked extraction for long contexts to prevent LLM from getting lost
        effective_context = context
        mr_threshold = settings.CONTEXT_CONFIG.get("map_reduce_threshold", 30000)
        if len(context) > mr_threshold:
            logger.info(f"[SYNTHESIZER] Context too long ({len(context)} chars > {mr_threshold}), enabling Map-Reduce")
            effective_context = self._map_reduce_extract(context, query, industry_pack=industry_pack, original_query=original_query)
            logger.info(f"[SYNTHESIZER] After Map-Reduce reduction: {len(effective_context)} chars")

        if industry_pack:
            manifest = getattr(industry_pack, "manifest", None)
            pack_name = getattr(manifest, "name", "unknown") if manifest else "unknown"
            pack_version = getattr(manifest, "version", "N/A") if manifest else "N/A"
            logger.info(f"[SYNTHESIZER] Using industry Prompt pack: {pack_name} v{pack_version} (category={routed_category})")
            # Use legacy prompt as base (compact structure, higher LLM compliance), append industry pack constraints
            base_prompt = self._build_legacy_prompt(query, effective_context, catalog, history_section, schematic_context, original_query)
            industry_addon = self._build_industry_addon(industry_pack, query)
            if industry_addon:
                prompt = base_prompt + "\n\n" + industry_addon
            else:
                prompt = base_prompt
        else:
            logger.warning(f"[SYNTHESIZER] No industry pack matched, using built-in default Prompt (category={routed_category})")
            prompt = self._build_legacy_prompt(query, effective_context, catalog, history_section, schematic_context, original_query)

        try:
            # FIX: When user explicitly requests a table, use generate_json to output structured data ensuring correct format
            # Use original_query for judgment to avoid false triggers from planner-rewritten English queries (e.g., "specification table")
            table_check_query = original_query or query
            if self._requires_table(table_check_query):
                answer = self._generate_table_answer(table_check_query, context, prompt)
            else:
                lang_instruction = self._get_language_instruction(original_query or query)
                logger.info(f"[SYNTHESIZER] Language instruction: {lang_instruction!r}")
                cfg = settings.CONTEXT_CONFIG
                synthesis_max_tokens = cfg.get("synthesis_max_tokens", 4096)
                raw_answer = self.model_client.generate(
                    prompt,
                    system_prompt=lang_instruction,
                    temperature=0.3,
                    max_tokens=synthesis_max_tokens
                )
                raw_answer = raw_answer.strip()
                if self._is_garbled(raw_answer):
                    logger.error("[SYNTHESIZER] LLM returned garbled or empty output")
                    fallback = self._build_fallback_answer(query, context, sources)
                    return {"answer": fallback, "sources": sources, "structured": False, "confidence": "low"}
                answer = self._post_process_answer(raw_answer)

            self_check_passed = False
            if industry_pack and getattr(industry_pack, "name", "generic") != "generic":
                answer_before_check = answer
                answer = self._self_check(query, answer, context)
                self_check_passed = (answer == answer_before_check)  # unchanged = passed

            confidence = self._assess_confidence(answer, context, self_check_passed, industry_pack=industry_pack)
            return {"answer": answer, "sources": sources, "structured": False, "confidence": confidence}
        except Exception as e:
            logger.error(f"Answer synthesis failed: {e}")
            fallback = self._build_fallback_answer(query, context, sources)
            return {"answer": fallback, "sources": sources, "structured": False, "confidence": "none"}

    def _build_legacy_prompt(self, query, context, catalog, history_section, schematic_context, original_query=None):
        prompt = f"""Answer the user's question based on the information below.{history_section}

Knowledge Base Catalog (lists all available documents; when asked "what information/documents are available", answer based on this entire catalog):
{catalog}

User question: {query}"""

        if schematic_context:
            prompt += f"""

Schematic structured data (precise pin-to-net connection relationships; when answering schematic routing/connection questions, prioritize this data):
{schematic_context}

Retrieved detailed content (may contain supplementary information related to schematics):
{context}"""
        else:
            prompt += f"""

Retrieved detailed content:
{context}"""

        prompt += """

Answer rules:
- If the user asks a global question like what information is in the knowledge base, what documents are available, or to list all content: answer based on the 【Knowledge Base Catalog】, grouping by **document category**, with table columns as "Document Category / Document Name / Main Content"
- If the user asks about schematic routing connections, pin relationships, or where a certain signal connects to: prioritize the 【Schematic Structured Data】 for precise answers
- **Core rule: It is strictly forbidden to answer based on your own knowledge, memory, or external information. You must answer strictly based on the 【Retrieved Detailed Content】 only**
- **Read all paragraphs carefully, not just those where the title matches. The answer may be hidden in any paragraph, table, or annotation in the main text.**
- For specific data queries: answer only based on the retrieved detailed content. Every specific value, model number, or parameter in the answer must have a clear source found in the retrieved document content
- **Critical judgment: If the retrieved document content genuinely does not contain the specific information the user is querying (e.g., the user asks about a specific attribute but the document mentions nothing about that attribute or any related concept), this is not a problem with your capability but an issue of incomplete knowledge base data. In this case, objectively state: "Based on the documents in the current knowledge base, no relevant information was found." Do not attempt to reason, guess, or fabricate**
- Reasonable semantic inference based on document content is allowed (such as synonym substitution, concept inclusion), but inference based on assumptions beyond the document content is strictly forbidden
- **Evidence hierarchy: explicit declarations take precedence over naming conventions.** Do not infer versions, feature support, or performance levels from numbers appearing in identifiers, labels, or file names unless the document explicitly declares that connection. Numbers in identifiers are often internal numbering, not version indicators.
- If the data in the document is a range value (e.g., "32-128"), you must present the range as-is; do not arbitrarily pick a fixed value
- **All tables (including data tables, comparison tables, parameter lists, allocation tables, etc.) MUST use standard Markdown table format (| Col1 | Col2 |). ASCII Art table format (+---+ borders) is strictly forbidden.**
- **When the user asks to draw diagrams, topologies, architecture diagrams, relationship diagrams, or to represent with diagrams: prefer Markdown tables or indented text lists to show hierarchy, connections, mappings, and allocation relationships. If code blocks are needed, simple flowcharts can be drawn with plain text (|, -, >), but tables must still use Markdown format.**
- **When the user asks for comparison, contrast, differences, distinctions, or explicitly requests table output: MUST use a Markdown table for side-by-side comparison, one dimension per row, one product per column. Describing two products' information in separate paragraphs without a comparison table is strictly forbidden.**
- **Only answer what the user explicitly asked. Do not list additional non-qualifying items as contrast or supplementary notes.**
- **Must completely list all qualifying items found in the retrieved results. Do not omit or self-filter by importance.**

Please answer the user's question directly, prioritizing conclusive data.
**Important: Output the final answer directly. It is strictly forbidden to output any non-final content such as thinking process, self-correction, drafts, internal notes, etc. The answer must be clean and directly presentable to the user.**
**Important: The answer must be fully expanded, not overly brief. When the user asks about a feature/module/interface, do not just answer "yes/no" or list names briefly; you must also detail all key performance parameters, supported standards/protocols, performance metrics (speed, capacity, throughput, etc.), and key features found in the document.**
For example: if the user asks whether a capability exists, the answer should include the concrete parameters and supported modes declared in the document, not just a bare confirmation.
**Important: If the retrieved document content genuinely does not contain the specific information the user is querying, objectively state "Based on the documents in the current knowledge base, no relevant information was found." This is an issue of incomplete knowledge base data, not your capability. Fabrication, guessing, or mixing in your own knowledge is strictly forbidden.**
**Note: Cross-document synthesis (such as multi-product comparison tables) does NOT constitute fabrication — placing data recorded in different documents into a single table is expected behavior, as long as the data comes from the actual content of each document.**
"""
        prompt += self._get_language_instruction(original_query or query)
        return prompt


    @staticmethod
    def _get_language_instruction(query: str) -> str:
        """Detect query language and return matching output instruction."""
        cn_chars = len(re.findall(r'[\u4e00-\u9fff]', query))
        if cn_chars > 0:
            return "你是中文助手，所有回答必须使用中文。"
        return "You are an English assistant. All answers must be in English."

    def _build_industry_addon(self, industry_pack, query: str = "") -> str:
        """Extract constraints from industry pack, append to end of legacy prompt"""
        if not industry_pack:
            return ""
        retrieval = getattr(industry_pack, 'retrieval', None)
        if not retrieval:
            return ""
        # Packs may expose the data either as plain attributes or via getter
        # methods (the sample semiconductor pack uses get_retrieval_prompts /
        # get_answer_constraints) — support both.
        prompts = getattr(retrieval, 'prompts', None)
        if prompts is None:
            get_prompts = getattr(retrieval, 'get_retrieval_prompts', None)
            prompts = get_prompts() if callable(get_prompts) else {}
        constraints = getattr(retrieval, 'answer_constraints', None)
        if constraints is None:
            get_constraints = getattr(retrieval, 'get_answer_constraints', None)
            constraints = get_constraints() if callable(get_constraints) else {}

        system_prompt = prompts.get("system_prompt", "") if isinstance(prompts, dict) else ""
        answer_rules = prompts.get("answer_rules", []) if isinstance(prompts, dict) else []
        context_filtering = prompts.get("context_filtering", "") if isinstance(prompts, dict) else ""
        format_constraints = constraints.get("format_constraints", []) if isinstance(constraints, dict) else []
        depth_constraints = constraints.get("depth_constraints", []) if isinstance(constraints, dict) else []
        prohibited_behaviors = constraints.get("prohibited_behaviors", []) if isinstance(constraints, dict) else []

        parts = []
        if system_prompt:
            parts.append(f"【Industry Background】\n{system_prompt.strip()}")
        if context_filtering:
            parts.append(f"【Industry Parameter Filtering Rules】\n{context_filtering.strip()}")
        if answer_rules:
            rules_text = "\n".join(f"- {rule}" for rule in answer_rules)
            parts.append(f"【Industry Answer Rules】\n{rules_text}")
        if format_constraints:
            fmt_text = "\n".join(f"- {c}" for c in format_constraints)
            parts.append(f"【Industry Format Constraints】\n{fmt_text}")
        if depth_constraints:
            depth_text = "\n".join(f"- {c}" for c in depth_constraints)
            parts.append(f"【Industry Depth Constraints】\n{depth_text}")
        if prohibited_behaviors:
            prohibit_text = "\n".join(f"- {c}" for c in prohibited_behaviors)
            parts.append(f"【Industry Prohibited Behaviors】\n{prohibit_text}")

        if not parts:
            return ""
        return "\n\n".join(parts)

    def _self_check(self, query: str, raw_answer: str, context: str) -> str:
        if "not found" in raw_answer.lower() or "not mentioned" in raw_answer.lower():
            return raw_answer

        # V5 FIX: Smart sampling for long document contexts
        # Extract key information from the answer (e.g., article numbers, crime names, etc.), locate relevant evidence in context
        # Avoid missing middle/later content by only sampling the first 5000 chars
        cfg = settings.CONTEXT_CONFIG
        extract_max_chars = cfg.get("context_extract_max_chars", 10000)
        sample_context = self._extract_relevant_context(raw_answer, context, max_chars=extract_max_chars)

        check_prompt = f"""Please review the following answer as a quality inspector.

User question: {query}
Candidate answer: {raw_answer}
Original document content (smart-sampled, containing key evidence paragraphs):
{sample_context}

Determine whether the specific conclusions in the answer are supported by original text evidence. Output JSON:
{{"passed": true/false, "issues": [...], "corrected_answer": "Corrected answer"}}
Output only JSON."""
        try:
            result = self.model_client.generate_json(check_prompt, temperature=0.1, max_tokens=2048)
            if result.get("passed", True) is False:
                corrected = result.get("corrected_answer", "")
                if corrected:
                    logger.info("[SYNTHESIZER] Self-Check found issues, using corrected answer")
                    return corrected
            return raw_answer
        except Exception as e:
            logger.warning(f"[SYNTHESIZER] Self-Check failed: {e}")
            return raw_answer

    def _extract_relevant_context(self, raw_answer: str, context: str, max_chars: int = None) -> str:
        """
        Extract key paragraphs related to the answer from the full context.
        Strategy:
        1. Extract keywords from the answer (e.g., article numbers, specific terms)
        2. Search for the positions of these keywords in the context
        3. Take keyword_sample chars around each keyword as sample (ensures complete articles are included)
        4. Take the first fragment_size chars and last fragment_size chars of the context
        5. Merge, deduplicate, and truncate to max_chars
        """
        cfg = settings.CONTEXT_CONFIG
        if max_chars is None:
            max_chars = cfg.get("context_extract_max_chars", 10000)
        keyword_sample = cfg.get("context_extract_keyword_sample", 3000)
        dedup_window = cfg.get("context_extract_dedup_window", 1000)
        fragment_size = cfg.get("context_extract_fragment_size", 3000)

        # Extract potential keywords: article numbers, crime names, legal terms
        keywords = set()

        # Match article numbers: Article XXX, Article 200-XX
        article_pattern = re.findall(r'第[一二三四五六七八九十百零\d]+条', raw_answer)
        keywords.update(article_pattern)

        # Match crime names: XX Crime
        crime_pattern = re.findall(r'[\u4e00-\u9fa5]{2,6}罪', raw_answer)
        keywords.update(crime_pattern)

        # Match numbers (e.g., amounts, prison terms)
        number_pattern = re.findall(r'\d+年|\d+万元|\d+元以上', raw_answer)
        keywords.update(number_pattern)

        if not keywords:
            # No keywords extracted, fallback to uniform sampling
            return self._uniform_sample(context, max_chars)

        logger.info(f"[SYNTHESIZER] Self-Check keywords: {list(keywords)}")

        # Collect context fragments from all match positions
        fragments = []

        # Include start and end (ensure boundary content is not missed)
        fragments.append((0, min(fragment_size, len(context)), "start"))
        if len(context) > fragment_size:
            fragments.append((max(0, len(context) - fragment_size), len(context), "end"))

        # Search for each keyword's position in the context
        for kw in keywords:
            pos = context.find(kw)
            search_count = 0
            while pos != -1 and search_count < 10:  # Limit each keyword to at most 10 matches
                # Check for overlap with existing fragments
                is_new = True
                for start, end, _ in fragments:
                    if abs(start - pos) < dedup_window or abs(end - pos) < dedup_window:
                        is_new = False
                        break
                if is_new:
                    start = max(0, pos - keyword_sample)
                    end = min(len(context), pos + keyword_sample)
                    fragments.append((start, end, kw))
                pos = context.find(kw, pos + 1)
                search_count += 1

        # Merge all fragments
        parts = []
        for start, end, kw in sorted(fragments, key=lambda x: x[0]):
            parts.append(f"=== Fragment (near {kw}) ===\n{context[start:end]}")

        merged = "\n\n".join(parts)
        if len(merged) > max_chars:
            merged = merged[:max_chars] + "\n... (context truncated)"

        return merged

    def _uniform_sample(self, context: str, max_chars: int) -> str:
        """
        Uniform sampling strategy: divide context into 5 segments, take equal-length samples from each
        """
        if len(context) <= max_chars:
            return context

        segments = 5
        segment_size = len(context) // segments
        sample_per_segment = max_chars // segments

        parts = []
        for i in range(segments):
            start = i * segment_size
            if i == segments - 1:
                end = len(context)
            else:
                end = (i + 1) * segment_size
            # Take the middle portion of each segment
            mid = (start + end) // 2
            s_start = max(start, mid - sample_per_segment // 2)
            s_end = min(end, mid + sample_per_segment // 2)
            parts.append(f"=== Segment {i+1} ===\n{context[s_start:s_end]}")

        return "\n\n".join(parts)

    def _build_fallback_answer(self, query: str, context: str, sources: list[dict]) -> str:
        """When LLM is unavailable, organize retrieval results by document sections as a degraded answer"""
        lines = ["[System Notice: Model service is temporarily unavailable. The following is reference information organized from retrieval results]\n"]
        lines.append(f"**Question**: {query}\n")
        # Split context by document, display by document sections
        if context:
            # Try splitting by document markers like "===== Document:" or "--- of " etc.
            import re
            doc_sections = re.split(r'\n(?=--+\s+of\s+\d+|=====\s+Document:|###\s+Sub-query)', context)
            for section in doc_sections[:6]:  # Show at most 6 segments
                section = section.strip()
                if len(section) < 50:
                    continue
                # Extract title line
                first_line = section.split('\n')[0][:80]
                lines.append(f"\n> **{first_line}**")
                # Extract core content (dedup, remove blank lines)
                content_lines = []
                for line in section.split('\n')[1:]:
                    line = line.strip()
                    if line and len(line) > 5 and not line.startswith('\u00a9') and not line.startswith('Confidential'):
                        content_lines.append(line)
                        if len(content_lines) >= 15:
                            break
                if content_lines:
                    lines.append('\n'.join(content_lines))
        if sources:
            lines.append(f"\n**Reference documents**: {len(sources)} documents")
            for s in sources[:5]:
                title = s.get("title") or s.get("filename") or s.get("doc_id", "")
                pages = s.get("pages", [])
                pages_str = f" (pages {min(pages)}-{max(pages)})" if pages else ""
                lines.append(f"- {title}{pages_str}")
        return "\n".join(lines)

    def _requires_table(self, query: str) -> bool:
        """
        Detect whether the user explicitly requests table output.
        No longer hardcodes keyword lists; uses LLM judgment.
        """
        q = query.lower()
        # Simple rule: query contains 'table' (Chinese '表格' or English) and is not a common datasheet query term like 'specification table'
        if '规格表' in q:
            return False
        has_table_kw = '表格' in q or 'table' in q
        return has_table_kw

    def _generate_table_answer(self, query: str, context: str, base_prompt: str) -> str:
        """
        When user requests a table, use generate_json to get structured data, render as Markdown table on the backend.
        JSON structured output is more reliably followed by models than free-form tables.
        """
        import json
        # Extract model names/entity names from the query
        model_pattern = re.findall(r'(?<![A-Za-z0-9])[A-Za-z]{1,}[-]?[A-Za-z0-9]+(?![A-Za-z0-9])', query)
        entities = [m.replace('-', '').replace(' ', '').upper() for m in model_pattern if len(m) >= 3]
        seen = []
        for e in entities:
            if e not in seen:
                seen.append(e)
        entities = seen[:4]
        cfg = settings.CONTEXT_CONFIG
        direct_max_tokens = cfg.get("direct_generate_max_tokens", 4096)
        # FIX: Single-chip queries are not suitable for comparison tables, fallback to normal answer generation
        if len(entities) < 2:
            logger.info(f"[SYNTHESIZER] Single-chip query ({entities}), skipping table generation, using normal answer")
            raw_answer = self.model_client.generate(base_prompt, temperature=0.3, max_tokens=direct_max_tokens)
            return self._post_process_answer(raw_answer.strip())

        entity_names = ", ".join(entities)
        entity_cols = ', '.join([f'"{e}": "..."' for e in entities])
        json_prompt = f"""Based on the following information, output specification comparison data for {entity_names}.

Requirements:
- Each dimension must include specific parameters for each product
- Parameters must be fully expanded, including specific values, standards, performance metrics
- If a product's documentation does not mention a specific dimension, fill in "Not explicitly mentioned in documentation"

Output JSON:
{{
  "dimensions": ["Specification", {', '.join([json.dumps(e) for e in entities])}],
  "rows": [
    {{"dimension": "Processor", {entity_cols}}},
    ...
  ],
  "summary": "2-3 sentence summary"
}}

{context}
"""
        try:
            # FIX: Use larger max_tokens to avoid JSON truncation
            data = self.model_client.generate_json(json_prompt, temperature=0.1, max_tokens=8192)
            if data and data.get("rows"):
                return self._render_json_table(data)
            logger.warning(f"[SYNTHESIZER] JSON table data incomplete: {data.keys() if data else 'empty'}")
        except Exception as e:
            logger.warning(f"[SYNTHESIZER] JSON table generation failed: {e}")

        # Fallback: let LLM generate normal answer directly
        raw_answer = self.model_client.generate(base_prompt, temperature=0.3, max_tokens=direct_max_tokens)
        return self._post_process_answer(raw_answer.strip())

    def _render_json_table(self, data: dict) -> str:
        """Render JSON data as a Markdown table"""
        dimensions = data.get("dimensions", [])
        rows = data.get("rows", [])
        summary = data.get("summary", "")
        if not dimensions or not rows:
            return ""
        lines = []
        # Header
        header = "| " + " | ".join(dimensions) + " |"
        lines.append(header)
        # Separator
        sep = "|" + "|".join([" :--- " for _ in dimensions]) + "|"
        lines.append(sep)
        # Data rows
        for row in rows:
            cells = []
            for dim in dimensions:
                key = "dimension" if dim == "Specification" else None
                if not key:
                    # Try matching column name in dimensions to key in row
                    for k in row.keys():
                        if k.lower() == dim.lower():
                            key = k
                            break
                if not key:
                    key = dim
                val = row.get(key, "")
                val = str(val).replace('\n', '<br>')
                cells.append(val)
            lines.append("| " + " | ".join(cells) + " |")
        result = "\n".join(lines)
        if summary:
            result += f"\n\n{summary}"
        return result

    def _is_garbled(self, text: str) -> bool:
        if not text:
            return True
        if len(text) < 5:
            return False
        question_count = text.count('?')
        if question_count > len(text) * 0.3:
            return True
        return False

    def _assess_confidence(self, answer: str, context: str, self_check_passed: bool = False, industry_pack=None) -> str:
        """Assess answer confidence: high / medium / low / none.

        Uses content-based heuristics (zero extra LLM latency) to provide a
        quality signal to the API consumer.  The score reflects how well the
        answer is supported by retrieved context rather than model guesswork.

        The domain specificity signal (units/proper-noun vocabulary) is
        supplied by the industry pack via get_specificity_vocabulary(); core
        keeps only the mechanism and structural signals (list items, table
        rows, self-check), so with no pack the vocabulary signals are empty.
        """
        if not answer or len(answer.strip()) < 10:
            return "none"

        ans_lower = answer.lower()
        ans_len = len(answer)

        # ── Negative indicators ──
        not_found_markers = [
            "not found", "no relevant", "no information",
            "未找到", "没有找到", "未提及", "不包含", "无相关",
            "does not contain", "does not mention"
        ]
        has_not_found = any(m in ans_lower for m in not_found_markers)

        # ── Specificity indicators ──
        # Domain vocabulary (regex fragments) comes from the industry pack.
        units, terms = [], []
        try:
            retrieval = getattr(industry_pack, 'retrieval', None) if industry_pack else None
            get_vocab = getattr(retrieval, 'get_specificity_vocabulary', None) if retrieval else None
            if callable(get_vocab):
                vocab = get_vocab()
                if isinstance(vocab, dict):
                    units = [u for u in (vocab.get("units") or []) if u]
                    terms = [t for t in (vocab.get("terms") or []) if t]
        except Exception as e:  # non-fatal: fall back to structural signals
            logger.warning(f"[SYNTHESIZER] specificity vocabulary hook failed: {e}")
            units, terms = [], []

        # Count specific data points: numbers with units, domain terms
        specificity = 0
        if units:
            specificity += len(re.findall(r'\d+[\.\d]*\s*(?:' + '|'.join(units) + ')', ans_lower))
        if terms:
            specificity += len(re.findall(r'\b(?:' + '|'.join(terms) + ')\b', ans_lower))
        # Markdown list items indicate structured, detailed answer
        specificity += ans_lower.count('\n- ') + ans_lower.count('\n* ')
        # Table rows
        specificity += ans_lower.count('\n|') // 2

        # ── Truncation context penalty ──
        context_truncated = "[CONTENT TRUNCATED" in context or "[TRUNCATED" in context

        # ── Score ──
        if has_not_found:
            # Check if there's at least SOME useful info mixed in
            if specificity >= 3:
                return "medium"  # partial answer with useful data despite gaps
            return "low"

        if self_check_passed and not context_truncated:
            # Self-check confirmation = strongest signal, but only when context is complete
            return "high"

        if specificity >= 8 and ans_len > 500:
            if context_truncated:
                return "medium"  # truncated context weakens trust in highly-specific answer
            return "high"
        elif specificity >= 4 and ans_len > 200:
            return "medium"
        elif specificity >= 2 and ans_len > 100:
            return "medium"
        elif ans_len > 50:
            return "low"
        else:
            return "low"

    def _post_process_answer(self, text: str) -> str:
        """
        Post-processing: strip LLM thinking process markers, extract clean final answer.
        Some models may output internal thinking text such as Self-Correction, Drafting, etc. in the answer.
        """
        if not text:
            return text

        # 1. Strip common thinking process marker lines
        think_patterns = [
            r'\*?\(?\s*Self-Correction[^\n]*',
            r'\*?\(?\s*Drafting[^\n]*',
            r'\*?\(?\s*Refining[^\n]*',
            r'\*?\(?\s*Ensure[^\n]*',
            r'\*?\(?\s*Check[^\n]*',
            r'\*?\(?\s*Correction[^\n]*',
            r'\*?\(?\s*Note[^\n]*',
            r'\*?\(?\s*Wait[^\n]*',
            r'\*?\(?\s*Actually[^\n]*',
            r'\*?\(?\s*Looking[^\n]*',
            r'\*?\(?\s*I must[^\n]*',
            r'\*?\(?\s*I will[^\n]*',
            r'\*?\(?\s*I need[^\n]*',
            r'\*?\(?\s*Let me[^\n]*',
            r'\*?\(?\s*The text[^\n]*',
            r'\*?\(?\s*Dimensions[^\n]*',
            r'\*?\(?\s*Columns[^\n]*',
            r'\*?\(?\s*Rows[^\n]*',
            r'\*?\(?\s*Compare the main[^\n]*',
            r'\*?\(?\s*Three documents[^\n]*',
            r'\*?\(?\s*Markdown table[^\n]*',
            r'\*?\(?\s*Detailed comparison[^\n]*',
        ]
        cleaned = text
        for pat in think_patterns:
            cleaned = re.sub(pat, '', cleaned, flags=re.IGNORECASE)

        # 2. If the answer contains a Markdown table, prioritize extracting the table and preceding summary paragraphs
        # Find where the first table starts
        table_match = re.search(r'\n\s*\|[^\n]+\|\s*\n\s*\|[-:\s|]+\|', cleaned)
        if table_match:
            start = table_match.start()
            # Keep at most 3 non-empty text lines before the table (usually title/intro)
            prefix = cleaned[:start]
            prefix_lines = [ln for ln in prefix.split('\n') if ln.strip()]
            keep_prefix = '\n'.join(prefix_lines[-3:]) if len(prefix_lines) > 3 else '\n'.join(prefix_lines)
            # Extract the table and content after it
            suffix = cleaned[start:]
            # Strip thinking text after the table
            suffix_lines = []
            for line in suffix.split('\n'):
                if re.match(r'\*?\(?\s*(Self-Correction|Drafting|Refining|Ensure|Check|Note|Wait|Actually|Looking|I must|I will|I need|Let me)', line, re.IGNORECASE):
                    break
                suffix_lines.append(line)
            cleaned = keep_prefix + '\n' + '\n'.join(suffix_lines)

        # 3. Detect ASCII flowcharts/diagrams, wrap with code blocks to prevent marked.js from treating | as table separator
        cleaned = self._wrap_ascii_diagrams(cleaned)

        # 4. Clean up excess blank lines and leading/trailing whitespace
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        cleaned = cleaned.strip()
        return cleaned

    def _wrap_ascii_diagrams(self, text: str) -> str:
        """Detect ASCII flowchart/diagram blocks and wrap with ``` code blocks

        FIX: Strictly exclude Markdown table rows to avoid misidentifying tables as ASCII diagrams.
        Markdown table rows are characterized by starting with '|' (possibly preceded by whitespace).
        """
        lines = text.split('\n')
        result = []
        i = 0
        while i < len(lines):
            stripped = lines[i].lstrip()
            # FIX: Skip Markdown table rows (starting with |), they are not ASCII diagrams
            if stripped.startswith('|'):
                result.append(lines[i])
                i += 1
                continue
            # Look for ASCII diagram block start: lines containing | and + or - (and not Markdown tables)
            if '|' in lines[i] and ('+' in lines[i] or '-' in lines[i]):
                # Scan backwards, collect consecutive ASCII diagram lines (stop on Markdown table rows)
                block_start = i
                while block_start > 0:
                    prev_stripped = lines[block_start - 1].lstrip()
                    if prev_stripped.startswith('|'):
                        break
                    if not ('|' in lines[block_start - 1] or '+' in lines[block_start - 1] or
                            '-' in lines[block_start - 1] or '>' in lines[block_start - 1] or
                            '<' in lines[block_start - 1] or '^' in lines[block_start - 1] or
                            'v' in lines[block_start - 1]):
                        break
                    block_start -= 1
                # Scan forward (stop on Markdown table rows)
                block_end = i
                while block_end < len(lines):
                    next_stripped = lines[block_end].lstrip()
                    if next_stripped.startswith('|'):
                        break
                    if not ('|' in lines[block_end] or '+' in lines[block_end] or
                            '-' in lines[block_end] or '>' in lines[block_end] or
                            '<' in lines[block_end] or '^' in lines[block_end] or
                            'v' in lines[block_end]):
                        break
                    block_end += 1
                block = lines[block_start:block_end]
                # Only process blocks with length >= 3 and at least one line containing + (diagram characteristic)
                if len(block) >= 3 and any('+' in ln for ln in block):
                    result.append('```')
                    result.extend(block)
                    result.append('```')
                    i = block_end
                    continue
            result.append(lines[i])
            i += 1
        return '\n'.join(result)

    # =========================================================================
    # Map-Reduce chunked extraction (prevents LLM from getting lost in long context scenarios)
    # =========================================================================

    def _split_context_into_chunks(self, context: str, chunk_size: int = None) -> list[str]:
        """Split context into chunks by page boundaries, each chunk not exceeding chunk_size"""
        cfg = settings.CONTEXT_CONFIG
        if chunk_size is None:
            chunk_size = cfg.get("map_reduce_chunk_size", 8000)
        if len(context) <= chunk_size:
            return [context]

        # Split by page boundaries: --- Chapter Title (Page N) --- or ===== Document: Title =====
        # Preserve separators as the start of each chunk
        # FIX: Page boundaries have two formats:
        #   --- Chapter Title (Page 5) ---  (with chapter title)
        #   --- Page 5 ---                  (without chapter title)
        #   ===== Document: Title =====      (document title)
        pattern = r'(\n--- .*? ---|\n===== .*? =====)'
        parts = re.split(pattern, context)

        chunks = []
        current = ""
        for part in parts:
            if not part:
                continue
            # If adding this part would exceed chunk_size, save current chunk first
            if current and len(current) + len(part) > chunk_size:
                chunks.append(current.strip())
                current = part
            else:
                current += part
        if current.strip():
            chunks.append(current.strip())

        # Fallback: if split failed (no page boundaries), hard-split by character count
        if not chunks:
            for i in range(0, len(context), chunk_size):
                chunks.append(context[i:i + chunk_size])

        return chunks

    def _map_reduce_extract(self, context: str, query: str, industry_pack=None, original_query: str = None) -> str:
        """Map-Reduce extraction: split long context into chunks, extract from each chunk, merge and deduplicate

        Returns: reduced context (only content directly relevant to the query)
        """
        cfg = settings.CONTEXT_CONFIG
        map_chunk_size = cfg.get("map_reduce_chunk_size", 12000)
        max_chunks = cfg.get("map_reduce_max_chunks", 5)
        chunks = self._split_context_into_chunks(context, chunk_size=map_chunk_size)
        if len(chunks) > max_chunks:
            budget = map_chunk_size * max_chunks
            logger.warning(
                f"[MAP-REDUCE] {len(chunks)} chunks exceeds max {max_chunks}, "
                f"pre-truncating context to {budget} chars"
            )
            context = context[:budget]
            chunks = self._split_context_into_chunks(context, chunk_size=map_chunk_size)
        logger.info(f"[MAP-REDUCE] Context {len(context)} chars, split into {len(chunks)} chunks")

        extracted_parts = []
        for i, chunk in enumerate(chunks, 1):
            map_prompt = f"""You are reading part {i}/{len(chunks)} of a document.

Please find content from this part that is directly relevant to the following question:
{query}

Requirements:
1. Faithfully list all paragraphs/content/information in this part that are related to the question, even if only one or two items
2. Critically important: If a paragraph contains keywords directly related to the user's question, you must list the complete paragraph even if some parts are not relevant
3. Preserve original numbering and key original text (do not rewrite)
4. Do not add explanations, analysis, or commentary; only extract original text
5. If there is genuinely no relevant content at all, only respond with the word: None

Document part:
{chunk}

Extraction result:"""
            try:
                cfg = settings.CONTEXT_CONFIG
                map_max_tokens = cfg.get("synthesis_max_tokens", 4096)
                result = self.model_client.generate(map_prompt, temperature=0.1, max_tokens=map_max_tokens)
                result = result.strip()
                if result and result != "None" and len(result) > 10:
                    extracted_parts.append(result)
                    logger.info(f"[MAP-REDUCE] Chunk {i}/{len(chunks)} extracted {len(result)} chars")
                else:
                    logger.info(f"[MAP-REDUCE] Chunk {i}/{len(chunks)} no relevant content")
            except Exception as e:
                logger.warning(f"[MAP-REDUCE] Chunk {i} extraction failed: {e}")

        if not extracted_parts:
            logger.warning("[MAP-REDUCE] No content extracted from any chunk, falling back to original context")
            return context

        combined = "\n\n".join(extracted_parts)
        logger.info(f"[MAP-REDUCE] Combined {len(combined)} chars")

        # Reduce: deduplicate and organize (if combined result is still long)
        reduce_limit = cfg.get("map_reduce_reduce_limit", 15000)
        if len(combined) > reduce_limit:
            # Batch Reduce: split combined into segments within reduce_limit,
            # reduce each separately, then join results — avoids blindly
            # discarding the tail which often contains the most relevant content.
            reduce_parts = []
            for i in range(0, len(combined), reduce_limit):
                segment = combined[i:i + reduce_limit]
                reduce_prompt = f"""The following is relevant content extracted from different parts of the document. Please perform the following operations:

1. Deduplicate: keep only one copy of the same article/clause (same number)
2. Categorize: group by chapter/category
3. Streamline: remove repetitive expressions, preserve core original text
4. Do not add explanations; only organize existing content

Extracted content:
{segment}

Organized result:"""
                try:
                    cfg = settings.CONTEXT_CONFIG
                    reduce_max_tokens = cfg.get("synthesis_max_tokens", 4096)
                    reduced = self.model_client.generate(reduce_prompt, temperature=0.1, max_tokens=reduce_max_tokens)
                    reduced = reduced.strip()
                    if reduced and len(reduced) > 50:
                        reduce_parts.append(reduced)
                except Exception as e:
                    logger.warning(f"[MAP-REDUCE] Reduce batch {i//reduce_limit} failed: {e}")
                    reduce_parts.append(segment)  # fallback: keep raw segment
            if reduce_parts:
                combined = "\n\n".join(reduce_parts)
                logger.info(f"[MAP-REDUCE] After Reduce: {len(combined)} chars")
                return combined
        elif len(combined) > 12000:
            reduce_prompt = f"""The following is relevant content extracted from different parts of the document. Please perform the following operations:

1. Deduplicate: keep only one copy of the same article/clause (same number)
2. Categorize: group by chapter/category
3. Streamline: remove repetitive expressions, preserve core original text
4. Do not add explanations; only organize existing content

Extracted content:
{combined}

Organized result:"""
            try:
                cfg = settings.CONTEXT_CONFIG
                reduce_max_tokens = cfg.get("synthesis_max_tokens", 4096)
                reduced = self.model_client.generate(reduce_prompt, temperature=0.1, max_tokens=reduce_max_tokens)
                reduced = reduced.strip()
                if reduced and len(reduced) > 50:
                    logger.info(f"[MAP-REDUCE] After Reduce: {len(reduced)} chars")
                    return reduced
            except Exception as e:
                logger.warning(f"[MAP-REDUCE] Reduce failed: {e}")

        return combined

    def _synthesize_version_compare(self, query, plan, context, sources, chat_history=None, routed_category=None, original_query=None):
        return self._synthesize_standard(query, plan, context, sources, chat_history, routed_category, original_query)

    def _synthesize_cross_reference(self, query, plan, context, sources, chat_history=None, routed_category=None, original_query=None):
        return self._synthesize_standard(query, plan, context, sources, chat_history, routed_category, original_query)
