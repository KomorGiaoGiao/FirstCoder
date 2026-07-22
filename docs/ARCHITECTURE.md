# FirstCoder Architecture

[中文版本](ARCHITECTURE.zh-CN.md)

This document is the architecture textbook for FirstCoder: package boundaries,
dependency rules, the main runtime path, and where a change should land.
Read it with [CODEBASE_READING_GUIDE.md](CODEBASE_READING_GUIDE.md) open, and
prefer opening the cited files while reading rather than memorizing names.

**Audience:** contributors who need to change runtime behavior without
accidentally inventing a second source of truth.

**Not this document:** user onboarding, provider API keys, or benchmark
runbooks. Those live in the root README and the evaluation docs.

---

## 1. Learning Outcomes

After this document, you should be able to:

1. Point to the package that owns a given concern (orchestration, facts,
   projection, tools, permissions, UI).
2. Explain why `tools` / `permissions` / `utils` must not import `agent`.
3. Trace one user turn from CLI/TUI into durable JSONL facts and back to UI.
4. Decide whether a change belongs in `AgentLoop`, `ContextWindowManager`,
   a tool executor, a provider adapter, or a slash-command handler.
5. Name the single assembly path for create / resume / fork sessions.

---

## 2. Mental Model

FirstCoder is a **local coding agent**: a model proposes actions; Python code
executes tools under policy; an append-only log keeps the audit trail.

Think in four concentric layers:

```text
┌────────────────────────────────────────────────────────────┐
│  Presentation: app/ (TUI, slash commands, picker, stream)  │
├────────────────────────────────────────────────────────────┤
│  Composition: factory, SessionBootstrap, ports             │
├────────────────────────────────────────────────────────────┤
│  Orchestration: agent/ (AgentLoop, AgentSession)           │
│    uses: providers, tools, permissions, skills, runtime    │
├────────────────────────────────────────────────────────────┤
│  Facts & projection: context/  (+ session lifecycle)       │
│  Side effects: tools/ executors under permissions/         │
└────────────────────────────────────────────────────────────┘
```

Three contracts matter more than any class name:

| Contract | Meaning |
| --- | --- |
| **Facts vs view** | JSONL events are durable; provider messages are a projection. |
| **Coordinate vs execute** | `AgentLoop` decides *when*; tools/providers do *what*. |
| **Policy vs prompt** | Safety is enforced by code paths; prompts only guide the model. |

If a design idea violates one of these, it is almost always wrong for this
codebase.

---

## 3. Package Map

Rough sizes fluctuate; treat them as orientation, not budgets. At the time of
writing, `firstcoder/` is ~25k lines of Python across 174 files. The largest
packages are `context/`, `app/`, `tools/`, and `agent/`.

| Package | Responsibility | Read first | Must not own |
| --- | --- | --- | --- |
| `runtime/` | Shared cancellation + structured user-input requests | `cancellation.py`, `user_input.py` | Loop policy, tools, UI |
| `app/` | Composition root, TUI, slash commands, UI-edge ports | `factory.py`, `runtime.py`, `ports.py`, `tui.py` | Provider protocol translation |
| `input/` | Attachment discovery, clipboard reads, and session staging | `attachments.py`, `clipboard.py` | Provider wire encoding or widget state |
| `agent/` | One-turn orchestration and the session runtime object | `loop.py`, `session.py`, `loop_limits.py` | Shell/HTTP concrete work |
| `context/` | Append-only facts, projection, compaction L1–L4 | `store.py`, `writer.py`, `context_builder.py`, `manager.py` | Widgets, vendor SDKs |
| `session/` | Catalog / index / new / resume / fork / share | `bootstrap.py`, `catalog.py`, `resume.py` | Model completion |
| `tools/` | Schemas, executors, session registry, hidden-tool list | `builtin.py`, `registry.py`, `session_registry.py`, `hidden.py` | Final permission decisions |
| `permissions/` | allow / ask / deny and grant persistence | `manager.py`, `policy.py`, `grants.py` | Executing tools |
| `providers/` | Internal ↔ vendor protocol adapters | `types.py`, `factory.py`, adapters | Session persistence |
| `skills/` | Discovery, routing, loading audit | `discovery.py`, `router.py`, `loader.py` | Tool registration |
| `mcp/` | External MCP servers exposed as tools | client + manager modules | Core loop control |
| `utils/` | Sandbox access, subprocess, text helpers | `sandbox_access.py` | Business orchestration |
| `config/` | Settings resolution | `loader` / config models | Runtime state |
| `eval/` | Benchmark adapters and metrics | package entry | Product UI |

