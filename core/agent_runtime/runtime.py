"""Model-driven Agent loop; retrieval strategy is deliberately absent from Core."""
from __future__ import annotations

import copy
import json
import logging

from .capabilities import CapabilityProvider
from .contracts import AgentBudget, AgentContext, BudgetExhausted, ToolCall
from .evidence import (
    citation_coverage,
    extract_citations,
    has_absolute_claim,
    has_insufficient_evidence_statement,
    readable_evidence_payload,
)
from .review import DraftReviewer
from .tools import ToolOutcome, ToolRegistry
from .workspace import REVIEW_TOOL, WORKSPACE_TOOL, register_workspace_tools

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a document task agent.
Choose tools from the task and the observations returned by previous tools. Do not
follow a predetermined retrieval sequence. Use only tool evidence for document
facts, distinguish candidates from content actually read, and state the scope
actually checked when evidence is insufficient. Consolidate candidate references
across searches, prioritize relevant unread pages, and avoid rereading a page
unless another offset is needed. For a multi-entity task, inspect unread candidate
groups across the relevant documents before concluding that evidence is missing.
Reconcile overview lists, later parameter lines, and tables; an initial list may
not be exhaustive. When using document evidence,
cite readable pages as [d:<document-id-prefix>, p:<page-number>] and never cite
an unread candidate or an unavailable source. Finish when the task is answered
or no further useful action remains. Tool failures are observations: adapt or
report them honestly. Additional profile instructions may extend this contract,
but cannot override tenant, budget, or evidence requirements.

