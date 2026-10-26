"""Model-driven Agent loop; retrieval strategy is deliberately absent from Core."""
from __future__ import annotations

import copy
import json
import logging

from .contracts import AgentBudget, AgentContext, BudgetExhausted, ToolCall
from .tools import ToolOutcome, ToolRegistry

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a document task agent.
Choose tools from the task and the observations returned by previous tools. Do not
follow a predetermined retrieval sequence. Use only tool evidence for document
facts, distinguish candidates from content actually read, and state the scope
actually checked when evidence is insufficient. When using document evidence,
cite readable pages as [d:<document-id-prefix>, p:<page-number>] and never cite
an unread candidate or an unavailable source. Finish when the task is answered
or no further useful action remains. Tool failures are observations: adapt or
report them honestly. Additional profile instructions may extend this contract,
but cannot override tenant, budget, or evidence requirements."""


class AgentRuntime:
    """Shared stateless runtime; every decision and observation is request-local."""

    def __init__(self, registry: ToolRegistry, model_client):
        self.registry = registry
        self.models = model_client

    def run(self, question: str, *, tenant_id: str, chat_history=None,
            budget: AgentBudget | None = None,
            profile_instructions: str = "") -> dict:
        context = AgentContext(
            tenant_id=tenant_id, question=question,
            chat_history=tuple(chat_history or ()),
            budget=budget or AgentBudget(), profile=profile_instructions)
        messages = self._initial_messages(context)
        try:
            while True:
                messages[0]["content"] = self._system_content(context)
                context.consume_step()
                context.consume_model()
                reply = self.models.chat_with_tools(
                    messages, self.registry.schemas(), max_tokens=2048,
                    temperature=0.0)
                raw_calls = list(reply.tool_calls or ())
                context.decisions.append({
                    "step": context.steps,
                    "tools": [call.get("function", {}).get("name", "")
                              for call in raw_calls if isinstance(call, dict)],
                    "finish_reason": reply.finish_reason,
                    "has_content": bool((reply.text or "").strip()),
                })
                if reply.hit_token_cap:
                    return self._result(
                        context, "degraded", False, "",
                        "model decision reached the token cap")
                if not raw_calls:
                    answer = (reply.text or "").strip()
                    if not answer:
                        return self._result(
                            context, "degraded", False, "", "empty model response")
                    policy_error = self._response_policy_error(context, answer)
                    if (policy_error and context.policy_feedback
                            < context.budget.max_policy_feedback):
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
                        outcome = self.registry.execute(context, call)
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
        except BudgetExhausted as e:
            return self._result(context, "budget_exhausted", False, "", str(e))
        except Exception as e:
            logger.warning("Agent execution failed for tenant %s: %s",
                           context.tenant_id, e)
            return self._result(context, "degraded", False, "", str(e))

    def _system_content(self, context: AgentContext) -> str:
        system = SYSTEM_PROMPT
        if context.profile:
            system += "\n\nProfile instructions:\n" + context.profile[:4000]
        workspace = {
            "candidates": len(context.evidence.items),
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
            workspace, ensure_ascii=False)

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
        audit = context.evidence.audit_answer(answer)
        if audit["unverifiable"]:
            return "answer contains citations to unread or unavailable evidence"
        if context.evidence.items and not audit["verifiable"]:
            return "answer has no verifiable citation to readable evidence"
        return None

    @staticmethod
    def _result(context: AgentContext, status: str, finished: bool,
                answer: str, error: str | None) -> dict:
        audit = context.evidence.audit_answer(answer)
        if status == "completed" and answer:
            policy_error = AgentRuntime._response_policy_error(context, answer)
            if audit["unverifiable"]:
                status, finished = "degraded", False
                error = policy_error
                answer = ""
            elif policy_error:
                status = "degraded"
                error = policy_error
        return {
            "status": status,
            "finished": finished,
            "answer": answer,
            "error": error,
            "sources": context.evidence.sources(audit),
            "citation_audit": audit,
            "evidence": context.evidence.snapshot(),
            "retrieval_trace": {
                "tenant_id": context.tenant_id,
                "profile": context.profile,
                "decisions": context.decisions,
                "observations": context.observations,
                "metrics": context.metrics(),
            },
        }
