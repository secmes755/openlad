"""Behavioral acceptance for the model-driven Agent kernel."""
import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.agent_runtime import (
    AgentBudget,
    AgentContext,
    AgentRuntime,
    ToolDefinition,
    ToolRegistry,
)
from core.models.client import ChatResult


def call(name, args=None, call_id=None, raw_arguments=None):
    return ChatResult("", "tool_calls", ({
        "id": call_id or f"{name}-1",
        "type": "function",
        "function": {
            "name": name,
            "arguments": raw_arguments if raw_arguments is not None else json.dumps(
                args or {}),
        },
    },))


class FakeModel:
    def __init__(self, replies):
        self.replies = list(replies) if isinstance(replies, list) else replies
        self.calls = []

    def chat_with_tools(self, messages, schemas, **kwargs):
        self.calls.append((copy.deepcopy(messages), copy.deepcopy(schemas), kwargs))
        if callable(self.replies):
            return self.replies(messages)
        return self.replies.pop(0)


def definition(name, handler, *, maximum=12):
    return ToolDefinition(
        name=name,
        description=f"Execute {name} for the current task.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "maxLength": 200},
                "limit": {"type": "integer", "minimum": 1, "maximum": maximum},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        handler=handler,
    )


def run(model, registry, **kwargs):
    return AgentRuntime(registry, model).run(
        kwargs.pop("question", "Find the requested fact"),
        tenant_id=kwargs.pop("tenant_id", "tenant-a"), **kwargs)


def test_agent_can_finish_without_any_unconditional_tool_stage():
    registry = ToolRegistry([
        definition("search_records", lambda *_: pytest.fail("tool must not run"))])
    model = FakeModel([ChatResult("No retrieval is needed.", "stop")])
    result = run(model, registry)

    assert result["status"] == "completed"
    assert result["answer"] == "No retrieval is needed."
    assert result["retrieval_trace"]["observations"] == []
    assert result["retrieval_trace"]["metrics"] == {
        "steps": 1, "model_calls": 1, "tool_calls": 0,
        "db_calls": 0, "embedding_calls": 0, "policy_feedback": 0,
        "finalization_retry": 0,
        "review_calls": 0}


def test_non_informative_punctuation_is_not_a_completed_answer():
    model = FakeModel([ChatResult("...", "stop")] * 6)
    result = run(model, ToolRegistry())

    assert result["status"] == "degraded"
    assert "non-informative" in result["error"]
    assert result["answer"] == "..."  # preserved as failed raw output for diagnostics


def test_tool_observations_change_the_agents_next_action():
    def decide(messages):
        tool_messages = [item for item in messages if item["role"] == "tool"]
        if not tool_messages:
            return call("search_records", {"query": "target"})
        observation = json.loads(tool_messages[-1]["content"])
        if observation["result"]["items"]:
            return ChatResult("Evidence was found.", "stop")
        return call("alternate_search", {"query": "rewritten"})

    primary_items = []
    registry = ToolRegistry([
        definition("search_records", lambda _ctx, _args: {"items": primary_items}),
        definition("alternate_search", lambda _ctx, _args: {"items": ["fact"]}),
    ])

    empty_result = run(FakeModel(decide), registry)
    primary_items.append("fact")
    found_result = run(FakeModel(decide), registry)

    def names(result):
        return [item["tool"] for item in result["retrieval_trace"]["observations"]]

    assert names(empty_result) == ["search_records", "alternate_search"]
    assert names(found_result) == ["search_records"]
    assert empty_result["status"] == found_result["status"] == "completed"


def test_one_decision_can_request_independent_tool_calls():
    model = FakeModel([
        ChatResult("", "tool_calls", (
            call("search_records", {"query": "a"}, "a").tool_calls[0],
            call("alternate_search", {"query": "b"}, "b").tool_calls[0],
        )),
        ChatResult("Both observations reviewed.", "stop"),
    ])
    registry = ToolRegistry([
        definition("search_records", lambda _ctx, args: {"items": [args["query"]]}),
        definition("alternate_search", lambda _ctx, args: {"items": [args["query"]]}),
    ])
    result = run(model, registry)

    assert [item["call_id"] for item in result["retrieval_trace"]["observations"]] == [
        "a", "b"]
    assert result["retrieval_trace"]["metrics"]["steps"] == 2
    assert result["retrieval_trace"]["metrics"]["tool_calls"] == 2


