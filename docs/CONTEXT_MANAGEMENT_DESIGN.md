# Context Management Design

[中文版本](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)

> Status: implemented v2 context-compression contract. The runtime uses lifecycle-gated L1-L4 compaction as described below. Legacy archive records and placeholders remain readable for session-resume compatibility; they do not define the behavior of new compaction writes.

## Purpose

FirstCoder must keep long coding sessions usable without losing the exact source, diagnostics, and tool-call structure needed for the next action. The context system therefore has two jobs:

1. keep a durable, auditable session record that can be resumed; and
2. project a smaller, provider-valid working context from that record.

The v2 design compresses tool outputs according to their lifecycle. It treats current source reads conservatively, makes lossy tool-output compaction reversible, and escalates to an LLM checkpoint only after deterministic work cannot meet the budget.

This is not a generic conversation summarizer.

## Architectural Foundations

### Append-only facts and effective views

`JsonlSessionStore` is the durable source of truth. Raw message events are never edited in place. Compaction appends a `compaction_completed` replacement event; L4 appends a `checkpoint_created` event. Replaying those events produces `SessionView`, the effective session view.

`SessionRuntimeState` holds replayable runtime facts that are not natural-language messages, including the active task hash, task-boundary stability, checkpoint identity, compaction history, and automatic-compaction circuit breaker.

`ContextBuilder` is the only component that turns `SessionView` into provider messages. It must preserve the provider's required sequence:

```text
assistant(tool_call) -> tool(tool_result)
```

Neither compaction nor checkpointing may leave an orphan tool result or a pending tool call in a projected tail.

### Ownership boundaries

| Component | Owns | Does not own |
|---|---|---|
| `context/tool_lifecycle.py` | Deterministic classification of a tool result as fresh, stale, superseded, derived, or duplicate. | Archive I/O, token policy, content compression. |
| `context/compaction.py` | L1-L3 ordering, lifecycle gating, token budgets, and replacement events. | Provider requests or route-specific parsing. |
| `context/content/*` | Content-type detection and deterministic compression candidates. | Lifecycle inference, session storage, retrieval. |
| `context/archive.py` | Session-local original-content backing store, archive metadata, and placeholders. | Tool registration and compaction selection. |
| `tools/retrieve_archive.py` | Bounded model-visible recovery of an archive. | Direct file-path access or lifecycle classification. |
| `context/manager.py` | Trigger policy, target selection, L1-L4 escalation, persistence of outcomes. | Tool execution and checkpoint-summary generation. |
| `context/llm_compact.py` | L4 checkpoint lifecycle and legal tail validation. | Deterministic L1-L3 transformations. |
| `context/context_builder.py` | Provider projection, checkpoint summary insertion, tool-sequence validation. | Persistent mutation or compaction decisions. |

These boundaries prevent a second archive system, a second compaction pipeline, or provider-specific logic leaking into session storage.

## Non-negotiable Invariants

- Raw JSONL events and archived originals remain append-only.
- A replacement keeps its `message_id`, `part.id`, `tool_call_id`, tool name, ordering, and success/error metadata.
- System/developer instructions, the latest user request, the stable system prefix, and the current task's exact source reads are never lossy L1-L3 inputs.
- Any accepted lossy L2 tool-result transformation has an original-content backing record before the replacement is persisted.
- Unknown read or mutation shapes fail open: they are never used to mark a source read stale or superseded.
- A model can retrieve an archived original only from its current session (or the copied archive directory of a fork).
- L1-L3 never call a model. L4 is the only semantic summary layer.
- Re-running compaction after resume is idempotent: no second archive, no duplicate replacement, and no expansion of archived text.

## Compaction Pipeline

```text
effective tail
  -> lifecycle index
  -> L1: trim safe old-task dialogue
  -> L2: type-aware reversible tool-result compression
  -> L3: archive placeholder selection
  -> L4: coding handoff checkpoint only if still over budget
```

The pipeline only works on the effective tail after the latest checkpoint. It must not compact raw history that the active checkpoint has already covered.

### L1 — old-task dialogue trimming

L1 is deliberate forgetting, not summarization. It trims only plain `text` parts that are confirmed to belong to an old task. It does not touch tool results, tool-call transactions, the latest user message, or an assistant message that contains a tool call.

Trimmed parts stay in durable raw events but are marked `compaction_state="trimmed"` in the effective view. `ContextBuilder` omits trimmed text and may inject one synthetic marker, `[Earlier dialogue trimmed]`, for the whole tail. It must not add one marker per trimmed part.

