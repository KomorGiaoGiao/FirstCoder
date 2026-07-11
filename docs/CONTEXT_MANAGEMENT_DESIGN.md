# Context Management Design

[中文版本](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)

## The Problem

A long coding session needs two properties that pull in opposite directions:
keep an auditable record of every important fact, yet send a model a bounded,
protocol-valid working view. FirstCoder solves this by separating **durable
facts** from **the current provider projection**. Compaction changes the latter
without destructively rewriting the former.

This is not a generic chat summarizer. It must preserve the exact source reads,
tool-call transactions, and error evidence needed for the next code change.

## The Three Views of a Session

```text
append-only JSONL events
  -> replay -> SessionView (effective FirstCoder facts)
  -> ContextBuilder -> ChatMessage[] (this provider request)
  -> provider
```

- `JsonlSessionStore` writes and reads `<data-root>/sessions/<id>.jsonl`.
- Replay applies additive replacement/checkpoint events to construct
  `SessionView`; raw events remain readable.
- `ContextBuilder` alone projects `SessionView` to provider messages. It adds a
  checkpoint summary when present, emits the tail, and validates tool ordering.

`system_meta` is internal state and is deliberately not projected as ordinary
conversation. The stable system prefix is supplied separately by the session.

## A Concrete Long-Session Story

1. The agent reads a source file and runs a large test command.
2. Events append the assistant call and tool result; `SessionView` contains both.
3. `ContextWindowManager` observes token/output pressure.
4. Deterministic L1–L3 compaction trims/archives eligible old material and
   appends a `compaction_completed` replacement event.
5. If still too large, L4 asks a summarizer for a coding handoff and appends a
   `checkpoint_created` event.
6. On the next provider call, the builder sends the checkpoint summary plus a
   legal uncompressed tail, not the entire raw log.
7. Resume replays the same events; it does not depend on a hidden in-memory
   transcript.

## Non-Negotiable Invariants

1. Facts and archived originals are append-only.
2. Provider projection never begins with an orphan `role=tool` result.
3. Every emitted tool result keeps its original `tool_call_id` pairing.
4. Latest user intent and fresh recognized source reads are protected from
   lossy L1–L3 transformations.
5. Lossy L2 output is archived before a replacement is persisted.
6. L1–L3 are deterministic; L4 is the only semantic model-summary layer.
7. Replaying/resuming compaction is idempotent: no duplicate archive expansion
   or repeated replacement for the same source.

Violating (2) or (3) causes provider APIs to reject history. Violating (4) is
more subtle: a model may make a patch with stale or incomplete source context.

## Compaction Levels

```text
effective tail -> lifecycle classification
  -> L1 old-task text trim
  -> L2 typed reversible tool-result compression
  -> L3 archive placeholder eviction
  -> L4 coding-handoff checkpoint only if needed
```

### L1: trim confirmed old-task dialogue

L1 marks safe plain-text parts from old tasks as trimmed. It does not touch tool
transactions, latest user input, or assistant messages containing tool calls.
Projection omits trimmed text and emits at most one `[Earlier dialogue trimmed]`
marker for the tail.

### L2: compress derived output, keep a recovery copy

L2 acts only on lifecycle-eligible derived results: logs, searches, diffs, JSON,
HTML, lists, and similar output. Route-aware compressors retain useful shape
(failed test/error block, paths/line numbers, diff headers, JSON status) and
must produce a strictly smaller candidate. `ToolResultArchive` stores the full
original first.

### L3: replace old material with retrieval placeholders

L3 replaces selected tool results with bounded placeholders, never removes a
tool transaction. Stale source reads (after a known mutation), superseded reads,
duplicate derived output, and old/large derived results can become candidates.
Fresh recognized source reads and current-turn retrieval results are protected.

`retrieve_archive` is injected per session. It accepts an archive id, optional
literal query, and bounded `max_chars`; it cannot become arbitrary filesystem
access or cross-session retrieval.

### L4: model-generated handoff

If deterministic work still misses the target, `LlmCompactService` requests a
structured handoff covering goal, constraints, decisions, files, commands,
errors, and immediate next step. Local code validates its tail boundary before
writing the checkpoint. The summary is useful context, not a replacement for
the append-only evidence.

## Lifecycle Classification Is Conservative

The classifier follows structured tool arguments/result data, not display-text
guesses.

| State | Meaning | Treatment |
| --- | --- | --- |
| `fresh` | known source read remains current | retain exact content |
| `stale` | known successful mutation touched its path later | L3 candidate |
| `superseded` | later known read covers it | L3 candidate |
| `derived` | non-source output such as log/search/diff | L2, then L3 if needed |
| `duplicate` | older derived content equals a later result | reuse backing/L3 candidate |

Unknown tools, ambiguous metadata, partial reads, and shell output fail open:
they are not guessed to be a source mutation or source read. Lower compression
is safer than removing the only correct file evidence.

## Triggers and Failure Handling

`ContextWindowManager` owns timing and escalation.

| Trigger | Meaning |
| --- | --- |
| `AUTO` | token/tail/output heuristics reach threshold |
| `TASK_HASH_CHANGED` | confirmed task switch; force cleanup of old derived context |
| `MANUAL` | user asks to compact/inspect |
| `PROMPT_TOO_LONG` | provider rejected the request; do blocking recovery then bounded retry |

The manager runs deterministic work first, records its outcome, and only asks
L4 when required. An automatic-compaction circuit breaker prevents expensive
repeated automatic failures; manual, task-boundary, and overflow recovery are
not silently skipped by that breaker.

## Source Map

| Concern | Start here |
| --- | --- |
| JSONL replay/effective view | `context/store.py`, `context/models.py` |
| provider projection/tool validity | `context/context_builder.py`, `context/tool_sequence.py` |
| deterministic L1–L3 | `context/compaction.py`, `context/tool_lifecycle.py`, `context/content/` |
| archive/retrieval | `context/archive.py`, `tools/retrieve_archive.py` |
| trigger and L4 escalation | `context/manager.py`, `context/triggers.py` |
| checkpoint generation/retry | `context/llm_compact.py`, `context/provider_summarizer.py` |
| replayable runtime facts | `context/runtime_state.py`, `context/runtime_replay.py` |

## Minimal Verification

```sh
.venv/bin/python -m pytest tests/test_context_builder_new.py \
  tests/test_context_compaction_pipeline.py tests/test_context_window_manager.py \
  tests/test_context_llm_compact.py tests/test_context_resume.py \
  tests/test_context_archive.py -q
```

When editing a level, test both its savings and what it must *not* change:
fresh source protection, archive recovery bounds, tool-call sequence validity,
and replay/resume idempotence.

## Common Mistakes

- **Deleting JSONL to save tokens:** destroys auditability; write a replacement
  or checkpoint event instead.
- **Summarizing before deterministic compaction:** costs a model call and loses
  structure unnecessarily.
- **Splitting a tool pair at a checkpoint:** makes the projected history invalid.
- **Treating an archive placeholder as lost data:** use the scoped retrieval
  tool; the backing original remains in the session archive.
- **Compressing current source just because it is long:** this is exactly the
  evidence a coding agent may need to patch correctly.

Related: [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.md),
[Tools](TOOLS_DESIGN.md), and [Providers](PROVIDERS_DESIGN.md).
