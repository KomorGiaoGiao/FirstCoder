# Agent Loop Guardrails

[中文版本](AGENT_LOOP_GUARDRAILS.zh-CN.md)

## Overview

The agent loop guardrails limit how far one user turn can run before the runtime stops it. In the current implementation, these limits are part of `AgentLoopLimits` and are enforced directly inside the loop.

The guardrails today focus on three boundaries:

- maximum tool rounds
- maximum provider calls
- maximum wall-clock time for one turn

They are narrower and more concrete than the older conceptual “safety budget” model.

## Key Files

- `firstcoder/agent/loop_limits.py`: limit fields and stop reasons
- `firstcoder/agent/loop.py`: enforcement inside the loop
- `firstcoder/agent/cancellation.py`: cancellation token support
- `firstcoder/agent/verification.py`: verification success checks that can end a turn early

## Limit Model

`AgentLoopLimits` currently contains:

- `max_tool_rounds`
- `max_provider_calls`
- `max_turn_seconds`
- `successful_verification_stop`

There is no `Age` tracker and no separate max-verification-count or total-tool-time budget in the current code.

`AgentLoopStopReason` currently includes:

- `tool_round_limit`
- `provider_call_limit`
- `turn_timeout`

## Defaults

The normal defaults are intentionally generous:

- `max_tool_rounds = 200`
- `max_provider_calls = 400`
- `max_turn_seconds = 3600`
- `successful_verification_stop = True`

Additional presets exist for narrower workflows:

- `default()`
- `swe_lite()`
- `summary()`

These presets are part of the actual implementation and should be treated as runtime presets, not just documentation suggestions.

## Enforcement In The Loop

`AgentLoop` owns the real control flow.

The loop currently does all of the following in one turn:

1. append the user message
2. compact context if needed
3. build provider-visible messages
4. call the provider
5. append assistant output or assistant tool calls
6. execute tools
7. append tool results
8. optionally compact again
9. repeat until the turn completes, pauses for user input, or hits a guardrail

Guardrails are checked during this loop rather than as a separate supervisory process.

## Related Runtime Behavior

Several runtime behaviors interact with these limits:

- prompt-too-long provider errors can trigger compaction and a retry path
- readonly tools can execute in parallel in some cases
- multi-step tool work without an existing todo plan can trigger a one-time planning reminder after multiple non-todo tool results
- stale todo state can trigger a progress reminder when several tools run after the last todo update
- successful verification can stop further tool looping and force a final model response
- permission confirmation can pause the turn and later resume it without losing tool-call integrity

These are not separate guardrail subsystems, but they affect how long a turn can continue and when it should stop.

## Cancellation

Cancellation exists alongside the limit system. It is not one of the stop-reason counters in `AgentLoopStopReason`, but it is still part of the runtime boundary model.

The loop uses cancellation support to interrupt:

- long-running tool execution
- streaming turns
- resumed turns waiting on further execution

This gives the runtime both automatic stop conditions and user-driven interruption.

## Design Notes

- The current guardrail system is loop-centric, not event-supervisor-centric.
- The real implementation is simpler than earlier conceptual docs: provider-call count matters more than abstract verification budgets.
- Verification still matters, but today it is modeled as an early-stop condition, not a max-count quota.
- Prompt-too-long recovery belongs in the same practical safety envelope because it prevents a turn from repeatedly failing on the same oversized prompt.
- Todo reminders are guardrails for planning quality, not a separate scheduler: they nudge the model to create or update a complete visible plan while keeping execution in the normal loop.
