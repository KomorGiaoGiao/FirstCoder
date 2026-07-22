# Agent Loop Guardrails

[中文版本](AGENT_LOOP_GUARDRAILS.zh-CN.md)

## Purpose and Boundary

`AgentLoop` is the transaction coordinator for one user turn. It records facts,
projects a valid provider request, asks the model, executes returned tools, and
repeats. It does not know how an OpenAI chunk is parsed or how a shell command
works.

Guardrails bound that transaction so a confused model, a slow provider, or a
tool-heavy task cannot continue indefinitely. They are code-enforced checks in
`firstcoder/agent/loop.py`, not good intentions in the system prompt.

## The Turn State Machine

```text
user text + optional attachments
  -> stage attachments, append user fact -> build request -> provider call
  -> plain assistant text ----------------------------> complete
  -> assistant tool calls -> tool registry execution
       -> ALLOW/result -> append tool result -> provider call
       -> DENY         -> append denied result -> provider call
       -> ASK          -> store pending execution -> waiting for user input
  -> resume answer -> resolve pending tool -> continue
```

Every branch preserves a crucial provider rule: an assistant tool call obtains
a matching tool result, even when it was denied or the user refused it. This is
why a permission prompt is a paused turn, not an exception thrown out of the
conversation.

## Limits and Defaults

`AgentLoopLimits` is the single limit configuration.

| Field | Default | Stops when |
| --- | ---: | --- |
| `max_tool_rounds` | 200 | completed model-to-tool rounds exceed the budget |
| `max_provider_calls` | 400 | provider requests exceed the budget |
| `max_turn_seconds` | 3600 | monotonic elapsed turn time exceeds the budget |

`swe_lite()` uses 60 rounds, 100 calls, and 1800 seconds. `summary()` uses 1,
3, and 120 seconds. `None` disables a particular numeric limit; it does not
disable permission checks or tool-result sequence validation.

The explicit stop reasons are `tool_round_limit`, `provider_call_limit`, and
`turn_timeout`. Cancellation is separate: `CancellationToken` (defined in
`firstcoder.runtime.cancellation`) lets a
user or UI interrupt active work, rather than masquerading as one of these
budgets.

## What Happens Before a Normal Tool Round

The first user message initializes the active task program-side. For every
later message, `TaskBoundaryClassifier` makes a hidden provider request before
the visible agent request. It asks for exact JSON (`same`, `new`, or
`uncertain`) anchored to the current real message ID, retries an invalid or
failed classification up to three times, then records `uncertain` if none is
valid. Program code feeds the result through the session-injected
`task_boundary` tool and records the resulting state transition. The hidden
request is not forwarded to the TUI, but it consumes provider calls and turn
time from the same per-turn budgets; benchmark expectations must account for it. A confirmed boundary may
trigger context compaction.

The loop also constructs a stable system prefix and projects conversation
history through `ContextBuilder`. The resulting `ChatRequest` contains two
separate channels: `messages` for instructions/history and `tools` for native
tool definitions. Tool JSON schemas are not duplicated in the system message.

## Module Map Inside `agent/`

`AgentLoop` stays the coordinator; several helpers keep turn concerns separated:

| Module | Role |
| --- | --- |
| `loop.py` | turn transaction, compact triggers, stop/pause orchestration |
| `loop_limits.py` | budgets and stop-reason enums |
| `tool_execution.py` | execute/record tool calls |
| `tool_flow.py` / `tool_settlement.py` | batch flow and settlement |
| `task_plan_policy.py` | active TaskPlan lookup and one-time final reconciliation |
| `task_boundary_classifier.py` | task-boundary classification helpers |
| `ports.py` | minimal `ContextManagerLike` for the loop |

Compact call sites should prefer named helpers on the loop
(`_auto_compact`, `_compact_for_prompt_too_long`,
`_compact_after_task_hash_changed`) so the *intent* is obvious.

Shared DTOs used by tools/permissions/utils live in `firstcoder.runtime`, not
in the loop package. See [ARCHITECTURE.md](ARCHITECTURE.md).

## Tool Scheduling and Quality Nudges

Readonly calls such as `view`, `grep`, and `git_diff` may run in parallel when
the response permits it. In bypass mode, a wider explicit set can run in
parallel. Mutation ordering is not casually parallelized.

TaskPlan mutations address stable task IDs and append exactly one
session-scoped `task_plan_updated` event when they change state. `SessionView.task_plan`,
resume, fork, and the TUI consume that durable snapshot. `linear` derives sequence
from stable order; `dag` uses explicit dependencies. The loop does not inject
periodic synthetic user reminders. Before natural completion it may send one
ephemeral system instruction to reconcile unfinished TaskPlan work; that
instruction is not written to the session log.

## Recovery Paths

- A prompt-too-long `ProviderError` triggers context recovery and a bounded
  retry; it must not spin on the same oversized request.
- A malformed/unknown tool call becomes a structured `ToolResult` error.
- Permission `ASK` creates `PendingPermissionExecution`; interaction resumes
  the same original call.
- A prewrite review rechecks its file snapshots before it dispatches; if they
  changed, the operation is blocked and the model must propose a fresh diff.
- A cancelled task reports cancellation through the runner/UI boundary.

## Minimal Evidence

```sh
.venv/bin/python -m pytest \
  tests/test_agent_loop_limits.py tests/test_agent_context_loop.py \
  tests/test_agent_tool_flow.py tests/test_context_system_prompt.py \
  tests/test_multimodal_input.py tests/test_prewrite_review.py -q
```

Then locate the exact assertion you are changing:

```sh
rg -n "TOOL_ROUND_LIMIT|max_provider_calls|prompt too long|PendingPermission" tests firstcoder
```

## Common Misreadings

**“200 is the maximum number of tool calls.”** It is the configured tool-round
limit; a round can contain more than one eligible parallel read.

**“A successful test always ends immediately.”** No. Verification output is
evidence returned to the model; it does not remove tool access or force an
early final answer.

**“Bypass removes the wrapper.”** No. It changes policy decisions. The session
registry, event logging, normalized result handling, and loop limits remain.

**“The model invokes `task_boundary` before every visible response.”** No. The
loop initializes the first task itself; later turns use an invisible classifier
request, then program code records the decision through the internal control
tool. The main model neither sees nor may execute that tool.

## Safe Changes

Change a guardrail in `loop_limits.py`, enforce it in `loop.py`, and add a test
that asserts both the stop reason and resulting conversation shape. Do not add
an invisible timer in a provider adapter: limits are user-turn semantics and
belong at the coordinator.

Related: [Architecture](ARCHITECTURE.md), [Tools](TOOLS_DESIGN.md),
[Permissions](PERMISSIONS_DESIGN.md), and
[Context Management](CONTEXT_MANAGEMENT_DESIGN.md).
