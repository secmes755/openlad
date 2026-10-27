"""Agent-owned workspace, optional review, and model-capability behavior."""
import copy
import json

import pytest

from core.agent_runtime import (
    AgentBudget,
    AgentCapabilities,
    AgentContext,
    AgentRuntime,
    CapabilityNotFound,
    CapabilityProvider,
    DraftReviewer,
    ToolRegistry,
)
from core.agent_runtime.workspace import REVIEW_TOOL, WORKSPACE_TOOL
from core.models.client import ChatResult


def raw_call(name, args, call_id="call-1"):
    return {"id": call_id, "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}


def tool_result(name, args):
    return ChatResult("", "tool_calls", (raw_call(name, args),))


class ClosedLoopModel:
    def __init__(self):
        self.calls = 0

    def chat_with_tools(self, messages, _schemas, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return tool_result(WORKSPACE_TOOL, {
                "plan": "Cover Alpha and Beta", "draft": "Alpha only"})
        if self.calls == 2:
            return tool_result(REVIEW_TOOL, {})
        if self.calls == 3:
            observation = messages[-1]["content"]
            assert "missing Beta" in observation
            return tool_result("search_records", {"query": "Beta"})
        assert any(message.get("name") == "search_records" for message in messages)
        return ChatResult("Alpha and Beta are covered.", "stop")


class StubReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, context, _args):
        self.calls += 1
        context.workspace.review_calls += 1
        return {"ok": True, "review": {
            "complete": False, "missing_subtasks": ["missing Beta"],
            "unsupported_claims": [], "contradictions": [],
            "suggested_actions": ["Search for Beta"]}}


def search_definition():
    from core.agent_runtime import ToolDefinition
    return ToolDefinition(
        "search_records", "Search", {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"],
         "additionalProperties": False},
        lambda _ctx, _args: {"items": [{"text": "Beta evidence"}]})


def test_agent_uses_review_feedback_to_choose_its_own_next_action():
    reviewer = StubReviewer()
    model = ClosedLoopModel()
    result = AgentRuntime(
        ToolRegistry([search_definition()]), model, reviewer=reviewer).run(
            "Cover Alpha and Beta", tenant_id="tenant-a")

    assert result["status"] == "completed"
    assert result["answer"] == "Alpha and Beta are covered."
    assert [item["tool"] for item in result["retrieval_trace"]["observations"]] == [
        WORKSPACE_TOOL, REVIEW_TOOL, "search_records"]
    assert result["workspace"]["plan"] == "Cover Alpha and Beta"
    assert result["workspace"]["draft"] == "Alpha only"
    assert result["retrieval_trace"]["metrics"]["review_calls"] == 1
    assert reviewer.calls == 1


def test_review_and_workspace_are_not_mandatory_stages():
    class ImmediateModel:
        def chat_with_tools(self, *_args, **_kwargs):
            return ChatResult("No retrieval or review needed.", "stop")

    class NoReview:
        def review(self, *_args):
            raise AssertionError("review must not be called")

    result = AgentRuntime(ToolRegistry(), ImmediateModel(), reviewer=NoReview()).run(
        "Simple task", tenant_id="tenant-a")

    assert result["status"] == "completed"
    assert result["retrieval_trace"]["observations"] == []
    assert result["workspace"]["review_calls"] == 0


def test_workspace_does_not_leak_between_requests():
    class Model:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return tool_result(WORKSPACE_TOOL, {
                    "plan": "private", "note": "tenant-specific", "draft": "draft"})
            return ChatResult("Done.", "stop")

    runtime = AgentRuntime(ToolRegistry(), Model())
    first = runtime.run("One", tenant_id="tenant-a")
    second = runtime.run("Two", tenant_id="tenant-b")

    assert first["workspace"]["plan"] == "private"
    assert second["workspace"] == {
        "plan": "", "notes": [], "draft": "", "review_calls": 0}


def test_runtime_clones_registry_and_does_not_share_reviewer_instances():
    class Model:
        def __init__(self):
            self.calls = 0

        def chat_with_tools(self, *_args, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                return tool_result(REVIEW_TOOL, {"draft": "Draft"})
            return ChatResult("Done.", "stop")

    base = ToolRegistry()
    first_reviewer, second_reviewer = StubReviewer(), StubReviewer()
    first = AgentRuntime(base, Model(), reviewer=first_reviewer)
    second = AgentRuntime(base, Model(), reviewer=second_reviewer)
    first.run("First", tenant_id="tenant-a")
    second.run("Second", tenant_id="tenant-b")

    assert WORKSPACE_TOOL not in base.names()
    assert REVIEW_TOOL not in base.names()
    assert first_reviewer.calls == 1
    assert second_reviewer.calls == 1


def test_capability_profile_disables_workspace_and_review_at_schema_and_execution():
    class Model:
        def __init__(self):
            self.schemas = []
            self.calls = 0

        def chat_with_tools(self, _messages, schemas, **_kwargs):
            self.schemas.append(copy.deepcopy(schemas))
            self.calls += 1
            if self.calls == 1:
                return tool_result(WORKSPACE_TOOL, {"plan": "must not execute"})
            return ChatResult("Finished without workspace.", "stop")

    minimal = AgentCapabilities(
        capability_id="minimal", workspace_enabled=False, review_enabled=False)
    provider = CapabilityProvider({"minimal": minimal})
    model = Model()
    result = AgentRuntime(
        ToolRegistry(), model, capability_provider=provider).run(
            "Task", tenant_id="tenant-a", capability_id="minimal")

    names = {schema["function"]["name"] for schema in model.schemas[0]}
    assert WORKSPACE_TOOL not in names
    assert REVIEW_TOOL not in names
    assert result["retrieval_trace"]["observations"][0]["result"]["ok"] is False
    assert result["workspace"]["plan"] == ""
    with pytest.raises(CapabilityNotFound):
        provider.resolve("missing")


def test_draft_reviewer_uses_native_structured_output_and_bounded_budget():
    class ReviewModel:
        def __init__(self):
            self.calls = 0
            self.messages = []

        def chat_with_tools(self, messages, schemas, **kwargs):
            self.calls += 1
            self.messages.append(copy.deepcopy(messages))
            assert kwargs["tool_choice"] == "required"
            assert schemas[0]["function"]["name"] == "submit_draft_review"
            review = {"complete": False, "missing_subtasks": ["Beta"],
                      "unsupported_claims": [], "contradictions": [],
                      "suggested_actions": ["Read another page"]}
            return tool_result("submit_draft_review", review)

    context = AgentContext(
        tenant_id="tenant-a", question="Alpha and Beta",
        budget=AgentBudget(max_review_calls=1))
    context.evidence.mark_read({
        "doc_id": "a" * 64, "page_num": 9, "content": ("a" * 1500) + "TARGET"})
    model = ReviewModel()
    reviewer = DraftReviewer(model)
    result = reviewer.review(context, {"draft": "Alpha only"})

    assert result["ok"] is True
    assert result["review"]["missing_subtasks"] == ["Beta"]
    payload = json.loads(model.messages[0][1]["content"])
    assert "TARGET" in payload["readable_evidence"][0]["content"]
    assert payload["readable_evidence"][0]["truncated"] is False
    assert context.model_calls == 1
    assert context.workspace.review_calls == 1
    second = reviewer.review(context, {"draft": "Alpha only"})
    assert second["error"]["kind"] == "review_budget_exhausted"
    assert model.calls == 1


def test_autonomous_finalization_keeps_native_reasoning_history():
    class Model:
        def __init__(self):
            self.calls = 0
            self.final_messages = []

        def chat_with_tools(self, messages, _schemas, **kwargs):
            self.calls += 1
            if kwargs["tool_choice"] == "none":
                self.final_messages = copy.deepcopy(messages)
                return ChatResult("Finished natively.", "stop")
            return tool_result("search_records", {"query": "task"})

    model = Model()
    result = AgentRuntime(ToolRegistry([search_definition()]), model).run(
        "Task", tenant_id="tenant-a", budget=AgentBudget(max_steps=2))

    assert result["status"] == "completed"
    assert any(message["role"] == "tool" for message in model.final_messages)
    assert result["retrieval_trace"]["capability"] == {
        "id": "autonomous", "finalization_mode": "native"}
    assert result["retrieval_trace"]["decisions"][-1][
        "finalization_channel"] == "native"


def test_guided_structured_response_submits_coverage_and_verifiable_references():
    from core.agent_runtime import ToolDefinition

    doc_id = "a" * 64

    def read_record(context, _args):
        context.evidence.mark_read({
            "doc_id": doc_id, "page_num": 1, "content": "Alpha evidence"})
        return {"ok": True}

    class StructuredModel:
        def chat_with_tools(self, _messages, schemas, **kwargs):
            if kwargs["tool_choice"] == "auto":
                return tool_result("read_record", {})
            assert kwargs["tool_choice"] == "required"
            assert schemas[0]["function"]["name"] == "submit_task_response"
            return tool_result("submit_task_response", {
                "task_coverage": [{"subtask": "Alpha", "status": "supported",
                                   "evidence_refs": [{"doc_id": doc_id, "page_num": 1}]}],
                "answer": "Alpha is supported by the readable evidence."})

    registry = ToolRegistry([ToolDefinition(
        "read_record", "Read a record", {"type": "object", "properties": {}},
        read_record)])
    result = AgentRuntime(registry, StructuredModel()).run(
        "Check Alpha", tenant_id="tenant-a", capability_id="guided",
        budget=AgentBudget(max_steps=2, max_model_calls=2))

    assert result["status"] == "completed"
    assert result["task_coverage"][0]["subtask"] == "Alpha"
    assert result["sources"] == [{
        "doc_id": doc_id, "page_num": 1, "doc_name": "", "section_title": ""}]
    assert f"[d:{doc_id}, p:1]" in result["answer"]
    assert result["retrieval_trace"]["decisions"][-1][
        "finalization_channel"] == "structured_evidence"


def test_guided_structured_response_rejects_prose_and_missing_support_refs():
    class ProseModel:
        def chat_with_tools(self, *_args, **_kwargs):
            return ChatResult("Unstructured response.", "stop")

    result = AgentRuntime(ToolRegistry(), ProseModel()).run(
        "Task", tenant_id="tenant-a", capability_id="guided",
        budget=AgentBudget(max_steps=1))
    assert result["status"] == "degraded"
    assert "exactly one structured response" in result["error"]

    class UnsupportedModel:
        def chat_with_tools(self, *_args, **_kwargs):
            return tool_result("submit_task_response", {
                "task_coverage": [{"subtask": "Alpha", "status": "supported",
                                   "evidence_refs": []}], "answer": "Alpha."})

    result = AgentRuntime(ToolRegistry(), UnsupportedModel()).run(
        "Task", tenant_id="tenant-a", capability_id="guided",
        budget=AgentBudget(max_steps=1))
    assert result["status"] == "degraded"
    assert "require readable evidence references" in result["error"]


def test_draft_reviewer_rejects_incomplete_structure():
    class BadModel:
        def chat_with_tools(self, *_args, **_kwargs):
            return tool_result("submit_draft_review", {"complete": True})

    context = AgentContext(tenant_id="tenant-a", question="Task")
    result = DraftReviewer(BadModel()).review(context, {"draft": "Draft"})

    assert result["ok"] is False
    assert "required fields" in result["error"]["message"]


def test_draft_reviewer_does_not_turn_unstructured_text_into_success():
    class BadModel:
        def chat_with_tools(self, *_args, **_kwargs):
            return ChatResult("Looks complete.", "stop")

    context = AgentContext(tenant_id="tenant-a", question="Task")
    result = DraftReviewer(BadModel()).review(context, {"draft": "Draft"})

    assert result["ok"] is False
    assert result["error"]["kind"] == "reviewer_protocol_failure"
