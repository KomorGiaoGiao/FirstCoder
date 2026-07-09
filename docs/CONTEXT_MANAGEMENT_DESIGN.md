# Context Management Design

[中文版本](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)

## Overview

FirstCoder's context management is built around an append-only JSONL session log plus runtime replay. Context compaction does not mutate history in place. Instead, the runtime records compaction facts, checkpoints, and task-boundary observations, then rebuilds the effective session view from those events.

The current system has two distinct layers:

- durable session facts rebuilt into `SessionView`
- runtime-only state rebuilt into `SessionRuntimeState`

## Key Files

- `firstcoder/context/store.py`: JSONL persistence and session-view rebuild
- `firstcoder/context/writer.py`: structured event-writing helpers
- `firstcoder/context/context_builder.py`: provider-visible message projection
- `firstcoder/context/manager.py`: `ContextWindowManager`
- `firstcoder/context/compaction.py`: deterministic L1-L3 compaction pipeline
- `firstcoder/context/llm_compact.py`: provider-backed L4 checkpoint summarization
- `firstcoder/context/checkpoint.py`: checkpoint model
- `firstcoder/context/archive.py`: archived tool-result storage
- `firstcoder/context/triggers.py`: compaction trigger logic
- `firstcoder/context/runtime_state.py`: runtime-only state
- `firstcoder/context/runtime_replay.py`: replay of runtime state from session events

## Durable View And Runtime State

The durable replayed view is `SessionView`. It contains:

- messages
- checkpoints
- metadata

This is the conversation state the runtime can rebuild from the append-only log.

The runtime-only replayed state is `SessionRuntimeState`. It tracks things such as:

- active task hash
- task-boundary candidate stability
- latest checkpoint id
- auto-compaction failure counters
- circuit-breaker state
- recent compaction history

This split is important: not every runtime fact is modeled as a visible conversation message.

## Context Projection

The agent does not send the raw JSONL log to the provider. `ContextBuilder` projects the rebuilt session state into provider-visible messages.

Checkpoints change projection, not history deletion:

- the raw event history remains on disk
- provider-visible context may use checkpoint summaries plus the raw tail after a boundary

This means compaction is fundamentally a projection strategy backed by durable facts.

## Compaction Layers

The current implementation is not a generic “truncate, summarize, summarize harder” pipeline.

It is structured into:

- L1-L3: deterministic programmatic compaction
- L4: provider-backed checkpoint summarization

### L1-L3

These are implemented in the programmatic compaction pipeline and do not require a model call.

Current behavior includes:

- task-aware compaction of older task material
- archiving oversized tool results to disk and replacing them with placeholders
- route-based compression of cold or force-routed content, such as diff output, HTML, JSON, or search results

This is content-aware and structure-aware compaction, not just generic summary text generation.

### L4

L4 is handled by `LlmCompactService`.

It creates provider-backed checkpoint summaries when deterministic compaction is not enough.

Important properties:

- the summary is stored as a checkpoint event
- checkpoint boundaries are validated so tool-call / tool-result ordering remains legal
- after L4 writes its checkpoint, the runtime rebuilds the session view again from disk

## Trigger Model

Trigger evaluation lives in `firstcoder/context/triggers.py`.

The current system uses multiple heuristics, including:

- estimated total tokens
- oversized tool results
- too many tool-result tokens in one turn
- too many tail messages
- too many tail tokens

The trigger names used by `ContextWindowManager` include:

- `AUTO`
- `TASK_HASH_CHANGED`
- `PROMPT_TOO_LONG`
- `MANUAL`

The thresholds are currently implemented as concrete token-oriented numbers, not simple percentages.

## Task-Boundary Integration

Task-aware compaction is tied to a stable task-boundary flow.

The runtime does not trust the model to invent task identities. Instead:

1. the model/tool emits only a structured task-boundary signal
2. the runtime computes a candidate hash
3. stability is enforced over a window
4. once confirmed, a task-boundary event is recorded
5. the context manager can then compact old-task material under `TASK_HASH_CHANGED`

This makes task-aware compaction a program-owned mechanism rather than a free-form model behavior.

The runtime also initializes an `active_task_hash` when a session starts doing work before the model calls `task_boundary`. This gives early user-message parts a task tag and lets later task switches compact old-task material consistently.

## Fallback And Circuit Breaking

If L4 fails, the manager can apply fallback policy at the manager layer rather than hiding all retry behavior inside the L4 service.

Current fallback behavior includes:

- stronger deterministic compaction before another L4 attempt
- failure recording into runtime state
- auto-compaction circuit breaking after repeated failures

This matters because auto compaction should not repeatedly trigger expensive or broken L4 attempts on every turn.

## Event Model

Compaction-related state is persisted as session events rather than hidden mutable state.

Important event types include:

- `task_boundary_observed`
- `compaction_completed`
- `llm_compaction_completed`
- `checkpoint_created`

These events are replayed later into both `SessionView` and `SessionRuntimeState`.

## Design Notes

- Context management is event-backed and replay-driven, not in-place history mutation.
- Programmatic compaction is the normal path; L4 is the expensive escalation path.
- Checkpoints alter provider projection, not the existence of raw historical facts.
- Task-aware compaction is runtime-owned and stabilized before it affects history projection.
- `TASK_HASH_CHANGED` is a semantic trigger: it can force old-task compaction even when the window is still under the normal token threshold and it is not blocked by the auto-compaction circuit breaker.