### How packages talk

```text
cli / app
  ├── session.bootstrap  ──► AgentSession (tools + permissions + skills)
  ├── providers.factory  ──► ChatProvider
  ├── context.manager    ──► compaction decisions
  └── AgentChatRunner    ──► agent.loop.AgentLoop
                                ├── input attachments → session store
                                ├── context.writer / builder
                                ├── providers.complete|astream
                                ├── tools (+ PermissionAwareToolRegistry)
                                └── runtime (cancel / user-input requests)
```

---

## 4. Dependency Rules

### Allowed direction (high level)

```text
cli / app
  -> agent / session / context / providers / mcp / skills

agent
  -> context / tools / providers / permissions / skills / runtime

session
  -> agent (AgentSession object) / context / skills / permissions / runtime helpers

tools / permissions / utils
  -> runtime     # never agent

context
  -> providers.types / tools.types   # data shapes, not orchestration

providers / config
  -> almost nothing above them
```

### Hard rules (with why)

1. **`utils`, `permissions`, and `tools` must not import `agent`.**
   - *Why:* those packages are leaf-ish. If they import the orchestrator, every
     small helper change risks circular imports and UI/test coupling.
   - *Where shared types live:* `firstcoder.runtime`
     (`CancellationToken`, `UserInputRequest`, …).
   - Runtime primitives are imported directly from `firstcoder.runtime`.

2. **UI and CLI depend on ports, not concrete loop internals.**
   - `firstcoder.app.ports`: `CommandHandlerLike`, `ChatRunnerLike`,
     `CurrentSessionLike`, `ContextManagerLike`.
   - `firstcoder.agent.ports`: minimal `ContextManagerLike` for the loop.
   - *Why:* TUI tests and alternate frontends can fake a runner without
     constructing a full model provider.

3. **Session construction is centralized.**
   - `firstcoder.session.bootstrap.SessionBootstrap` is the single assembly
     path for create / resume / fork / factory.
   - *Why:* grants path, skill catalog, AGENTS.md, tools resolution, and
     sandbox wiring used to drift across call sites.

4. **Hidden control-plane tools are listed once.**
   - `firstcoder.tools.hidden.HIDDEN_TOOL_STATUS_NAMES`
   - The session registry retains internal `task_boundary` for the hidden classifier, but the main model request filters it out.

### Soft edges (known, keep narrow)

| Edge | Direction | Note |
| --- | --- | --- |
| Session object lives in agent | `session` → `agent` | Expected: bootstrap builds `AgentSession`. |
| Catalog maintenance | `context.store` → `session.index` (lazy) | Keep lazy; do not add a second write path. |
| Type-only imports | e.g. `runtime.user_input` → `ToolResult` under `TYPE_CHECKING` | Avoid runtime cycles. |

### Anti-patterns

| Do not | Do instead |
| --- | --- |
| Import `AgentLoop` from a tool executor | Return a structured `ToolResult`; let the loop decide |
| Copy grant-store / skill wiring into a new service | Call `SessionBootstrap` |
| Put vendor-specific fields on `AgentSession` | Keep them in the provider adapter |
| Hide a tool from the UI by special-casing `tui.py` | Add the name to `tools.hidden` |
| “Fix” context size by deleting JSONL lines | Use compaction / projection |

---

## 5. One User Turn (Detailed Path)

This is the spine of the system. Memorize the *shape*, not every helper name.

