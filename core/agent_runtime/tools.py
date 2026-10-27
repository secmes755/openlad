"""Generic tool registration and deterministic execution policy."""
from __future__ import annotations

import copy
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .contracts import AgentContext, ToolCall


@dataclass(frozen=True)
class ToolDefinition:
    name: str
    description: str
    parameters: dict
    handler: Callable[[AgentContext, dict], Any]

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass(frozen=True)
class ToolOutcome:
    call_id: str
    name: str
    result: dict
    elapsed_s: float


class ToolRegistry:
    """A shared immutable-at-runtime catalog; all invocation state is request-local."""

    def __init__(self, definitions=()):
        self._definitions: dict[str, ToolDefinition] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: ToolDefinition):
        if not definition.name or definition.name in self._definitions:
            raise ValueError(f"invalid or duplicate tool: {definition.name!r}")
        self._definitions[definition.name] = definition

    def schemas(self) -> list[dict]:
        return copy.deepcopy([
            definition.schema() for definition in self._definitions.values()])

    def names(self) -> set[str]:
        return set(self._definitions)

    def clone(self) -> ToolRegistry:
        return ToolRegistry(self._definitions.values())

    def execute(self, context: AgentContext, call: ToolCall,
                allowed_tools: set[str] | None = None) -> ToolOutcome:
        context.consume_tool()
        started = time.monotonic()
        definition = self._definitions.get(call.name)
        if definition is None or (allowed_tools is not None and call.name not in allowed_tools):
            return ToolOutcome(call.call_id, call.name, _error(
                "unknown_tool", f"unknown or disabled tool: {call.name}"), 0.0)
        validation_error = _validate(definition.parameters, call.args)
        if validation_error:
            return ToolOutcome(call.call_id, call.name, _error(
                "invalid_arguments", validation_error), 0.0)
        try:
            value = definition.handler(context, dict(call.args))
            result = value if isinstance(value, dict) and "ok" in value else {
                "ok": True, "result": value,
            }
        except Exception as e:
            result = _error("tool_failure", str(e)[:500])
        return ToolOutcome(
            call.call_id, call.name, result,
            round(time.monotonic() - started, 6))


def _error(kind: str, message: str) -> dict:
    return {"ok": False, "error": {"kind": kind, "message": message}}


def _validate(schema: dict, value: Any, path: str = "args") -> str | None:
    """Validate the JSON-schema subset used by tool contracts, including hard caps."""
    expected = schema.get("type")
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "number": lambda item: isinstance(item, (int, float))
        and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    if expected in checks and not checks[expected](value):
        return f"{path} must be {expected}"
    if expected == "object":
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}.{key} is required"
        if schema.get("additionalProperties") is False:
            unknown = set(value) - set(properties)
            if unknown:
                return f"{path} contains unsupported fields: {sorted(unknown)}"
        for key, item in value.items():
            if key in properties:
                error = _validate(properties[key], item, f"{path}.{key}")
                if error:
                    return error
    elif expected == "array":
        if len(value) > schema.get("maxItems", len(value)):
            return f"{path} exceeds maxItems"
        for index, item in enumerate(value):
            error = _validate(schema.get("items", {}), item, f"{path}[{index}]")
            if error:
                return error
    elif expected == "string":
        if len(value) > schema.get("maxLength", len(value)):
            return f"{path} exceeds maxLength"
    elif expected in {"integer", "number"}:
        if value < schema.get("minimum", value):
            return f"{path} is below minimum"
        if value > schema.get("maximum", value):
            return f"{path} exceeds maximum"
    return None
