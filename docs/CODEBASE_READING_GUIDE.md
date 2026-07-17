# Codebase Reading Guide

[中文版本](CODEBASE_READING_GUIDE.zh-CN.md)

This is a **route through the running system**, not a folder inventory. Use it
when you need “where should I change this?” without cargo-culting a nearby file.

Pair with [ARCHITECTURE.md](ARCHITECTURE.md) for package rules and the coupling
cleanup. This guide focuses on *how to learn* the code: timeboxed paths,
end-to-end traces, exercises, and debugging recipes.

---

## 1. What You Will Be Able To Do

| After… | You should be able to… |
| --- | --- |
| 30 minutes | Name the main turn path and open the five critical files |
| 60 minutes | Trace one tool call from model output to JSONL to UI |
| 2 hours | Place a small change in the correct layer and name the tests to run |

---

## 2. Timeboxed Learning Paths

### Path A — 30 minutes: get the map

1. Skim [ARCHITECTURE.md](ARCHITECTURE.md) §§2–5 (mental model + one turn).
2. Open these files in order (do not deep-read yet):
   - `firstcoder/cli.py`
   - `firstcoder/app/factory.py` (`create_firstcoder_app`)
   - `firstcoder/app/runtime.py` (`AgentChatRunner`)
   - `firstcoder/agent/loop.py` (`AgentLoop`)
   - `firstcoder/session/bootstrap.py` (`SessionBootstrap`)
3. Run:

```sh
rg -n "class AgentLoop|def create_firstcoder_app|class SessionBootstrap" firstcoder
```

### Path B — 60 minutes: one real capability

Pick **one** of:

- loop limits → `agent/loop_limits.py` + stop handling in `loop.py`
- permission ask → `permissions/manager.py` + resume in `AgentChatRunner`
- compaction trigger → `AgentLoop` helpers + `context/manager.py`

For the chosen topic:

1. `rg -n "keyword" firstcoder tests`
2. Read the owning module, then **one** caller, then **one** test.
3. Run the narrowest pytest file you found.

### Path C — 2 hours: change-ready

1. Finish Path B.
2. Read the matching design doc end-to-end (tools / permissions / context / …).
3. Complete Exercise 1 and Exercise 2 below without editing product code first.
4. Only then make a one-file change and re-run the focused tests.

---

## 3. Dependency Rules (Short)

- `utils` / `permissions` / `tools` import `firstcoder.runtime`, **never**
  `firstcoder.agent`.
- Build sessions through `session.bootstrap.SessionBootstrap`.
- UI/CLI boundaries use `app.ports` / `agent.ports`.
- Hidden status tools live in `tools.hidden.HIDDEN_TOOL_STATUS_NAMES`.

If a PR violates these, review architecture before debating style.

---

## 4. Start With One Real Turn

Trace a request such as **“find the loop limit and explain it”**:

```text
firstcoder/cli.py
  -> firstcoder/app/factory.py:create_firstcoder_app
       SessionBootstrap.from_project
       ContextWindowManager
       AgentChatRunner
       command router + FirstCoderApp
  -> firstcoder/app/runtime.py:AgentChatRunner.run_user_turn
  -> firstcoder/agent/loop.py:AgentLoop
  -> ContextBuilder builds ChatRequest(messages, tools, system)
  -> provider.complete / astream
  -> session tool registry execute (+ permissions)
  -> append facts to .firstcoder/sessions/<id>.jsonl
  -> runtime events back to TUI / CLI
```

That trace teaches the central contract:

| Layer | Does | Does not |
| --- | --- | --- |
| Loop | Coordinate the turn | Call vendor SDKs directly for business logic |
| Provider | Translate protocols | Persist sessions / decide permissions |
| Tools | Local side effects | Own final allow/deny policy |
| Context | Persist + project facts | Render widgets |
| App/TUI | Present + collect input | Reimplement tool execution |

---

## 5. The Map (Where To Look)

| Area | Read first | Owns | Does not own |
| --- | --- | --- | --- |
| `runtime/` | `cancellation.py`, `user_input.py` | Shared cancel + user-input DTOs | Loop policy, UI |
| `app/` | `factory.py`, `runtime.py`, `tui.py`, `ports.py` | Wiring, ports, terminal UX | Model protocol translation |
| `agent/` | `loop.py`, `session.py`, `loop_limits.py` | One-turn orchestration | Concrete shell/HTTP |
| `context/` | `store.py`, `writer.py`, `context_builder.py`, `manager.py` | Durable facts + projection | UI widgets |
| `tools/` | `builtin.py`, `registry.py`, `session_registry.py` | Schemas, dispatch, session wrappers | Final permission policy |
| `permissions/` | `manager.py`, `policy.py`, `grants.py` | allow/ask/deny + grants | Executing a tool |
| `providers/` | `types.py`, `factory.py`, adapters | Internal ↔ vendor conversion | Session persistence |
| `skills/` | `discovery.py`, `router.py`, `loader.py` | Skill catalog + load audit | Tool registration |
| `session/` | `bootstrap.py`, `catalog.py`, `resume.py`, `fork.py` | Lifecycle services | Agent turn semantics |
| `mcp/` | manager + client modules | External tools as first-class tools | Core loop control |