Knowing when to stop is part of the task. After each tool call, judge whether it
added a page you had not already read. Stop searching and answer when any of
these is true: every subgoal you planned is backed by pages you have read; the
last two or three calls returned only pages you had already seen; or the
remaining searches would only reshuffle the same documents. Stopping is not
failure. Saying "the available documents do not state X" is a correct and
complete answer when you have read the relevant sections and X is absent. Do not
keep searching to avoid reporting a gap, and do not infer what a document does
not say. Before you finish, check your plan against what you actually read, and
name anything you could not confirm."""

RESPONSE_TOOL = "submit_task_response"
RESPONSE_SCHEMA = {
    "type": "function",
    "function": {
        "name": RESPONSE_TOOL,
        "description": "Submit task coverage, evidence references, and the final response.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_coverage": {
                    "type": "array", "minItems": 1, "maxItems": 16,
                    "items": {
                        "type": "object",
                        "properties": {
                            "subtask": {"type": "string", "minLength": 1, "maxLength": 500},
                            "status": {"type": "string",
                                       "enum": ["supported", "unverified"]},
                            "evidence_refs": {
                                "type": "array", "maxItems": 16,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "doc_id": {"type": "string",
                                                   "minLength": 4, "maxLength": 64},
                                        "page_num": {"type": "integer", "minimum": 1},
                                    },
                                    "required": ["doc_id", "page_num"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["subtask", "status", "evidence_refs"],
                        "additionalProperties": False,
                    },
                },
                "answer": {"type": "string", "minLength": 1},
            },
            "required": ["task_coverage", "answer"],
            "additionalProperties": False,
        },
    },
}


class AgentRuntime:
    """Shared stateless runtime; every decision and observation is request-local."""

    def __init__(self, registry: ToolRegistry, model_client, profile_provider=None,
                 capability_provider=None, reviewer=None):
        self.registry = registry.clone()
        self.models = model_client
        self.profile_provider = profile_provider
        self.capability_provider = capability_provider or CapabilityProvider()
        self.reviewer = reviewer or DraftReviewer(model_client)
        register_workspace_tools(self.registry, self.reviewer.review)

    def run(self, question: str, *, tenant_id: str, chat_history=None,
            budget: AgentBudget | None = None,
            profile_instructions: str = "", profile_id: str | None = None,
            capability_id: str | None = None) -> dict:
        capabilities = self.capability_provider.resolve(capability_id)
        profile, resolved_id, version = self._resolve_profile(
            profile_id, profile_instructions, capabilities.profile_prompt_mode)
        context = AgentContext(
            tenant_id=tenant_id, question=question,
            chat_history=tuple(chat_history or ()), budget=budget or AgentBudget(),
            capabilities=capabilities,
            profile=profile, profile_id=resolved_id, profile_version=version)
        messages = self._initial_messages(context)
        try:
            while True:
                # The loop must terminate on its own terms. Two independent
                # counters trigger the answer phase: the step budget, and the
                # number of pages already gathered. Waiting for the model to
                # decide it is done does not work - it keeps searching.
                out_of_steps = (
                    context.steps + 1 >= context.budget.max_steps
                    or context.model_calls + 1 >= context.budget.max_model_calls
                    or context.tool_calls >= context.budget.max_tool_calls)
                enough_evidence = self._collection_complete(context)
                finalization_only = out_of_steps or enough_evidence
                messages[0]["content"] = self._system_content(context, finalization_only)
                if not (finalization_only and context.finalization_retry):
                    # Finalization retries are corrective turns on an answer the
                    # model already gathered evidence for; they must not be billed
                    # against the exploration budget or the loop cannot finish.
                    context.consume_step()
                    context.consume_model()
                structured_response = (
                    finalization_only and context.capabilities.finalization_mode
                    == "structured_evidence")
                request_messages = (
                    self._finalization_messages(context)
                    if finalization_only and context.capabilities.finalization_mode
                    in {"evidence_summary", "structured_evidence"} else messages)
                planning_only = (
                    context.capabilities.workspace_enabled
                    and not context.workspace.plan and not context.observations
                    and not finalization_only)
                active_schemas = (
                    [RESPONSE_SCHEMA] if structured_response
                    else [] if finalization_only
                    else self._planning_schemas(context) if planning_only
                    else self._schemas(context))
                no_tools = not active_schemas
                reply = self.models.chat_with_tools(
                    request_messages,
                    active_schemas,
                    max_tokens=(context.budget.max_response_tokens
                                if finalization_only or planning_only or no_tools
                                else context.budget.max_decision_tokens),
                    temperature=0.0,
                    tool_choice=(
                        "required" if (structured_response or planning_only) and not no_tools
                        else "none" if finalization_only or no_tools else "auto"))
                raw_calls = list(reply.tool_calls or ())
                context.decisions.append({
                    "step": context.steps,
                    "tools": [call.get("function", {}).get("name", "")
                              for call in raw_calls if isinstance(call, dict)],
                    "finish_reason": reply.finish_reason,
                    "has_content": bool((reply.text or "").strip()),
                    "finalization_only": finalization_only,
                    "planning_only": planning_only,
                    "finalization_channel": (
                        context.capabilities.finalization_mode if finalization_only else "native"),
                })
                if reply.hit_token_cap:
                    return self._result(
                        context, "degraded", False, "",
                        "model decision reached the token cap")
                if structured_response:
                    answer, response_error = self._parse_structured_response(
                        context, raw_calls)
                    if response_error:
                        return self._result(context, "degraded", False, "", response_error)
                    context.decisions[-1]["response_contract"] = "task_coverage"
                    context.decisions[-1]["task_coverage"] = context.task_coverage
                    return self._result(context, "completed", True, answer, None)
                if not raw_calls:
                    answer = (reply.text or "").strip()
                    markup = next((marker for marker in (
                        "<tool_call>", "<function=", "</parameter>", "<|tool_call",
                        "</tool_call>") if marker in answer), None)
                    if (finalization_only and (markup or not answer)
                            and context.finalization_retry < 3):
                        # Finalization asked for prose but got markup or nothing.
                        # Retry with a blunt instruction before giving up.
                        context.finalization_retry += 1
                        context.decisions[-1]["finalization_retry"] = True
                        if answer:
                            messages.append({"role": "assistant", "content": answer})
                        messages.append({"role": "user", "content": (
                            "No tools are available. Reply with the final answer as "
                            "plain prose only: what the documents you read say, with "
                            "[d:<doc_id>, p:<page>] citations, and what they do not "
                            "cover. Do not write tool markup and do not reply empty.")})
                        continue
                    if markup and not finalization_only:
                        # The model tried to call a tool but emitted markup as text
                        # instead of structured tool_calls. Tell it so it can retry
                        # properly rather than ending the task with no answer.
                        if (context.policy_feedback < context.budget.max_policy_feedback
                                and context.steps < context.budget.max_steps
                                and context.model_calls < context.budget.max_model_calls):
                            context.policy_feedback += 1
                            context.decisions[-1]["policy_feedback"] = "tool_markup_as_text"
                            messages.append({"role": "assistant", "content": answer})
                            messages.append({"role": "user", "content": (
                                "Your previous reply contained tool-call markup as plain "
                                "text, so no tool ran. Emit the call through the tool-call "
                                "interface instead of writing markup in the message body, "
                                "or give the final answer with no markup.")})
                            continue
                        return self._result(
                            context, "degraded", False, "",
                            "model emitted tool markup instead of a tool call")
                    if not answer:
                        if finalization_only and context.finalization_retry < 2:
                            context.finalization_retry += 1
                            messages.append({"role": "user", "content": (
                                "Write the final answer now as plain prose using the "
                                "evidence already read. Do not call tools and do not "
                                "return an empty message.")})
                            continue
                        return self._result(
                            context, "degraded", False, "", "empty model response")
                    if finalization_only and markup:
                        # Native finalization asked for prose but the model still
                        # produced tool markup. Give it a corrective turn instead of
                        # discarding the evidence it already gathered.
                        if (context.finalization_retry
                                < context.budget.max_policy_feedback):
                            context.finalization_retry += 1
                            context.decisions[-1]["finalization_retry"] = True
                            messages.append({"role": "assistant", "content": answer})
                            messages.append({"role": "user", "content": (
                                "Your reply contained tool-call markup, but no tools are "
                                "available now. Write the final answer as plain prose "
                                "using the evidence you have already read, with "
                                "[d:<doc_id>, p:<page>] citations. Do not emit markup.")})
                            continue
                        return self._result(
                            context, "degraded", False, "",
                            "model emitted tool markup instead of a final response")
                    policy_error = self._response_policy_error(context, answer)
                    # Absolute-claim review is a policy gate the model cannot
                    # resolve by rewriting; everything else is worth one more try.
                    retryable = policy_error is not None and policy_error != (
                        "absolute claims require claim-level evidence review")
                    if (retryable and finalization_only
                            and context.finalization_retry < 3):
                        context.finalization_retry += 1
                        context.decisions[-1]["policy_feedback"] = policy_error
                        messages.append({"role": "assistant", "content": answer})
                        messages.append({"role": "user", "content": (
                            "Your answer was rejected: " + str(policy_error)
                            + ". Rewrite it as plain prose. Cite every fact you take "
                              "from a document as [d:<doc-id>, p:<page>] using only "
                              "pages you actually read, or say explicitly that the "
                              "documents do not cover it.")})
                        continue
                    if (retryable
                            and context.policy_feedback < context.budget.max_policy_feedback
                            and context.steps < context.budget.max_steps
                            and context.model_calls < context.budget.max_model_calls):
                        context.policy_feedback += 1
                        context.decisions[-1]["policy_feedback"] = policy_error
                        messages.append({"role": "assistant", "content": answer})
                        messages.append({"role": "user", "content": (
                            "Response policy feedback: " + policy_error
                            + ". Continue the task with available tools or provide a "
                              "corrected response; do not repeat unsupported citations.")})
                        continue
                    return self._result(context, "completed", True, answer, None)

                messages.append({
                    "role": "assistant", "content": reply.text or "",
                    "tool_calls": raw_calls,
                })
                for index, raw in enumerate(raw_calls):
                    call, parse_error = self._parse_call(raw, index)
                    if parse_error:
                        outcome = ToolOutcome(
                            call.call_id, call.name,
                            {"ok": False, "error": {
                                "kind": "invalid_action", "message": parse_error}},
                            0.0)
                    else:
                        outcome = self.registry.execute(
                            context, call, allowed_tools=self._allowed_tools(context))
                    context.observations.append({
                        "step": context.steps, "call_id": outcome.call_id,
                        "tool": outcome.name, "args": call.args,
                        "result": outcome.result, "elapsed_s": outcome.elapsed_s,
                    })
                    messages.append({
                        "role": "tool", "tool_call_id": outcome.call_id,
                        "name": outcome.name,
                        "content": self._compact(
                            outcome.result, context.budget.max_observation_chars),
                    })
                    repeat_note = self._repeat_call_note(context, outcome.name, call.args)
                    if repeat_note:
                        messages.append({"role": "user", "content": repeat_note})
                    fresh = self._record_seen_pages(context, outcome)
                    yield_note = (self._marginal_yield_note(context, fresh)
                                  if fresh is not None else None)
                    if yield_note:
                        messages.append({"role": "user", "content": yield_note})
        except BudgetExhausted as e:
            return self._result(context, "budget_exhausted", False, "", str(e))
        except Exception as e:
            logger.warning("Agent execution failed for tenant %s: %s",
                           context.tenant_id, e)
            return self._result(context, "degraded", False, "", str(e))

    def _allowed_tools(self, context: AgentContext) -> set[str]:
        allowed = self.registry.names()
        if not context.capabilities.workspace_enabled:
            allowed.discard(WORKSPACE_TOOL)
            allowed.discard(REVIEW_TOOL)
        if not context.capabilities.review_enabled:
            allowed.discard(REVIEW_TOOL)
        if self._collection_complete(context):
            # Enough pages are in hand to answer. Stop offering retrieval so the
            # remaining steps go to writing instead of more searching.
            return set()
        return allowed

    @staticmethod
    def _collection_complete(context: AgentContext) -> bool:
        """True once the agent has gathered enough pages to answer.

        Document tasks need a bounded evidence set, not exhaustive recall: a
        dozen pages covers the sections these questions target, and the evidence
        budget cannot hold much more. Past that point more searching only
        re-finds the same pages, so the loop should switch to answering.

        Count only pages whose content was actually read. Search results are
        candidates, not evidence: counting them would let a few broad queries
        hit the ceiling before anything is read, and the answer would then have
        nothing to cite.
        """
        read = {item.ref for item in context.evidence.readable_items()}
        return len(read) >= context.budget.max_evidence_pages

    @staticmethod
    def _record_seen_pages(context: AgentContext, outcome) -> int | None:
        """Return how many newly read pages this call added, or None if not a read.

        Progress means new *content*, not new candidates. A search that returns
        the same pages already read is not progress even if the candidate list
        differs, so the baseline is the readable set before and after the call.
        """
        before = context.readable_before
        after = {item.ref for item in context.evidence.readable_items()}
        context.readable_before = after
        result = outcome.result or {}
        if not isinstance(result, dict) or not result.get("ok"):
            return None
        items = result.get("items")
        if not isinstance(items, list):
            return None
        paged = [item for item in items if isinstance(item, dict)
                 and item.get("doc_id") is not None and item.get("page_num") is not None]
        if not paged:
            return None
        fresh = len(after - before)
        context.seen_pages = after
        return fresh

    def _marginal_yield_note(self, context: AgentContext, fresh: int) -> str | None:
        """Tell the agent when a tool call produced no new pages.

        The agent cannot otherwise tell whether it is making progress, so it
        keeps searching. Reporting the delta lets it decide to stop and state
        what is missing instead of exhausting the budget.
        """
        if fresh:
            context.stalled_rounds = 0
            return None
        context.stalled_rounds += 1
        if context.stalled_rounds == 2:
            return ("Your last 2 calls returned only pages you had already seen. If a "
                    "third attempt produces nothing new, stop searching and answer "
                    "from the evidence already read, naming what the documents do "
                    "not cover.")
        if context.stalled_rounds >= 3:
            return ("Repeated calls are returning nothing new. Stop searching now and "
                    "give the final answer from the pages you have read. State "
                    "explicitly which parts the available documents do not cover "
                    "instead of searching further.")
        return None

    def _repeat_call_note(self, context: AgentContext, name: str,
                          args: dict) -> str | None:
        """Warn when the same call keeps repeating without new information.

        Repeated identical calls usually mean the agent is stuck cycling
        synonyms for a fact the corpus does not contain. Surfacing the count
        lets it decide to widen, change tool, or report the gap.
        """
        signature = (name, json.dumps(args, sort_keys=True, default=str))
        seen = 0
        for observation in context.observations:
            if (observation["tool"], json.dumps(
                    observation["args"], sort_keys=True, default=str)) == signature:
                seen += 1
        if seen == 3:
            return ("You have now made this identical call 3 times with the same "
                    "result. Repeating it again will not produce new evidence. Either "
                    "use a different tool or different query, or state that the "
                    "documents do not contain this information and answer from what "
                    "you have read.")
        if seen == 5:
            return ("Identical call repeated 5 times. Stop repeating it. Answer now "
                    "with the evidence already gathered, explicitly noting what the "
                    "available documents do not cover.")
        return None

    def _schemas(self, context: AgentContext) -> list[dict]:
        allowed = self._allowed_tools(context)
        return [schema for schema in self.registry.schemas()
                if schema["function"]["name"] in allowed]

    def _planning_schemas(self, context: AgentContext) -> list[dict]:
        """Restrict the first decision to the workspace plan so the task is
        decomposed before any retrieval happens."""
        return [schema for schema in self.registry.schemas()
                if schema["function"]["name"] == WORKSPACE_TOOL]

    def _resolve_profile(self, profile_id: str | None,
                         instructions: str, prompt_mode: str) -> tuple[str, str, str]:
        if profile_id and instructions:
            raise ValueError("profile_id and profile_instructions are mutually exclusive")
        if instructions:
            return instructions, "manual", ""
        if self.profile_provider is not None:
            profile = self.profile_provider.resolve(profile_id, prompt_mode=prompt_mode)
            return profile.instructions, profile.profile_id, profile.version
        if profile_id:
            raise ValueError("a profile provider is required when profile_id is used")
        return "", "", ""

    def _finalization_messages(self, context: AgentContext) -> list[dict]:
        messages = self._initial_messages(context)
        messages[0]["content"] = self._system_content(context, True)
        evidence = readable_evidence_payload(
            context.evidence, context.budget.max_evidence_chars)
        scope = [{
            "tool": observation["tool"], "args": observation["args"],
            "ok": observation["result"].get("ok", False),
            "stats": observation["result"].get("stats", {}),
            "health": observation["result"].get("health", []),
        } for observation in context.observations[-16:]]
        messages.append({"role": "user", "content": (
            "Readable evidence and retrieval scope for the final response:\n"
            + json.dumps({"readable_evidence": evidence, "retrieval_scope": scope,
                          "agent_workspace": context.workspace.snapshot()},
                         ensure_ascii=False, default=str)
            + "\nAnswer the original task now. Cover every requested entity and dimension, "
              "without adding unrelated capabilities. Use only readable evidence for "
              "document facts and include [d:<document-id>, p:<page>] citations. "
              "Before stating that something is unverified, check all supplied content. "
              "An item marked truncated=true has omitted text: never treat that omitted "
              "text as evidence of absence. State only the scope actually checked; do "
              "not turn missing evidence into an absolute claim of absence."
            + (" Call submit_task_response exactly once: enumerate each requested subtask "
               "with supported/unverified status and readable evidence references, then "
               "provide the final answer."
               if context.capabilities.finalization_mode == "structured_evidence"
               else " Provide the best supported final response now."))})
        return messages

    def _system_content(self, context: AgentContext,
                        finalization_only: bool = False) -> str:
        system = SYSTEM_PROMPT
        if self._collection_complete(context):
            system += (
                f"\n\nEVIDENCE COMPLETE: you have read {len(context.seen_pages)} pages, "
                "which is enough for this task. Retrieval tools are now withdrawn. "
                "Do not look for more evidence. Write the final answer now from the "
                "pages already read, cite them as [d:<document-id-prefix>, "
                "p:<page-number>], and list the specific facts the documents do not "
                "cover. Every factual sentence taken from a document must carry a "
                "citation; an answer without citations will be rejected.")
        if context.profile:
            label = context.profile_id or "manual"
            if context.profile_version:
                label += "@" + context.profile_version
            system += f"\n\nProfile instructions ({label}):\n" + context.profile[:4000]
        needs_plan = (context.capabilities.workspace_enabled
                      and not context.workspace.plan and not context.observations)
        if needs_plan:
            system += (
                "\n\nPLANNING STEP: Before any retrieval, call update_workspace with a "
                "concrete plan listing every subgoal the task requires, including each "
                "entity and each requested dimension. Only one field is needed now: plan. "
                "No other tool is available in this step. The plan you write is carried "
                "forward and shown on every later step, so make it an actionable "
                "checklist you can track.")
        elif context.capabilities.workspace_enabled:
            system += (
                "\n\nFor a task with multiple requested parts, maintain a compact plan "
                "and a draft or structured outline with update_workspace. Once useful "
                "readable evidence exists, review that draft before finalizing when "
                "budget allows. Use concrete review gaps to decide the next action "
                "instead of repeatedly broadening searches. Simple tasks need none of "
                "these stages.")
        if context.capabilities.review_enabled and not needs_plan:
            system += (
                " You may call review_draft to obtain task-completeness and evidence "
                "feedback. Treat the review as advisory and choose whether to read "
                "more, revise, or finish.")
        readable_refs = {
            (item.doc_id, item.page_num) for item in context.evidence.readable_items()}
        unread_candidates = [{
            "doc_id": item.doc_id, "page_num": item.page_num,
            "section_title": item.section_title,
        } for item in context.evidence.items.values() if item.ref not in readable_refs]
        candidate_groups = {}
        for item in unread_candidates:
            group = candidate_groups.setdefault(item["doc_id"], {
                "doc_id": item["doc_id"], "unread_count": 0, "candidates": []})
            group["unread_count"] += 1
            if len(group["candidates"]) < 6:
                group["candidates"].append({
                    "page_num": item["page_num"], "section_title": item["section_title"]})
        workspace = {
            "agent_workspace": {
                "plan": context.workspace.plan[:2000],
                "notes": context.workspace.notes[-5:],
                "draft_chars": len(context.workspace.draft),
                "review_calls": context.workspace.review_calls,
            },
            "your_plan": context.workspace.plan[:2000] or None,
            "your_done": context.workspace.done[-20:],
            "candidates": len(context.evidence.items),
            "stalled_rounds": context.stalled_rounds,
            "pages_read": len(context.seen_pages),
            "unread_candidate_count": len(unread_candidates),
            "unread_candidate_documents": list(candidate_groups.values()),
            "unread_candidates": unread_candidates[:24],
            "readable_evidence": [{
                "doc_id": item.doc_id, "page_num": item.page_num,
                "segments": [{"offset": segment["offset"], "chars": len(segment["content"])}
                             for segment in item.segments],
            } for item in context.evidence.readable_items()],
            "failed_observations": sum(
                not observation["result"].get("ok", False)
                for observation in context.observations),
            "remaining_budget": {
                "steps": context.budget.max_steps - context.steps,
                "model_calls": context.budget.max_model_calls - context.model_calls,
                "tool_calls": context.budget.max_tool_calls - context.tool_calls,
            },
        }
        return system + "\n\nCurrent workspace and remaining budget:\n" + json.dumps(
            workspace, ensure_ascii=False) + (
                "\n\nTool budget is exhausted. Do not call tools or emit tool markup. "
                "Provide the best supported final response now, or state what "
                "remains unverified." if finalization_only else "")

    def _initial_messages(self, context: AgentContext) -> list[dict]:
        messages = [{"role": "system", "content": self._system_content(context)}]
        history = context.chat_history[-context.budget.max_history_items:]
        for item in history:
            if not isinstance(item, dict) or item.get("role") not in {
                    "user", "assistant"}:
                continue
            content = str(item.get("content") or "")[
                :context.budget.max_history_chars].strip()
            if content:
                messages.append({"role": item["role"], "content": content})
        messages.append({"role": "user", "content": context.question})
        return messages

    def _parse_structured_response(self, context: AgentContext,
                                   raw_calls: list) -> tuple[str, str | None]:
        if len(raw_calls) != 1:
            return "", "model did not submit exactly one structured response"
        call, parse_error = self._parse_call(raw_calls[0], 0)
        if parse_error or call.name != RESPONSE_TOOL:
            return "", "invalid structured response: " + (parse_error or call.name)
        answer = str(call.args.get("answer") or "").strip()
        coverage = call.args.get("task_coverage")
        if not answer or not isinstance(coverage, list) or not 1 <= len(coverage) <= 16:
            return "", "structured response requires an answer and 1-16 coverage items"
        normalized, references = [], []
        for item in coverage:
            if not isinstance(item, dict):
                return "", "structured coverage items must be objects"
            subtask = str(item.get("subtask") or "").strip()
            status = item.get("status")
            refs = item.get("evidence_refs")
            if (not subtask or status not in {"supported", "unverified"}
                    or not isinstance(refs, list) or len(refs) > 16):
                return "", "invalid structured coverage item"
            normalized_refs = []
            for ref in refs:
                doc_id = str(ref.get("doc_id") or "").strip() if isinstance(ref, dict) else ""
                page_num = ref.get("page_num") if isinstance(ref, dict) else None
                if (not 4 <= len(doc_id) <= 64 or isinstance(page_num, bool)
                        or not isinstance(page_num, int) or page_num < 1):
                    return "", "invalid structured evidence reference"
                normalized_ref = {"doc_id": doc_id, "page_num": page_num}
                normalized_refs.append(normalized_ref)
                pair = (doc_id, page_num)
                if pair not in references:
                    references.append(pair)
            if status == "supported" and not normalized_refs:
                return "", "supported coverage items require readable evidence references"
            normalized.append({"subtask": subtask, "status": status,
                               "evidence_refs": normalized_refs})
        context.task_coverage = normalized
        inline = set(extract_citations(answer))
        missing = [ref for ref in references if ref not in inline]
        if missing:
            answer += "\n\nEvidence references:\n" + "\n".join(
                f"[d:{doc_id}, p:{page_num}]" for doc_id, page_num in missing)
        return answer, None

    @staticmethod
    def _parse_call(raw: dict, index: int) -> tuple[ToolCall, str | None]:
        if not isinstance(raw, dict):
            return ToolCall(f"invalid-{index}", "invalid_tool", {}), (
                "tool call envelope must be an object")
        call_id = str(raw.get("id") or f"call-{index}")
        function = raw.get("function") or {}
        name = str(function.get("name") or "invalid_tool")
        arguments = function.get("arguments", "{}")
        try:
            args = json.loads(arguments) if isinstance(arguments, str) else arguments
            if not isinstance(args, dict):
                raise ValueError("arguments must decode to an object")
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return ToolCall(call_id, name, {}), str(e)[:300]
        return ToolCall(call_id, name, args), None

    @staticmethod
    def _compact(value: dict, limit: int) -> str:
        text = json.dumps(value, ensure_ascii=False, default=str)
        if len(text) <= limit:
            return text
        compact = copy.deepcopy(value)
        items = compact.get("items")
        if isinstance(items, list) and items:
            # Item refs already carry the same identity as evidence_refs; keep
            # every candidate visible instead of dropping the tail of the set.
            compact.pop("evidence_refs", None)
            per_item = max(80, (limit - 1500) // len(items))
            for item in items:
                if not isinstance(item, dict):
                    continue
                for key in ("content", "excerpt", "source_text"):
                    if isinstance(item.get(key), str) and len(item[key]) > per_item:
                        item[key] = item[key][:per_item] + "…[content truncated]"
            compact["observation_truncated"] = True
            text = json.dumps(compact, ensure_ascii=False, default=str)
            if len(text) <= limit:
                return text
            for item in items:
                if isinstance(item, dict):
                    for key in ("content", "excerpt", "source_text"):
                        if isinstance(item.get(key), str):
                            item[key] = item[key][:80]
            compact["observation_items_compacted"] = True
            text = json.dumps(compact, ensure_ascii=False, default=str)
            if len(text) <= limit:
                return text
        return text[:limit] + "…[truncated]"

    @staticmethod
    def _response_policy_error(context: AgentContext, answer: str) -> str | None:
        if not answer:
            return None
        if not any(character.isalnum() for character in answer):
            return "model returned an empty or non-informative response"
        audit = context.evidence.audit_answer(answer)
        if audit["unverifiable"]:
            return "answer contains citations to unread or unavailable evidence"
        if audit["malformed_citations"]:
            return "answer contains malformed document citations"
        if audit["ambiguous_page_references"]:
            return "page-only citations are ambiguous across readable documents"
        if (context.evidence.items and not audit["verifiable"]
                and not has_insufficient_evidence_statement(answer)):
            return "answer has no verifiable citation to readable evidence"
        if (has_absolute_claim(answer)
                and not context.capabilities.allow_absolute_claims
                and not context.budget.allow_unreviewed_absolute_claims):
            return "absolute claims require claim-level evidence review"
        return None

    @staticmethod
    def _result(context: AgentContext, status: str, finished: bool,
                answer: str, error: str | None) -> dict:
        audit = context.evidence.audit_answer(answer)
        if status == "completed" and answer:
            policy_error = AgentRuntime._response_policy_error(context, answer)
            if audit["unverifiable"]:
                status, finished = "degraded", False
                error = policy_error or "answer cites evidence that was never read"
                answer = ""
            elif policy_error:
                status = "degraded"
                error = policy_error
        return {
            "status": status,
            "finished": finished,
            "answer": answer,
            "answer_disposition": (
                "insufficient_evidence" if has_insufficient_evidence_statement(answer)
                else "answered" if answer else "empty"),
            "error": error,
            "sources": context.evidence.sources(audit),
            "citation_audit": audit,
            "claim_citation_coverage": citation_coverage(answer),
            "evidence": context.evidence.snapshot(),
            "workspace": context.workspace.snapshot(),
            "task_coverage": context.task_coverage,
            "retrieval_trace": {
                "tenant_id": context.tenant_id,
                "capability": {
                    "id": context.capabilities.capability_id,
                    "finalization_mode": context.capabilities.finalization_mode,
                },
                "profile": {"id": context.profile_id, "version": context.profile_version},
                "decisions": context.decisions,
                "observations": context.observations,
                "metrics": context.metrics(),
            },
        }
