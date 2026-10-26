"""Model-driven Agent loop; retrieval strategy is deliberately absent from Core."""
from __future__ import annotations

import json
import logging

from .contracts import AgentBudget, AgentContext, BudgetExhausted, ToolCall
from .tools import ToolOutcome, ToolRegistry

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a document task agent.
Choose tools from the task and the observations returned by previous tools. Do not
follow a predetermined retrieval sequence. Use only tool evidence for document
facts, distinguish candidates from content actually read, and state the scope
actually checked when evidence is insufficient. Finish when the task is answered
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

    def _initial_messages(self, context: AgentContext) -> list[dict]:
        system = SYSTEM_PROMPT
        if context.profile:
            system += "\n\nProfile instructions:\n" + context.profile[:4000]
        messages = [{"role": "system", "content": system}]
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
        return text if len(text) <= limit else text[:limit] + "…[truncated]"

    @staticmethod
    def _result(context: AgentContext, status: str, finished: bool,
                answer: str, error: str | None) -> dict:
        return {
            "status": status,
            "finished": finished,
            "answer": answer,
            "error": error,
            "retrieval_trace": {
                "tenant_id": context.tenant_id,
                "profile": context.profile,
                "decisions": context.decisions,
                "observations": context.observations,
                "metrics": context.metrics(),
            },
        }