```text
User submits text and optional staged attachments in TUI / CLI
  │
  ▼
firstcoder/cli.py
  -> app.factory.create_firstcoder_app(...)
       builds: store, tools (+ MCP), provider, SessionBootstrap.from_project,
               ContextWindowManager, AgentChatRunner, command router, FirstCoderApp
  │
  ▼
app.runtime.AgentChatRunner.run_user_turn / resume_with_user_input
  │
  ▼
agent.loop.AgentLoop
  1. copy attachments beneath session storage; append user message/metadata
     via the session writer  (durable facts)
  2. initialize the first task, or run hidden task-boundary classification
  3. compact triggers:
       _auto_compact
       _compact_for_prompt_too_long
       _compact_after_task_hash_changed
     → ContextWindowManager.compact_if_needed
  4. ContextBuilder builds ChatRequest(messages, tools, system)
  5. provider.complete or provider.astream
  6. for each tool_call:
       session tool registry (+ permissions preflight)
       on ASK: pause with UserInputRequest (kind=permission_confirmation)
       on direct file mutation: build trusted prewrite diff; standard asks for
         permission, allowed/aggressive paths ask for review-only Apply
       on allow: execute; append tool result fact
  7. settle according to AgentLoopLimits; verification results return to the model as evidence
  8. if the model stops with unfinished active-task-plan work, send one
     ephemeral system instruction to reconcile it; it is neither a user message
     nor a durable fact
  │
  ▼
runtime events → AgentChatRunner → TUI transcript / activity / permission UI
```

### Durable vs process-local state

| Durable (survive process exit) | Process-local (rebuild or lose) |
| --- | --- |
| `.firstcoder/sessions/<id>.jsonl` facts | `SessionRuntimeState` |
| `.firstcoder/attachments/<session-id>/` staged attachment bytes | pending attachment chips in the composer |
| permission grants file (`permissions.json`) | pending permission original tool_call |
| `task_plan_updated` snapshots replayed into `SessionView.task_plan` | current review-card expansion state |
| skill files on disk | prompt prefix cache |
| MCP server configs | live MCP connections |

Resume rebuilds what it can by replaying JSONL. Anything that must not be lost
across restarts must be a fact or an explicit grant—not only a Python object.

Successful TaskPlan mutations emit runtime tool events so the TUI refreshes
immediately. Replay later projects the same `task_plan_updated` snapshot; the
UI labels it as model-reported state and does not infer completion from commands,
file mutations, or passing tests. `linear` plans derive their sequence from
stable order; `dag` plans use explicit dependencies. All ordinary mutations
address stable task IDs and carry the current revision. A revision conflict is
resolved by reading `task_list` and retrying; it is never resolved by replacing
the whole plan.

TaskPlan sessions use a strict schema boundary. Resume and fork reject old,
missing-version, and future-version event schemas before rebuilding state. There
is no migration, legacy-event fallback, or tool-result reconstruction path.

### Key objects that cross boundaries

| Object | Package | Role |
| --- | --- | --- |
| `ChatRequest` / `ChatResponse` | `providers.types` | Internal model I/O |
| `UserAttachment` / `PreparedAttachment` | `input.attachments` | composer input and session-safe attachment metadata |
| `ContentPart` | `providers.types` | provider-neutral text/image content projection |
| `Tool` / `ToolCall` / `ToolResult` | `tools.types` | Schema + execution result |
| `PermissionRequest` / decision | `permissions` | allow / ask / deny |
| `UserInputRequest` | `runtime.user_input` | Pause for human (permission or ask_user) |
| `ContextCompactRequest` | `context.manager` | Whether/how to compact |
| `AgentTurnResult` / status | `agent.user_input` | Turn outcome for the app layer |
| `AgentLoopLimits` | `agent.loop_limits` | Round / call / time budgets |

---

## 6. Session Assembly

`SessionBootstrap` is intentionally boring: one place that knows how a
project-bound `AgentSession` is built.

```text
SessionBootstrap
  resolve tools (static list or tools_provider)
  permission_manager = project policy + FilePermissionGrantStore(data_root)
  create / resume / from_project
      -> AgentSession.*
           writer, runtime_state, session tool registry,
           agents_md, skill_catalog, sandbox_access
```

Call sites:

- `session.new.NewSessionService`
- `session.resume.ResumeService`
- `session.fork.ForkSessionService`
- `app.factory.create_firstcoder_app`

If you are about to paste “create PermissionManager + discover skills + read
AGENTS.md” into a fifth file, stop and extend `SessionBootstrap` instead.

### Catalog public API

Session discovery helpers are public on purpose:

- `session.catalog.record_from_path`
- `session.catalog.build_record_from_events`
- `session.catalog.session_sort_key`

`session.index` imports these public functions. Compatibility aliases such as
`_record_from_path` may remain for older tests/monkeypatches, but new code
should use the public names.

---

## 7. Orchestration Inside `AgentLoop`

`AgentLoop` is a **transaction manager for one user turn**, not a kitchen sink.

It owns:

