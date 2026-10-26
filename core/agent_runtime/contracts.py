"""Request-local contracts for the model-driven document Agent."""
from __future__ import annotations

from dataclasses import dataclass, field


class AgentError(RuntimeError):
    """Base class for explicit Agent execution failures."""


class BudgetExhausted(AgentError):
    """The request cannot perform more model, step, or tool work."""


@dataclass(frozen=True)
class AgentBudget:
    max_steps: int = 6
    max_model_calls: int = 6
    max_tool_calls: int = 12
    max_observation_chars: int = 6000
    max_history_items: int = 10
    max_history_chars: int = 1500


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
    profile: str = ""
    steps: int = 0
    model_calls: int = 0
    tool_calls: int = 0
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

    def metrics(self) -> dict:
        return {
            "steps": self.steps,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
        }
