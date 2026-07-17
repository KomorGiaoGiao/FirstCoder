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
user input
  -> append user fact -> build request -> provider call
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
| `successful_verification_stop` | `True` | a qualifying verification result asks for finalization |

`swe_lite()` uses 60 rounds, 100 calls, and 1800 seconds. `summary()` uses 1,
3, and 120 seconds. `None` disables a particular numeric limit; it does not
disable permission checks or tool-result sequence validation.

The explicit stop reasons are `tool_round_limit`, `provider_call_limit`, and
`turn_timeout`. Cancellation is separate: `CancellationToken` (defined in
`firstcoder.runtime.cancellation`, re-exported from `agent.cancellation`) lets a
user or UI interrupt active work, rather than masquerading as one of these
budgets.

## What Happens Before a Normal Tool Round

At the start of a tool-capable turn, the loop can force the session-injected
`task_boundary` tool. The program supplies the stable task hash; the model may
only report a decision based on a real user-message id. Once a boundary is
confirmed, context compaction may run under a task-switch trigger.

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
| `todo_policy.py` | todo reminder policy for the loop |
| `task_boundary_classifier.py` | task-boundary classification helpers |
| `verification.py` | successful-verification early stop |
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

The loop also observes todo behavior. After enough non-todo tool results it can
ask for a plan; after several results since the last update it can ask for todo
progress. These are model-visible reminders, not a second planner and not a
permission mechanism.

## Recovery Paths

- A prompt-too-long `ProviderError` triggers context recovery and a bounded
  retry; it must not spin on the same oversized request.
- A malformed/unknown tool call becomes a structured `ToolResult` error.
- Permission `ASK` creates `PendingPermissionExecution`; interaction resumes
  the same original call.
- A cancelled task reports cancellation through the runner/UI boundary.

## Minimal Evidence

```sh
.venv/bin/python -m pytest \
  tests/test_agent_loop_limits.py tests/test_agent_context_loop.py \
  tests/test_agent_tool_flow.py tests/test_agent_verification.py -q
```

Then locate the exact assertion you are changing:

```sh
rg -n "TOOL_ROUND_LIMIT|max_provider_calls|prompt too long|PendingPermission" tests firstcoder
```

## Common Misreadings

**“200 is the maximum number of tool calls.”** It is the configured tool-round
limit; a round can contain more than one eligible parallel read.

**“A successful test always ends immediately.”** Only results recognized by
`agent/verification.py`, with `successful_verification_stop` enabled, prompt
that early-finalization behavior.

**“Bypass removes the wrapper.”** No. It changes policy decisions. The session
registry, event logging, normalized result handling, and loop limits remain.

## Safe Changes

Change a guardrail in `loop_limits.py`, enforce it in `loop.py`, and add a test
that asserts both the stop reason and resulting conversation shape. Do not add
an invisible timer in a provider adapter: limits are user-turn semantics and
belong at the coordinator.

Related: [Architecture](ARCHITECTURE.md), [Tools](TOOLS_DESIGN.md),
[Permissions](PERMISSIONS_DESIGN.md), and
[Context Management](CONTEXT_MANAGEMENT_DESIGN.md).