- turn lifecycle (start → model ↔ tools → settle → stop)
- compact *triggers* (when to ask the context manager)
- pause/resume for permissions and `ask_user`
- stop reasons from `AgentLoopLimits` (`tool_round_limit`,
  `provider_call_limit`, `turn_timeout`)

It does **not** own:

- vendor HTTP details (providers)
- file/shell side effects (tools)
- token math and L1–L4 algorithms (context)
- widget rendering (app)

Related modules (splits of loop concerns):

| Module | Concern |
| --- | --- |
| `agent/tool_execution.py` | Executing and recording tool calls |
| `agent/tool_flow.py` | Flow control around tool batches |
| `agent/tool_settlement.py` | Settling tool outcomes into the turn |
| `agent/task_boundary_classifier.py` | Task-boundary classification |
| `agent/task_plan_policy.py` | Active TaskPlan lookup and one-time final reconciliation instruction |
| `agent/loop_limits.py` | Budgets and stop-reason enums |

### Compact trigger helpers

Prefer named helpers over re-assembling trigger flags at each call site:

- `_auto_compact()`
- `_compact_for_prompt_too_long()`
- `_compact_after_task_hash_changed()`

They wrap `_compact_if_needed` so the *intent* is readable in the loop.

---

## 8. Context: Facts, Projection, Compaction

Ownership split:

| Layer | Owner | Role |
| --- | --- | --- |
| Append-only log | `context.store.JsonlSessionStore` | Bytes on disk |
| Write API | `context.writer.SessionEventWriter` | Typed appends for the session |
| Effective facts | replay → `SessionView` / runtime replay helpers | What “is true now” |
| Provider projection | `context.context_builder.ContextBuilder` | `ChatMessage[]` for this request |
| Compact routing | `context.manager.ContextWindowManager` | Whether / which level |
| L1–L3 | `context.compaction` / `context.content.*` | Deterministic compression |
| L4 | `context.llm_compact` | Model-authored coding handoff |

```text
JSONL events
  -> replay -> SessionView
  -> ContextBuilder -> ChatMessage[] (+ tools schema on ChatRequest)
  -> provider
```

Invariants worth tattooing on your monitor:

1. Compaction does **not** delete the audit log; it changes the projection.
2. Provider history must never start with an orphan `role=tool` message.
3. Every tool result keeps its original `tool_call_id` pairing.
4. Tool schemas travel on `ChatRequest.tools`, not by being pasted into the
   system prompt.

Deep dive: [CONTEXT_MANAGEMENT_DESIGN.md](CONTEXT_MANAGEMENT_DESIGN.md).

---

## 9. Tools, Permissions, and Human Pauses

```text
model tool_call
  -> PermissionAwareToolRegistry
       PermissionManager.preflight
         ALLOW  -> execute tool
         DENY   -> structured denial result (still a tool message)
         ASK    -> UserInputRequest(kind="permission_confirmation")
                   AgentLoop stores original tool_call locally
                   UI answers by request_id
                   resolve_confirmation -> resume execution
```

`ask_user` uses the same `UserInputRequest` shape with `kind="ask_user"`.

For `write`, `edit`, `apply_patch`, and `delete`, `ToolExecutor` builds a
trusted `PrewriteReview` before execution. In standard mode the diff is part of
the normal permission pause; an `ALLOW` decision (including aggressive mode or
a matching grant) still becomes a review-only Apply pause. Bypass emits the
same diff as a non-blocking event and proceeds; non-interactive benchmark
adapters can explicitly disable it. Resume rechecks the saved snapshots, and
the UI never supplies the executable call payload.

Critical safety rule: the pending original `tool_call` must come from **local
session state**, never from a model-replayed payload the user could not see.

Hidden tools (`tools.hidden`) still execute when called; they are only omitted
from noisy human-facing activity streams.

Deep dives: [TOOLS_DESIGN.md](TOOLS_DESIGN.md),
[PERMISSIONS_DESIGN.md](PERMISSIONS_DESIGN.md).

---

## 10. Providers and Extension Seams

Providers convert internal `ChatRequest` / stream events to vendor protocols
(OpenAI-compatible, Anthropic, …) and back. They must not write sessions or
decide permissions.

Skills are discovered and routed into the prompt surface; they are not a
parallel tool registry. MCP servers *are* extra tools, merged at composition
time via the factory’s tool provider, and still pass through permissions.

