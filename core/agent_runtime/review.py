"""Optional model-driven draft review; never an automatic retrieval stage."""
import json

from .contracts import AgentContext

REVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "submit_draft_review",
        "description": "Return a structured task-and-evidence review.",
        "parameters": {
            "type": "object",
            "properties": {
                "complete": {"type": "boolean"},
                "missing_subtasks": {"type": "array", "maxItems": 8,
                                     "items": {"type": "string", "maxLength": 500}},
                "unsupported_claims": {"type": "array", "maxItems": 8,
                                       "items": {"type": "string", "maxLength": 500}},
                "contradictions": {"type": "array", "maxItems": 8,
                                   "items": {"type": "string", "maxLength": 500}},
                "suggested_actions": {"type": "array", "maxItems": 8,
                                      "items": {"type": "string", "maxLength": 500}},
            },
            "required": ["complete", "missing_subtasks", "unsupported_claims",
                         "contradictions", "suggested_actions"],
            "additionalProperties": False,
        },
    },
}


class DraftReviewer:
    def __init__(self, model_client):
        self.models = model_client

    def review(self, context: AgentContext, args: dict) -> dict:
        if context.workspace.review_calls >= context.budget.max_review_calls:
            return {"ok": False, "error": {
                "kind": "review_budget_exhausted",
                "message": "draft review budget exhausted"}}
        draft = str(args.get("draft") or context.workspace.draft).strip()
        if not draft:
            return {"ok": False, "error": {
                "kind": "missing_draft", "message": "no draft supplied or stored"}}
        context.workspace.review_calls += 1
        context.consume_model()
        payload = {
            "original_task": context.question,
            "history": list(context.chat_history[-4:]),
            "agent_plan": context.workspace.plan,
            "agent_notes": context.workspace.notes[-8:],
            "draft": draft,
            "readable_evidence": self._evidence(context),
        }
        messages = [
            {"role": "system", "content": (
                "Review a draft against the original task and supplied readable evidence. "
                "Identify omitted task parts, unsupported claims, and contradictions. Do "
                "not invent facts or prescribe a fixed retrieval workflow. Suggested actions "
                "are advisory to the Agent. Call submit_draft_review exactly once.")},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)},
        ]
        reply = self.models.chat_with_tools(
            messages, [REVIEW_SCHEMA], max_tokens=1200, temperature=0.0,
            tool_choice="required")
        calls = list(reply.tool_calls or ())
        if reply.hit_token_cap or not calls:
            return {"ok": False, "error": {
                "kind": "reviewer_protocol_failure",
                "message": "reviewer did not return a structured review"}}
        try:
            function = calls[0].get("function", {})
            arguments = function.get("arguments", "{}")
            review = json.loads(arguments) if isinstance(arguments, str) else arguments
            if function.get("name") != "submit_draft_review" or not isinstance(review, dict):
                raise ValueError("wrong reviewer tool or result")
            required = {"complete", "missing_subtasks", "unsupported_claims",
                        "contradictions", "suggested_actions"}
            if not required.issubset(review) or not isinstance(review["complete"], bool):
                raise ValueError("review is missing required fields")
            if any(not isinstance(review[key], list)
                   or not all(isinstance(item, str) for item in review[key])
                   for key in required - {"complete"}):
                raise ValueError("review lists must contain strings")
        except (json.JSONDecodeError, TypeError, ValueError, AttributeError) as e:
            return {"ok": False, "error": {
                "kind": "reviewer_protocol_failure", "message": str(e)[:300]}}
        return {"ok": True, "review": review}

    @staticmethod
    def _evidence(context: AgentContext) -> list[dict]:
        readable = context.evidence.readable_items()
        per_item = max(1000, 8000 // max(1, len(readable)))
        return [{
            "doc_id": item.doc_id,
            "page_num": item.page_num,
            "doc_name": item.doc_name,
            "content": "\n".join(
                segment["content"] for segment in item.segments)[:per_item],
        } for item in readable]
