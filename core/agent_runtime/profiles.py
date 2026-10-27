"""Versioned industry profiles adapted from the existing external pack registry."""
from __future__ import annotations

from dataclasses import dataclass

from ..plugins import PluginRegistry


class ProfileNotFound(ValueError):
    """An explicitly requested profile cannot be resolved."""


@dataclass(frozen=True)
class AgentProfile:
    profile_id: str
    version: str
    instructions: str


class PluginProfileProvider:
    """Resolve immutable request snapshots; never auto-detect a domain in Core."""

    def __init__(self, scan_dirs: list[str] | None = None, registry=None):
        self.registry = registry or PluginRegistry(scan_dirs=scan_dirs or [])

    def resolve(self, profile_id: str | None = None) -> AgentProfile:
        selected = (self.registry.get_plugin(profile_id)
                    if profile_id else self.registry.get_generic())
        if selected is None:
            if profile_id:
                raise ProfileNotFound(f"unknown Agent profile: {profile_id}")
            return AgentProfile("", "", "")
        composed = self.registry.compose_with_base(selected)
        if composed is None:
            raise ProfileNotFound(f"Agent profile is unavailable: {profile_id}")
        prompts = composed.retrieval.get_retrieval_prompts()
        instructions = self._render(prompts)
        base = self.registry.get_generic()
        version = str(composed.manifest.version)
        if base is not None and base.manifest.id != composed.manifest.id:
            version = f"{base.manifest.version}+{version}"
        return AgentProfile(composed.manifest.id, version, instructions)

    @staticmethod
    def _render(prompts: dict) -> str:
        sections = []
        system = str(prompts.get("system_prompt") or "").strip()
        if system:
            sections.append(system)
        rules = prompts.get("answer_rules") or []
        if isinstance(rules, list) and rules:
            sections.append("Response guidance:\n" + "\n".join(
                f"- {rule}" for rule in rules if str(rule).strip()))
        filtering = str(prompts.get("context_filtering") or "").strip()
        if filtering:
            sections.append("Evidence selection guidance:\n" + filtering)
        return "\n\n".join(sections)
