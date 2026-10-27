"""External, versioned profile resolution for the Agent runtime."""
import copy

import pytest
import yaml

from core.agent_runtime import (
    AgentRuntime,
    PluginProfileProvider,
    ProfileNotFound,
    ToolRegistry,
)
from core.models.client import ChatResult


def make_pack(root, profile_id, version, system, rule):
    pack = root / profile_id
    (pack / "retrieval").mkdir(parents=True)
    (pack / "manifest.yaml").write_text(yaml.safe_dump({
        "id": profile_id, "name": profile_id, "version": version,
        "description": "test pack", "category_mapping": [],
    }))
    (pack / "retrieval" / "prompts.yaml").write_text(yaml.safe_dump({
        "system_prompt": system, "answer_rules": [rule],
        "context_filtering": "",
    }))


class RecordingModel:
    def __init__(self):
        self.calls = []

    def chat_with_tools(self, messages, *_args, **_kwargs):
        self.calls.append(copy.deepcopy(messages))
        return ChatResult("Done.", "stop")


def test_external_profile_is_composed_and_versioned(tmp_path):
    make_pack(tmp_path, "generic", "1.0.0", "Base system", "base-rule")
    make_pack(tmp_path, "custom", "2.3.0", "Custom system", "custom-rule")
    provider = PluginProfileProvider([str(tmp_path)])

    profile = provider.resolve("custom")

    assert profile.profile_id == "custom"
    assert profile.version == "1.0.0+2.3.0"
    assert "Custom system" in profile.instructions
    assert "base-rule" in profile.instructions
    assert "custom-rule" in profile.instructions


def test_runtime_injects_profile_and_records_resolved_version(tmp_path):
    make_pack(tmp_path, "generic", "1.0.0", "Base system", "base-rule")
    make_pack(tmp_path, "custom", "2.3.0", "Custom system", "custom-rule")
    model = RecordingModel()
    runtime = AgentRuntime(
        ToolRegistry(), model, profile_provider=PluginProfileProvider([str(tmp_path)]))

    result = runtime.run("Task", tenant_id="tenant-a", profile_id="custom")

    assert result["status"] == "completed"
    system = model.calls[0][0]["content"]
    assert "custom@1.0.0+2.3.0" in system
    assert "Custom system" in system
    assert result["retrieval_trace"]["profile"] == {
        "id": "custom", "version": "1.0.0+2.3.0"}


def test_profile_selection_is_request_local(tmp_path):
    for profile_id in ("custom", "other"):
        make_pack(tmp_path, profile_id, "1.0.0", f"{profile_id}-system", f"{profile_id}-rule")
    provider = PluginProfileProvider([str(tmp_path)])
    model = RecordingModel()
    runtime = AgentRuntime(ToolRegistry(), model, profile_provider=provider)

    runtime.run("First", tenant_id="tenant-a", profile_id="custom")
    runtime.run("Second", tenant_id="tenant-a", profile_id="other")

    assert "custom-system" in model.calls[0][0]["content"]
    assert "other-system" not in model.calls[0][0]["content"]
    assert "other-system" in model.calls[1][0]["content"]
    assert "custom-system" not in model.calls[1][0]["content"]


def test_unknown_profile_and_ambiguous_configuration_are_explicit(tmp_path):
    provider = PluginProfileProvider([str(tmp_path)])
    with pytest.raises(ProfileNotFound):
        provider.resolve("missing")

    runtime = AgentRuntime(ToolRegistry(), RecordingModel(), profile_provider=provider)
    with pytest.raises(ValueError, match="mutually exclusive"):
        runtime.run("Task", tenant_id="tenant-a", profile_id="custom",
                    profile_instructions="manual")
    with pytest.raises(ValueError, match="profile provider"):
        AgentRuntime(ToolRegistry(), RecordingModel()).run(
            "Task", tenant_id="tenant-a", profile_id="custom")
