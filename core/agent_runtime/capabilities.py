"""Model-capability policy, independent of industry and retrieval strategy."""
from .contracts import AgentCapabilities


class CapabilityNotFound(ValueError):
    pass


class CapabilityProvider:
    def __init__(self, profiles: dict[str, AgentCapabilities] | None = None):
        self.profiles = dict(profiles or {
            "autonomous": AgentCapabilities(),
            "constrained": AgentCapabilities(
                capability_id="constrained",
                finalization_mode="evidence_summary",
                workspace_enabled=True,
                review_enabled=True,
                allow_absolute_claims=False,
                profile_prompt_mode="full",
            ),
            "guided": AgentCapabilities(
                capability_id="guided",
                finalization_mode="structured_evidence",
                workspace_enabled=True,
                review_enabled=True,
                allow_absolute_claims=False,
                profile_prompt_mode="system_only",
            ),
        })

    def resolve(self, capability_id: str | None = None) -> AgentCapabilities:
        resolved = capability_id or "autonomous"
        try:
            profile = self.profiles[resolved]
        except KeyError as e:
            raise CapabilityNotFound(f"unknown model capability profile: {resolved}") from e
        if profile.finalization_mode not in {"native", "evidence_summary", "structured_evidence"}:
            raise ValueError(f"invalid finalization mode: {profile.finalization_mode}")
        if profile.profile_prompt_mode not in {"system_only", "full"}:
            raise ValueError(f"invalid profile prompt mode: {profile.profile_prompt_mode}")
        return profile
