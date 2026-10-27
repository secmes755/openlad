"""Request-local contracts for the model-driven document Agent."""
from __future__ import annotations

from dataclasses import dataclass, field

from .evidence import EvidenceStore


class AgentError(RuntimeError):
    """Base class for explicit Agent execution failures."""


class BudgetExhausted(AgentError):
    """The request cannot perform more model, step, or tool work."""


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int = 6
    max_model_calls: int = 6
    max_tool_calls: int = 12
    max_db_calls: int = 100
    max_embedding_calls: int = 12
    max_policy_feedback: int = 3
    max_review_calls: int = 1
    allow_unreviewed_absolute_claims: bool = False
    max_decision_tokens: int = 2048
    max_response_tokens: int = 4096
    max_observation_chars: int = 6000
    max_evidence_pages: int = 8
    max_history_items: int = 10
    max_history_chars: int = 1500
    max_evidence_chars: int = 48000


@dataclass(frozen=True)
class AgentCapabilities:
    capability_id: str = "autonomous"
    finalization_mode: str = "native"  # native, evidence_summary, structured_evidence
    workspace_enabled: bool = True
    review_enabled: bool = True
    allow_absolute_claims: bool = True
    profile_prompt_mode: str = "system_only"  # system_only or full legacy prompts


@dataclass
class AgentWorkspace:
    plan: str = ""
    notes: list[str] = field(default_factory=list)
    draft: str = ""
    done: list[str] = field(default_factory=list)
    review_calls: int = 0

    def snapshot(self) -> dict:
        return {
            "plan": self.plan,
            "notes": list(self.notes),
            "draft": self.draft,
            "review_calls": self.review_calls,
        }


@dataclass(frozen=True)
class ToolCall:
    call_id: str
    name: str
    args: dict


@dataclass
class AgentContext:
    tenant_id: str
    question: str
    chat_history: tuple[dict, ...] = ()
    budget: AgentBudget = field(default_factory=AgentBudget)
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    profile: str = ""
    profile_id: str = ""
    profile_version: str = ""
    steps: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    db_calls: int = 0
    embedding_calls: int = 0
    policy_feedback: int = 0
    finalization_retry: int = 0
    stalled_rounds: int = 0
    seen_pages: set = field(default_factory=set)
    readable_before: set = field(default_factory=set)
    evidence: EvidenceStore = field(default_factory=EvidenceStore)
    workspace: AgentWorkspace = field(default_factory=AgentWorkspace)
    task_coverage: list[dict] = field(default_factory=list)
    cache: dict = field(default_factory=dict)
    decisions: list[dict] = field(default_factory=list)
    observations: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.tenant_id = (self.tenant_id or "").strip()
        self.question = (self.question or "").strip()
        if not self.tenant_id:
            raise ValueError("tenant_id is required")
        if not self.question:
            raise ValueError("question is required")
        self.chat_history = tuple(self.chat_history or ())

    def consume_step(self):
        if self.steps >= self.budget.max_steps:
            raise BudgetExhausted("agent step budget exhausted")
        self.steps += 1

    def consume_model(self):
        if self.model_calls >= self.budget.max_model_calls:
            raise BudgetExhausted("agent model budget exhausted")
        self.model_calls += 1

    def consume_tool(self):
        if self.tool_calls >= self.budget.max_tool_calls:
            raise BudgetExhausted("agent tool budget exhausted")
        self.tool_calls += 1

    def consume_db(self):
        if self.db_calls >= self.budget.max_db_calls:
            raise BudgetExhausted("agent database budget exhausted")
        self.db_calls += 1

    def consume_embedding(self):
        if self.embedding_calls >= self.budget.max_embedding_calls:
            raise BudgetExhausted("agent embedding budget exhausted")
        self.embedding_calls += 1

    def metrics(self) -> dict:
        return {
            "steps": self.steps,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "db_calls": self.db_calls,
            "embedding_calls": self.embedding_calls,
            "policy_feedback": self.policy_feedback,
            "finalization_retry": self.finalization_retry,
            "review_calls": self.workspace.review_calls,
        }
