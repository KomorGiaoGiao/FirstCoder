# Codebase Reading Guide

[中文版本](CODEBASE_READING_GUIDE.zh-CN.md)

This is a route through the running system, not a list of folders. Use it when
you need to answer “where should I change this?” without cargo-culting a nearby
file.

## Start With One Real Turn

Trace the request **“find the loop limit and explain it”**:

```text
firstcoder/cli.py
  -> firstcoder/app/factory.py:create_firstcoder_app
  -> firstcoder/app/runtime.py:AgentChatRunner
  -> firstcoder/agent/loop.py:AgentLoop
  -> ContextBuilder builds ChatRequest(messages, tools)
  -> provider.complete/astream
  -> session.tool_registry.execute
  -> append facts to .firstcoder session JSONL
  -> runtime emits UI updates
```

That trace introduces the central contract: the loop coordinates; providers
translate protocols; tools perform local work; the context package persists and
projects facts; the TUI renders events. Do not make provider SDK calls from the
loop or UI mutations from a tool executor.

## The Map

| Area | Read first | Owns | Does not own |
| --- | --- | --- | --- |
| `app/` | `factory.py`, `runtime.py`, `tui.py` | application wiring and terminal presentation | model protocol translation |
| `agent/` | `loop.py`, `session.py`, `loop_limits.py` | one-turn orchestration and pause/resume | concrete shell or HTTP behavior |
| `context/` | `store.py`, `writer.py`, `context_builder.py` | durable facts and model-visible projection | UI widgets |
| `tools/` | `builtin.py`, `registry.py`, `session_registry.py` | schemas, dispatch, session wrappers | final permission policy |
| `permissions/` | `manager.py`, `policy.py`, `grants.py` | allow/ask/deny decisions and grants | executing a tool |
| `providers/` | `types.py`, `factory.py`, adapters | internal-to-vendor protocol conversion | session persistence |
| `skills/` | `discovery.py`, `router.py`, `loader.py` | skill catalog and loading audit | tool registration |
| `session/` | `catalog.py`, `resume.py`, `fork.py` | session discovery and lifecycle services | agent turn semantics |

## A Four-Pass Reading Method

1. **Name the behavior.** Search the visible command, tool name, or error using
   `rg -n "term" firstcoder tests`. Avoid guessing from filenames.
2. **Find the seam.** Follow imports toward the interface (`ChatProvider`,
   `ToolRegistryLike`, or a dataclass) before diving into an implementation.
3. **Follow data, not control flow alone.** For a turn, inspect `ChatRequest`,
   `ChatResponse`, `ToolCall`, `ToolResult`, and session events. These objects
   show what crosses boundaries.
4. **Read the test next.** Tests specify error handling and invariants far more
   precisely than happy-path code. Run the narrow test before editing.

## Useful Entry Commands

```sh
rg -n "class AgentLoop|def create_firstcoder_app" firstcoder tests
rg -n "ChatRequest\(|tool_registry\.execute|preflight\(" firstcoder tests
.venv/bin/python -m pytest tests -q
```

Use `pytest tests`, not a bare repository-wide `pytest`: generated benchmark
runs can contain their own unrelated `tests/` directories.

## First Changes: Choose the Correct Layer

- A new vendor option belongs in `providers/` and configuration, not the loop.
- A new local capability starts as a `Tool` and must declare its permission
  spec; do not add an ad-hoc `if` in `AgentLoop`.
- A different approval rule belongs in `permissions/policy.py` or grants.
- Different history visibility belongs in context projection/compaction, never
  destructive edits to the original event log.
- A new slash command belongs in `app/` command handlers and should not bypass
  session services.

## Common Wrong Turns

**“The system prompt is the whole context.”** No. It is a stable prefix.
`ContextBuilder` separately projects session history and `ChatRequest.tools`
carries tool schemas.

**“Bypass means no safety code exists.”** No. Bypass is a policy mode. Tool
execution still goes through the registry and produces structured results.

**“Compaction deleted history.”** No. the JSONL facts remain; a checkpoint
changes the current provider view.

## A Practical First Exercise

Open `firstcoder/agent/loop_limits.py`, change nothing, and run the relevant
tests found by `rg -n "max_tool_rounds|TOOL_ROUND_LIMIT" tests`. Then trace the
stop reason back into `loop.py`. You will see the project’s intended workflow:
state a small invariant, test it, then change the smallest owning layer.