def test_unknown_tool_and_malformed_action_are_observations_not_fake_success():
    registry = ToolRegistry()
    unknown = run(FakeModel([
        call("missing_tool"), ChatResult("The capability is unavailable.", "stop"),
    ]), registry)
    malformed = run(FakeModel([
        call("bad_json", raw_arguments="{broken"),
        ChatResult("The action was invalid.", "stop"),
    ]), registry)

    assert unknown["status"] == "completed"
    assert unknown["retrieval_trace"]["observations"][0]["result"]["error"]["kind"] == (
        "unknown_tool")
    assert malformed["retrieval_trace"]["observations"][0]["result"]["error"]["kind"] == (
        "invalid_action")


def test_schema_cap_rejects_model_arguments_before_handler_execution():
    executions = []
    registry = ToolRegistry([
        definition("search_records", lambda _ctx, args: executions.append(args))])
    model = FakeModel([
        call("search_records", {"query": "fact", "limit": 999}),
        ChatResult("The requested limit was rejected.", "stop"),
    ])
    result = run(model, registry)

    assert executions == []
    observation = result["retrieval_trace"]["observations"][0]
    assert observation["result"]["ok"] is False
    assert observation["result"]["error"]["kind"] == "invalid_arguments"


def test_budget_exhaustion_is_not_a_completed_answer():
    registry = ToolRegistry([
        definition("search_records", lambda _ctx, _args: {"items": []})])
    model = FakeModel(lambda _messages: call("search_records", {"query": "again"}))
    result = run(model, registry, budget=AgentBudget(
        max_steps=2, max_model_calls=2, max_tool_calls=4))

    assert result["status"] == "budget_exhausted"
    assert result["finished"] is False
    assert result["answer"] == ""
    assert result["retrieval_trace"]["metrics"]["model_calls"] == 2


def test_workspace_summary_distinguishes_unread_candidates_from_read_pages():
    context = AgentContext(tenant_id="tenant-a", question="Task")
    context.evidence.add_candidates([
        {"doc_id": "a" * 64, "page_num": 1, "section_title": "Read"},
        {"doc_id": "b" * 64, "page_num": 2, "section_title": "Unread"},
    ], "keyword")
    context.evidence.mark_read({
        "doc_id": "a" * 64, "page_num": 1, "content": "readable fact"})

    system = AgentRuntime(ToolRegistry(), FakeModel([]))._system_content(context)
    payload = json.loads(system.split("Current workspace and remaining budget:\n", 1)[1])

    assert payload["unread_candidate_count"] == 1
    assert payload["unread_candidates"] == [{
        "doc_id": "b" * 64, "page_num": 2, "section_title": "Unread"}]
    assert payload["unread_candidate_documents"] == [{
        "doc_id": "b" * 64, "unread_count": 1,
        "candidates": [{"page_num": 2, "section_title": "Unread"}]}]


def test_last_decision_is_reserved_for_finalization():
    class FinalizationModel:
        def __init__(self):
            self.tool_choices = []
            self.schemas = []
            self.systems = []
            self.messages = []
            self.max_tokens = []

        def chat_with_tools(self, messages, schemas, **kwargs):
            self.tool_choices.append(kwargs["tool_choice"])
            self.schemas.append(schemas)
            self.systems.append(messages[0]["content"])
            self.messages.append(copy.deepcopy(messages))
            self.max_tokens.append(kwargs["max_tokens"])
            if kwargs["tool_choice"] == "none":
                return ChatResult("Finished within budget.", "stop")
            return call("search_records", {"query": "task"})

    model = FinalizationModel()
    registry = ToolRegistry([
        definition("search_records", lambda _ctx, _args: {"items": []})])
    result = AgentRuntime(registry, model).run(
        "Task", tenant_id="tenant-a", capability_id="constrained",
        budget=AgentBudget(max_steps=2, max_model_calls=3))

    assert result["status"] == "completed"
    assert result["retrieval_trace"]["metrics"]["tool_calls"] == 1
    assert model.tool_choices[-1] == "none"
    assert model.schemas[-1] == []
    assert model.max_tokens == [4096, 4096]  # planning step gets the larger budget
    assert "Do not call tools or emit tool markup" in model.systems[-1]
    assert all(message["role"] != "tool" for message in model.messages[-1])
    assert result["retrieval_trace"]["decisions"][-1]["finalization_only"] is True