---

## 6. Four-Pass Reading Method

Use this instead of randomly opening large files.

### Pass 1 — Name the behavior

Search the visible command, tool name, error string, or UI label:

```sh
rg -n "term" firstcoder tests
```

Avoid guessing from filenames alone (`manager.py` is not unique enough).

### Pass 2 — Find the seam

Follow imports toward an interface or dataclass before an implementation:

- `ChatProvider`
- `Tool` / registry protocols
- `ContextManagerLike`
- `ChatRunnerLike`
- `PermissionRequest` / `UserInputRequest`

Seams tell you what is allowed to change independently.

### Pass 3 — Follow data, not only control flow

For one turn, keep these objects in view:

| Object | Why it matters |
| --- | --- |
| `ChatRequest` | What the model actually receives |
| `ChatResponse` / stream events | What came back |
| `ToolCall` / `ToolResult` | Local work unit |
| Session JSONL events | Durable truth |
| `UserInputRequest` | Why the turn paused |
| `AgentTurnResult` | What the UI was told |

If you only follow `if` branches, you will miss projection and resume bugs.

### Pass 4 — Read the test next

Tests specify error handling and invariants more precisely than happy-path code.
Run the narrow test before editing:

```sh
.venv/bin/python -m pytest tests/test_whatever.py -q
```

Use `pytest tests` or explicit paths—not a bare repository-wide `pytest`—because
generated benchmark trees may ship their own `tests/` directories.

---

## 7. Glossary (Minimum Viable)

| Term | Meaning in FirstCoder |
| --- | --- |
| **Turn** | One user submission handled by `AgentLoop` until stop or pause |
| **Fact** | An append-only session event (or derived effective state after replay) |
| **Projection** | Provider-visible messages built from facts (`ContextBuilder`) |
| **Compaction** | Shrinking the projection; does not erase the audit log |
| **Checkpoint** | L4 handoff summary used as a new projection basis |
| **Grant** | Remembered allow decision for a scope |
| **Port** | `Protocol` boundary so UI/tests do not hard-bind implementations |
| **Bootstrap** | Single assembly path for a project-bound `AgentSession` |
| **Hidden tool** | Still callable; omitted from noisy human status streams |
| **Bypass mode** | A permission *policy mode*, not “no safety code” |

---

## 8. Useful Entry Commands

```sh
# orchestration & assembly
rg -n "class AgentLoop|def create_firstcoder_app|class SessionBootstrap" firstcoder tests

# request / tool / permission seams
rg -n "ChatRequest\(|tool_registry\.execute|preflight\(" firstcoder tests

# pause / resume human input
rg -n "UserInputRequest|resume_with_user_input|permission_confirmation" firstcoder tests

# compact triggers
rg -n "_auto_compact|compact_if_needed|ContextWindowTrigger" firstcoder tests

# full unit suite (from repo root)
.venv/bin/python -m pytest tests -q
```

---

## 9. First Changes: Choose The Correct Layer

| You want… | Change… | Do not… |
| --- | --- | --- |
| New vendor option | `providers/` + config | Patch the loop with vendor fields |
| New local capability | new `Tool` + permission spec | Add ad-hoc `if` in `AgentLoop` |
| Different approval rule | `permissions/policy.py` or grants | Hardcode allow in a tool |
| Different history visibility | context projection / compaction | Destructively edit JSONL |
| New slash command | `app/` command handlers | Bypass session services |
| Shared cancel/input DTO | `runtime/` | Import `agent` from tools |

---

## 10. Common Wrong Turns

**“The system prompt is the whole context.”**  
No. It is a stable prefix. `ContextBuilder` projects session history separately;
tool schemas ride on `ChatRequest.tools`.

**“Bypass means no safety code exists.”**  
No. Bypass is a policy mode. Execution still goes through the registry and
returns structured results.

**“Compaction deleted history.”**  
No. JSONL facts remain; a checkpoint changes the current provider view.

**“The TUI owns the session.”**  
No. The TUI displays and routes. Session facts live under `context` / `.firstcoder`.

**“High out-degree means the module is badly designed.”**  
Not always. Factories and the agent loop *should* depend on many collaborators.
High *in-degree* on a leaf that imports upward is the smell.