L1 does not use keyword heuristics to delete old dialogue from the active task. If active-task semantics still need to shrink, L4 is the safe fallback.

### L2 — typed, reversible tool-result compression

L2 accepts only lifecycle-eligible `tool_result` parts, normally `derived` outputs. It first applies safe formatting cleanup when it reduces size, then asks the existing `RouteCompactRouter` for one content-specific candidate. A candidate is used only when it is strictly smaller than the original.

Before a lossy candidate replaces a raw tool result, the original is stored through `ToolResultArchive`. The provider then sees the compact result, while `retrieve_archive` can recover the raw original later.

The route layer preserves structure rather than inventing prose:

| Content | Required retained information |
|---|---|
| search results | paths, line numbers, representative matches, omitted counts |
| build/test logs | summary, failed tests, error blocks, traceback/stack context |
| unified diffs | file and hunk headers, additions/deletions, limited context |
| JSON | parseable compact structure, errors/status fields, schema and representative items |
| HTML | title, headings, visible text, links |
| lists and directory output | item kind/header, count, representative paths, truncation signal |

L2 does not compress a recognized fresh source read. Existing content routers can still compress source-code-shaped **derived** output, but they cannot bypass the lifecycle gate.

### L3 — prompt eviction with retrieval

L3 replaces selected tool results with a small archive placeholder. It does not delete the original or remove the tool result from its transaction.

The following results are mandatory L3 candidates:

- a stale source read, after a successful known mutation of the same path;
- a superseded source read, after a later known read covers the same range;
- an older duplicate derived result whose content hash matches a later derived result.

Large or old derived results are also L3 candidates when L2 did not meet a per-result budget or the context still exceeds its target. Fresh source reads and current-turn retrieval results are never L3 candidates.

New placeholders are bounded and contain only archive id, tool name, status, lifecycle, original token count, short deterministic summary, limited error lines, and retrieval instructions. They intentionally do not expose an arbitrary raw preview, especially for archived source.

### L4 — coding handoff checkpoint

L4 runs only after L1-L3 cannot meet the active target, or after a prompt-too-long blocking pass still cannot meet its blocking target. `LlmCompactService` remains the only writer of checkpoints.

The summarizer produces a fixed coding handoff with these sections:

```text
## 当前目标
## 已知事实与硬约束
## 已确认的决定及理由
## 相关文件与当前实现状态
## 已运行命令及有效结果
## 当前错误与未解决事项
## 下一步（可立即执行）
```

The headings are deliberately fixed Chinese strings. If a section has no evidence, the model writes `无`. The model writes summary content only. Local code chooses and validates the checkpoint tail boundary so it begins at a legal tool-call transaction boundary.

## Tool-result Lifecycle

Lifecycle classification is a deterministic pass over the effective tail. It associates each tool result with its preceding tool call through `tool_call_id`; it uses only structured tool arguments and `ToolResult.data`, never display-text guesses.

| State | Meaning | Allowed action |
|---|---|---|
| `fresh` | A known source read has not been replaced by a later covering read and its path has not had a known successful mutation. | Keep exact content; no L2/L3. |
| `stale` | A known successful mutation touched the source-read path after the read. | Archive directly in L3. |
| `superseded` | A later known source read covers the earlier read's path/range. | Archive directly in L3. |
| `derived` | Search, logs, diffs, JSON, HTML, lists, unknown tools, and other non-source-read output. | L2, then L3 if needed. |
| `duplicate` | An older derived result has the same full content hash as a later derived result in the effective tail. | Reuse backing and archive in L3. |

Initial support is intentionally narrow:

- source reads: successful `view` and non-truncated successful `read_multi` with valid structured file metadata;
- mutations: successful `write`, `edit`, `delete`, and structured `apply_patch` results;
- shell commands, unknown tools, malformed metadata, partial `read_multi`, and ambiguous ranges are not inferred as source mutations or source reads.

This conservative boundary matters more than compression ratio: a false stale classification can remove the exact file context needed to make a correct patch.

## Archive and Retrieval Contract

`ToolResultArchive` is the sole owner of the session-local backing store at:

```text
<store.root>/archives/<session-id>/<archive-id>.txt
<store.root>/archives/<session-id>/<archive-id>.json
```

New archive ids are content-addressed from the complete UTF-8 SHA-256 of original text. Identical content in one session reuses the same backing record. Backing metadata records the hash, size, token estimate, creation time, and archive schema version; part-specific lifecycle provenance remains on the replacement part, not in a shared record.

Archive reads derive paths from a validated session id and archive id. They never trust a model-supplied path or a historical `archive_path` field. New writes are atomic and must verify an existing content-addressed file before reusing it. Legacy archive ids and placeholders remain readable for resume compatibility.

