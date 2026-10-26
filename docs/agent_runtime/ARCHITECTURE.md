# Agent Runtime Architecture

## Status

The package contains the first generic control-plane increment. It is not yet a
document-retrieval API and is not registered as a route. The legacy query engine
is unchanged.

## Control and responsibility split

`AgentRuntime` gives the configured model a task, bounded history, dynamic tool
schemas, and previous tool observations. Each model reply either requests native
tool calls or finishes the task. Tool results are appended and the model decides
the next action. Core contains no search order, query-type classifier, industry
vocabulary, entity convention, or mandatory answer/review stage.

`ToolRegistry` owns generic registration and execution policy. Handlers receive
the request's explicit `AgentContext`; tenant identity is never accepted from a
model argument. A small JSON-schema validator enforces required fields, rejected
extra fields, types, string/array limits, and numeric caps before a handler runs.
Unknown tools, malformed actions, and tool failures become explicit observations
rather than empty successes.

`AgentContext` is request-local and bounds model decisions, tool calls, history,
and observation size. Shared runtime and registry objects carry no per-request
tenant, messages, or results.

## Model protocol

The existing configured `ModelClient` supports native OpenAI-compatible tool
calls through `chat_with_tools`. `ChatResult` carries tool calls and finish reason
on the individual response; callers do not read shared last-call mirrors. No
model name, gateway, or credential is defined by the Agent package.

## Current acceptance

Behavioral tests prove that an immediate final response invokes no tool and that
empty versus non-empty tool observations can produce different next actions.
They also cover batched tool calls, malformed/unknown actions, schema caps,
budget exhaustion, empty/truncated decisions, profile instructions, and
interleaved tenants. A real configured-model protocol probe and a synthetic-tool
smoke are development evidence; they are not retrieval-quality results.

## Next increments

1. Add tenant-scoped document/search/structure/fact/read tools and an append-only
   evidence workspace.
2. Add citation/source validation and response evidence semantics.
3. Add versioned industry-profile providers without moving domain rules into Core.
4. Validate real document tasks, observation compaction, parallel execution, and
   same-model A/B quality and efficiency before exposing an experimental route.