Deep dives: [PROVIDERS_DESIGN.md](PROVIDERS_DESIGN.md),
[SKILL_SYSTEM_DESIGN.md](SKILL_SYSTEM_DESIGN.md), [MCP.md](MCP.md).

`ContextBuilder` also projects persisted image attachments into `ContentPart`
values at request time. It reads only paths that resolve under the session store;
the JSONL log stores relative paths and metadata, never image base64.

---

## 11. Decision Tree: Where Does My Change Go?

```text
Is it how a vendor HTTP body is shaped?
  -> providers/ (+ config)

Is it a new local capability the model can call?
  -> tools/ (Tool + permission spec), register in builtin/session registry

Is it when allow/ask/deny happens?
  -> permissions/policy.py or grants

Is it what the model is allowed to *see* from history?
  -> context projection / compaction (never delete JSONL for this)

Is it stop/pause/continue of a turn?
  -> agent/loop.py (+ loop_limits / tool_* helpers)

Is it a slash command or TUI widget?
  -> app/ commands / views; call session services, do not reimplement them

Is it create/resume/fork wiring of grants, skills, tools?
  -> session/bootstrap.py

Is it a shared cancel or user-input DTO needed by tools/permissions?
  -> runtime/
```

---

## 12. New-Code Checklist

1. Would a lower layer import `agent` only for a type? Move the type to
   `runtime` (or another neutral package).
2. Creating or restoring a session? Use `SessionBootstrap`.
3. New command or chat-runner dependency? Extend `app.ports` first.
4. Tool that should not spam the status stream? Add it to `tools.hidden`.
5. Changing compaction strategy? Prefer `context.manager` / pipeline over the
   loop or TUI.
6. Does a runtime-behavior claim in a PR description point at a real file?
   If not, the design is still fog.

---

## 13. How to Verify Architecture Claims

```sh
# dependency intent: tools/permissions/utils should not import agent
rg -n "from firstcoder\.agent|import firstcoder\.agent" firstcoder/tools firstcoder/permissions firstcoder/utils

# single assembly path
rg -n "SessionBootstrap" firstcoder tests

# ports surface
rg -n "ChatRunnerLike|CommandHandlerLike|ContextManagerLike" firstcoder tests

# hidden tools single list
rg -n "HIDDEN_TOOL_STATUS_NAMES" firstcoder tests

# focused suite often used after architecture edits
.venv/bin/python -m pytest \
  tests/test_app_tui.py tests/test_session_*.py \
  tests/test_app_factory.py tests/test_app_runtime.py \
  tests/test_cli.py tests/test_permissions_manager.py \
  tests/test_permission_results.py -q
```

Use `pytest tests` (or explicit files), not a bare repo-wide `pytest`:
generated benchmark trees can contain their own `tests/` directories.

---

## 14. Related Documents

| Topic | Doc |
| --- | --- |
| First end-to-end reading route | [CODEBASE_READING_GUIDE.md](CODEBASE_READING_GUIDE.md) |
| TUI assembly and streaming | [CLI_TUI_DESIGN.md](CLI_TUI_DESIGN.md) |
| Turn stop / pause / continue | [AGENT_LOOP_GUARDRAILS.md](AGENT_LOOP_GUARDRAILS.md) |
| Facts and compaction | [CONTEXT_MANAGEMENT_DESIGN.md](CONTEXT_MANAGEMENT_DESIGN.md) |
| Tools | [TOOLS_DESIGN.md](TOOLS_DESIGN.md) |
| Permissions | [PERMISSIONS_DESIGN.md](PERMISSIONS_DESIGN.md) |
| Providers | [PROVIDERS_DESIGN.md](PROVIDERS_DESIGN.md) |
| Multimodal attachment path | [MULTIMODAL_INPUT_DESIGN.md](MULTIMODAL_INPUT_DESIGN.md) |
| Skills | [SKILL_SYSTEM_DESIGN.md](SKILL_SYSTEM_DESIGN.md) |
| MCP | [MCP.md](MCP.md) |
| Index of all tech docs | [README.md](README.md) |

---

## 15. Document History Notes

Architecture moves in this tree preferred **structural** changes (extract
shared modules, ports, bootstrap) over feature rewrites. That can temporarily
*increase* line count (shims, ports, extra files) while reducing coupling.
Line-count goals and decoupling goals are not the same optimization.