`retrieve_archive` is session-scoped, not a generic filesystem tool. Its schema is:

```text
retrieve_archive(archive_id, query=None, max_chars=6000, full=False)
```

- `max_chars` is bounded to 1–12,000.
- A query returns literal matching lines with small neighboring context and line numbers.
- A full request returns only the leading bounded segment and reports truncation.
- Missing, invalid, cross-session, or hash-mismatched records return safe tool errors without filesystem paths.
- Retrieval output is pinned for its producing turn so automatic compaction cannot immediately archive it again.

The tool is injected by `create_session_tool_registry`, because it needs the current session id and store root. It must not be added to the stateless builtin registry.

## Trigger and Budget Policy

`ContextWindowManager` remains the single trigger and escalation point.

| Trigger | Required behavior |
|---|---|
| `AUTO` | Use existing token/tail/tool-output heuristics, then L1 -> L2 -> L3. Enter L4 only if still over target. |
| `TASK_HASH_CHANGED` | Use a lower task-switch target and require L2/L3 to run even if L1 already meets the ordinary target. This removes old derived context after a confirmed task switch. |
| `MANUAL` | Run deterministic compaction under the normal target, but never override fresh-source protection. |
| `PROMPT_TOO_LONG` | Run one blocking deterministic pass, then L4 if required; retry the original provider request at most once. |

Automatic compaction may honor the circuit breaker. Manual, task-boundary, and provider-overflow recovery must not be skipped by it.

Budgets belong in `ContextCompactionConfig`, including a normal target, optional blocking target, task-switch target, and L2 per-result target. Compressors and archive tools do not embed global token-policy constants.

## Resume, Fork, and Observability

Compaction facts are persisted through the existing `compaction_completed` event and replayed as replacement parts. Event additions must be backward-compatible: old sessions have no lifecycle metadata and are classified conservatively at runtime; old archive placeholders remain projectable.

A session fork copies its archive directory to the fork's session id, matching the copied JSONL. Archives are session-local: do not introduce cross-session deduplication or shared mutable memory.

Compaction events should expose enough information to diagnose savings without reparsing content: per-level before/after tokens, changed counts, lifecycle counts, archive ids/counts, and trigger/target. Existing inspector, catalog, transcript, and runtime replay paths should consume these as additive fields.

### Read-only evaluation metrics

Evaluation derives its metrics directly from the session transcript (JSONL events, the replacement-aware effective view, and that session's archive directory). It is read-only: it does not add a second state store, mutate the transcript, or maintain a separate archive index.

| Metric group | Derived values |
|---|---|
| Compaction savings | Total before/after tokens, total savings, and savings per L1/L2/L3 level when level metrics are present. |
| Archive footprint | Count and byte size of that session's archived original `.txt` payloads. |
| Recovery and handoff | Successful `retrieve_archive` executions and successfully completed L4 events. |
| Tool-output mix | Transcript-order tool-result counts by tool name, plus effective replacement content-type counts. |
| Source rereads | Repeated successful structured `view`/`read_multi` source targets in transcript order. |

All of these fields are additive and backward-compatible. Missing legacy event fields, absent archives, or unrecognized tool metadata produce partial/zero metrics rather than requiring migration or changing compaction behavior.

## External Reference Boundary

The design borrows only three ideas from Headroom: content-aware routing, reversible local backing for compressed tool output, and on-demand retrieval. It does not embed Headroom or adopt its proxy, MCP server, learned/ML compression, SQLite store, cross-agent memory, TTL/LRU eviction, provider request rewriting, or provider-cache optimization.

FirstCoder already owns the agent loop, session event log, provider projection, and tool registry. Reusing those seams keeps the design local, replayable, and provider-neutral.

## Verification Scope

The implemented behavior is verified by focused context/session/agent tests. Full-repository and benchmark runs remain separate release checks because they may depend on optional benchmark fixtures, providers, or external task environments.

The core verification conditions are:

- fresh `view`/`read_multi` source reads cannot be lossy-compressed merely because they are large;
- stale and superseded reads are archived and recoverable;
- type-specific L2 compression happens before optional L3 eviction for derived output;
- L2 transformations always have original backing and `retrieve_archive` is bounded and session-safe;
- task switches run L2/L3 below the ordinary automatic threshold;
- provider projection remains tool-sequence valid after every L1-L4 outcome;
- resume/fork remain idempotent and backward-compatible; and
- focused context/session/agent tests preserve provider-valid tool transactions and resume/fork compatibility.