**“I can trust the model to resend the pending tool_call after approval.”**  
No. Resume must use the locally stored original call.

---

## 11. Debugging Recipes

### Permission UI appeared but resume does nothing

1. Confirm `UserInputRequest.id` matches what the UI submits.
2. Trace `AgentChatRunner.resume_with_user_input`.
3. Ensure the original `tool_call` is restored from session/runtime state.
4. Tests: `rg -n "resume_with_user_input|permission_confirmation" tests`.

### Model says it called a tool but workspace unchanged

1. Did the call become a durable tool result event?
2. Was the decision `ASK` or `DENY`?
3. Did the executor raise and get normalized into a `ToolResult`?
4. Follow `PermissionAwareToolRegistry` then the concrete tool module.

### Resume lost the thread of work

1. Open `.firstcoder/sessions/<id>.jsonl` and check events actually appended.
2. Replay path: store → view → `ContextBuilder`.
3. Look for checkpoint / compaction events that changed the projection.
4. Confirm you did not expect process-local `SessionRuntimeState` to survive.

### “Tool not found” / MCP tool missing

1. Factory tool list: builtins vs `McpToolProvider` merge.
2. MCP connect happened? Background connect failures are easy to miss.
3. Session registry vs global registry: session-scoped tools need
   `create_session_tool_registry`.

### Turn stopped with a limit reason

1. Read `agent/loop_limits.py` and the stop-reason enum.
2. Find where `AgentLoop` records `tool_round_limit` / `provider_call_limit` /
   `turn_timeout`.
3. Check whether the caller passed a non-default `AgentLoopLimits` profile
   (`default`, `swe_lite`, `summary`, …).

---

## 12. Progressive Exercises

Do these with the repo open. Prefer reading and tests over speculative edits.

### Exercise 1 — Loop limits (safe, no product edit)

1. Open `firstcoder/agent/loop_limits.py`.
2. Run:

```sh
rg -n "max_tool_rounds|TOOL_ROUND_LIMIT|AgentLoopLimits" tests firstcoder
.venv/bin/python -m pytest tests -q -k "loop_limit or tool_round or AgentLoopLimits" 
```

3. Write (for yourself) one sentence: *what stops, what is recorded, who sees it.*

### Exercise 2 — Bootstrap is the only assembly path

1. List call sites:

```sh
rg -n "SessionBootstrap|AgentSession.create|AgentSession.resume" firstcoder
```

2. Confirm new/resume/fork/factory go through bootstrap for grants/skills/tools.
3. If you find a parallel assembly path, treat it as debt—not as a template.

### Exercise 3 — Trace one permission ask

1. Start from `PermissionManager.preflight` / `build_confirmation`.
2. Find how `UserInputRequest` reaches the TUI.
3. Find how the answer returns to tool execution.
4. Note where the original `tool_call` is stored.

### Exercise 4 — Projection vs facts

1. Read `ContextBuilder.build_provider_messages`.
2. Find one compaction event type in `context/` and the test that protects
   tool-call ordering.
3. Explain why deleting a JSONL line is not a valid “fix context overflow”.

---

## 13. Tests As Documentation

| Concern | Good starting tests (search) |
| --- | --- |
| App assembly | `tests/test_app_factory.py`, `tests/test_cli.py` |
| TUI / runner | `tests/test_app_tui.py`, `tests/test_app_runtime.py` |
| Sessions | `tests/test_session_*.py` |
| Permissions | `tests/test_permissions_manager.py`, `tests/test_permission_results.py` |
| Context / compact | `tests/test_context_*.py` |
| Tools | `tests/test_tools_*.py` or tool-named files |
| Providers | `tests/test_providers_*.py` / adapter tests |

When a design doc and a test disagree, **believe the test**, then fix the doc
in the same PR as the behavior change.

---

## 14. Related Documents

- [ARCHITECTURE.md](ARCHITECTURE.md) — boundaries and dependency rules
- [CLI_TUI_DESIGN.md](CLI_TUI_DESIGN.md) — startup, commands, streaming
- [AGENT_LOOP_GUARDRAILS.md](AGENT_LOOP_GUARDRAILS.md) — stop/pause/continue
- [CONTEXT_MANAGEMENT_DESIGN.md](CONTEXT_MANAGEMENT_DESIGN.md) — facts & compact
- [TOOLS_DESIGN.md](TOOLS_DESIGN.md) / [PERMISSIONS_DESIGN.md](PERMISSIONS_DESIGN.md)
- [PROVIDERS_DESIGN.md](PROVIDERS_DESIGN.md) / [SKILL_SYSTEM_DESIGN.md](SKILL_SYSTEM_DESIGN.md)
- [docs/README.md](README.md) — full index
