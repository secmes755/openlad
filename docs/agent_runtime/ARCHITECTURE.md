# Agent Runtime Architecture

## Status

The package contains generic control-plane and tenant-scoped document-tool
increments. It is not yet registered as a public query route. The legacy query
engine is unchanged.

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

## Document tools and evidence

`create_document_registry` exposes tenant-scoped catalog, keyword, vector,
structure, structured-fact, and page-read capabilities. Their registration does
not invoke them: the model selects each capability and may choose different
channels or stop without searching. Document filters resolve unique active IDs,
and the keyword data plane supports an optional exact document-ID set without
changing existing callers.

The request-local `EvidenceStore` separates candidates from readable evidence.
Only `read_pages` creates readable segments. Repeated/offset reads append
segments rather than erasing earlier observations. Citation validation accepts
short or full document IDs, square/parenthesized references, and bounded page
ranges; every expanded page must be readable in the same request. Invalid
citations are rejected. Positive evidence-bearing responses without verifiable
citations degrade, while a leading scoped-insufficiency conclusion does not need
an irrelevant citation. One deterministic policy-feedback opportunity lets the
Agent correct its own response within existing budgets.

Tool observations retain all candidate identities while compressing duplicate
references and long text. Page reads expose offset, total length, and truncation.
A workspace summary informs each decision of readable evidence and remaining
budget without prescribing the next action.

## Profiles and bounded finalization

`PluginProfileProvider` adapts the existing external pack registry. Profiles are
selected explicitly, composed with the generic base, and recorded with their
resolved version in the trace. Core does not auto-detect an industry or load its
vocabulary into planning code.

When only one decision remains (or the tool budget is exhausted), the runtime
uses a finalization-only model call. It removes native tool history and supplies
bounded readable segments plus retrieval-scope observations. This prevents a
model from spending its last decision on another tool call or emitting tool
markup as the answer. Decision and response token limits are independent.
Ordinary model-selected final responses do not pass through this stage.

Citation validity is not claim-level support: an answer can cite real pages and
still overstate an absence or mix in unsupported claims. Complex multi-part
responses can also omit citations and correctly degrade. These remain explicit
quality gates for reviewer/claim evaluation; completion status and keyword-only
matrix checks must not be treated as production accuracy certification.

## Next increments

1. Add optional reviewer policies and claim-level answer-support evaluation.
2. Expand real document matrices and tune observation/model/DB efficiency.
3. Run same-model legacy A/B before exposing an experimental route.
4. Add an authenticated experimental API only after the A/B gates are defined.