def test_finalization_messages_carry_readable_evidence_without_tool_history():
    context = AgentContext(tenant_id="tenant-a", question="Task")
    context.evidence.mark_read({
        "doc_id": "a" * 64, "page_num": 1, "content": ("a" * 1500) + "TARGET"})
    messages = AgentRuntime(ToolRegistry(), FakeModel([]))._finalization_messages(context)

    assert "TARGET" in messages[-1]["content"]
    assert '"truncated": false' in messages[-1]["content"]
    assert all(message["role"] != "tool" for message in messages)


def test_textual_tool_markup_is_not_a_final_response():
    class MarkupModel:
        def chat_with_tools(self, _messages, _schemas, **kwargs):
            if kwargs["tool_choice"] == "none":
                return ChatResult("<tool_call><function=search_records>", "stop")
            return call("search_records", {"query": "task"})

    registry = ToolRegistry([
        definition("search_records", lambda _ctx, _args: {"items": []})])
    result = AgentRuntime(registry, MarkupModel()).run(
        "Task", tenant_id="tenant-a",
        budget=AgentBudget(max_steps=2, max_model_calls=8))

    # Retrying markup consumes the model budget, so the run ends either
    # degraded (retry budget spent) or budget_exhausted (model budget spent).
    assert result["status"] in {"degraded", "budget_exhausted"}
    assert result["answer"] == ""
    assert result["retrieval_trace"]["metrics"].get("finalization_retry", 0) >= 1


@pytest.mark.parametrize("reply", [
    ChatResult("", "stop"),
    ChatResult("partial", "length"),
])
def test_empty_or_truncated_model_decisions_degrade(reply):
    result = run(FakeModel([reply]), ToolRegistry())
    assert result["status"] == "degraded"
    assert result["finished"] is False


def test_shared_runtime_interleaves_requests_without_sharing_tenant_state():
    barrier = threading.Barrier(2)

    def observe(context, args):
        barrier.wait(timeout=5)
        return {"ok": True, "tenant": context.tenant_id, "task": args["query"]}

    registry = ToolRegistry([definition("search_records", observe)])

    def decide(messages):
        if not any(item["role"] == "tool" for item in messages):
            question = next(item["content"] for item in reversed(messages)
                            if item["role"] == "user")
            return call("search_records", {"query": question})
        return ChatResult("Done.", "stop")

    runtime = AgentRuntime(registry, FakeModel(decide))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(runtime.run, f"task-{tenant}", tenant_id=tenant)
            for tenant in ("tenant-a", "tenant-b")
        ]
        results = [future.result(timeout=10) for future in futures]

    for result, tenant in zip(results, ("tenant-a", "tenant-b"), strict=True):
        trace = result["retrieval_trace"]
        assert trace["tenant_id"] == tenant
        assert trace["observations"][0]["result"]["tenant"] == tenant
        assert trace["observations"][0]["result"]["task"] == f"task-{tenant}"


def test_observation_compaction_preserves_structured_metadata():
    value = {"ok": True, "items": [{
        "doc_id": "a" * 64, "page_num": 7, "total_chars": 10000,
        "content": "x" * 10000,
    }]}
    compacted = AgentRuntime._compact(value, 2000)
    parsed = json.loads(compacted)

    assert parsed["observation_truncated"] is True
    assert parsed["items"][0]["page_num"] == 7
    assert parsed["items"][0]["total_chars"] == 10000
    assert len(parsed["items"][0]["content"]) < 1000


def test_observation_compaction_does_not_drop_candidate_tail():
    items = [{"doc_id": f"{index:064x}", "page_num": index + 1,
              "section_title": "Section", "doc_name": "document",
              "channel": "keyword", "excerpt": "x" * 1000}
             for index in range(12)]
    value = {"ok": True, "items": items,
             "evidence_refs": [{"doc_id": item["doc_id"], "page_num": item["page_num"]}
                               for item in items]}
    parsed = json.loads(AgentRuntime._compact(value, 6000))

    assert len(parsed["items"]) == 12
    assert parsed["items"][-1]["doc_id"] == f"{11:064x}"
    assert "evidence_refs" not in parsed


def test_profile_instructions_are_data_not_core_policy():
    model = FakeModel([ChatResult("Done.", "stop")])
    result = run(model, ToolRegistry(), profile_instructions="Use the active pack.")

    system = model.calls[0][0][0]["content"]
    assert "Use the active pack." in system
    assert result["retrieval_trace"]["profile"] == {"id": "manual", "version": ""}
