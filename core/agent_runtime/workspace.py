"""Agent-owned, request-local planning and drafting tools."""
from .contracts import AgentContext
from .tools import ToolDefinition, ToolRegistry

WORKSPACE_TOOL = "update_workspace"
REVIEW_TOOL = "review_draft"


def update_workspace(context: AgentContext, args: dict) -> dict:
    updated = []
    if "plan" in args:
        context.workspace.plan = args["plan"].strip()
        updated.append("plan")
    if "note" in args:
        note = args["note"].strip()
        if note:
            if len(context.workspace.notes) >= 20:
                raise ValueError("workspace note limit reached")
            context.workspace.notes.append(note)
            updated.append("note")
    if "draft" in args:
        context.workspace.draft = args["draft"].strip()
        updated.append("draft")
    if "done" in args:
        for entry in args["done"]:
            entry = str(entry).strip()
            if entry and entry not in context.workspace.done:
                if len(context.workspace.done) >= 20:
                    raise ValueError("workspace done limit reached")
                context.workspace.done.append(entry)
        updated.append("done")
    if not updated:
        raise ValueError(
            "at least one non-empty plan, note, draft, or done entry is required")
    return {
        "updated": updated,
        "plan_chars": len(context.workspace.plan),
        "note_count": len(context.workspace.notes),
        "draft_chars": len(context.workspace.draft),
        "done": list(context.workspace.done),
    }


def register_workspace_tools(registry: ToolRegistry, reviewer) -> None:
    if WORKSPACE_TOOL not in registry.names():
        registry.register(ToolDefinition(
            WORKSPACE_TOOL,
            "Optionally maintain the Agent's own plan, findings, or current draft. "
            "Use only when it helps task completeness; this is not a required stage.",
            {"type": "object", "properties": {
                "plan": {"type": "string", "maxLength": 4000},
                "note": {"type": "string", "maxLength": 2000},
                "draft": {"type": "string", "maxLength": 12000},
                "done": {"type": "array", "maxItems": 20,
                         "items": {"type": "string", "maxLength": 200}},
            }, "additionalProperties": False},
            update_workspace,
        ))
    if REVIEW_TOOL not in registry.names():
        registry.register(ToolDefinition(
            REVIEW_TOOL,
            "Review the current or supplied draft/outline against the original task "
            "and readable evidence. Returns gaps and suggestions; the Agent decides what "
            "to do next. Review does not approve facts or modify the draft.",
            {"type": "object", "properties": {
                "draft": {"type": "string", "maxLength": 12000},
            }, "additionalProperties": False},
            reviewer,
        ))
