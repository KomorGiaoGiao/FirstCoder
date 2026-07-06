# Tools Design

[中文版本](TOOLS_DESIGN.zh-CN.md)

## Overview

The tools layer gives the agent controlled access to the local environment. Tools are registered as concrete local executors, exposed to the model as normalized schemas, and optionally wrapped with permission preflight.

The runtime intentionally separates three concerns:

- model-visible tool definitions
- local execution functions
- program-side permission requirements

## Key Files

- `firstcoder/tools/types.py`: `Tool`, `ToolResult`, `ToolPermissionSpec`
- `firstcoder/tools/registry.py`: base `ToolRegistry`
- `firstcoder/tools/permission_registry.py`: `PermissionAwareToolRegistry`
- `firstcoder/tools/builtin.py`: builtin tool assembly
- `firstcoder/tools/session_registry.py`: session-scoped wrapping and injection
- `firstcoder/tools/descriptions.py`: curated model-facing descriptions
- `firstcoder/utils/introspection.py`: function-to-tool schema generation

Representative tool implementations:

- file tools: `view.py`, `write.py`, `edit.py`, `delete.py`, `apply_patch.py`
- search and inspection tools: `ls.py`, `tree.py`, `glob.py`, `grep.py`, `read_multi.py`
- execution tools: `shell.py`, `python_exec.py`, `diagnostics.py`
- git tools: `git_status.py`, `git_diff.py`, `git_log.py`
- network tools: `fetch.py`, `web_search.py`
- interaction tools: `ask_user.py`, `todo.py`, `think.py`, `task_boundary.py`

## Core Data Model

`Tool` is a concrete dataclass, not a protocol. It contains:

- `definition`: the model-visible `ToolDefinition`
- `executor`: a local callable returning `ToolResult`
- `permission`: optional `ToolPermissionSpec`

`ToolResult` is the normalized execution result returned by all tools:

- `name`
- `ok`
- `content`
- `data`
- `error`

This result is later converted into a tool message for the provider-facing conversation history.

## Registry Model

The base registry in `firstcoder/tools/registry.py` is intentionally simple.

Responsibilities:

- store tool objects by unique name
- expose model-visible `ToolDefinition` values
- dispatch execution by tool name
- convert runtime failures into structured `ToolResult` errors instead of raising through the loop

This keeps the agent loop resilient: unknown tools, bad argument shapes, and executor failures all come back as model-visible tool errors.

## Builtin Tool Assembly

Builtin tools are assembled in `firstcoder/tools/builtin.py` through `create_builtin_registry(...)`.

The base registry is composed in categories:

- readonly filesystem and inspection tools
- optional mutation tools
- optional execution tools
- optional network tools

After tools are created, they are passed through `apply_agent_tool_description(...)` so the model sees curated descriptions rather than raw Python docstrings.

This description rewrite is important: schema generation comes from Python signatures, but the final tool descriptions are tuned for model use.

## Session-Scoped Wrapping

The agent does not expose the raw builtin registry directly. `create_session_tool_registry(...)` in `firstcoder/tools/session_registry.py` performs session-level wrapping.

Current behavior:

- inject the session-scoped `task_boundary` tool if needed
- wrap the registry with `PermissionAwareToolRegistry` when a `PermissionManager` is available

This means the final registry used by the loop is session-aware rather than purely static.

## Permission Coupling

Permissions are not implemented as a global static mapping. Each tool can carry a `ToolPermissionSpec` that describes how to build a `PermissionRequest` from runtime arguments.

`ToolPermissionSpec` supports:

- a fixed `PermissionAction`
- target extraction from an argument name
- a fixed target value
- a custom target builder
- optional cwd extraction
- policy hints such as `allow_always` and `allow_auto`

`PermissionAwareToolRegistry` uses this information to:

1. build a `PermissionRequest`
2. ask the `PermissionManager` for a preflight decision
3. either:
   - allow execution
   - return a denied tool result
   - return a structured confirmation result that pauses the turn

This pause/resume behavior is a core part of the tool execution model.

## Tool Execution Flow

The real flow through the runtime is:

1. provider returns normalized `ToolCall` values
2. agent loop appends the assistant tool-call message to the session log
3. session tool registry performs permission preflight
4. if permission is required, the turn pauses and pending execution is stored
5. otherwise the executor runs locally and returns `ToolResult`
6. the loop appends the final tool result to the session log
7. the next provider call continues from the updated history

The loop also guarantees that every assistant tool call is matched by a tool result, including permission-denied and skipped cases. This keeps provider-visible message sequences valid.

## Special Tools

Some tools are not simple environment adapters:

- `todo` updates a session-visible todo model used by the TUI
- `think` records structured reasoning text without changing the environment
- `task_boundary` is injected per session and participates in task-aware context compaction
- `web_search` is a concrete backend-specific search tool, not a generic abstract search interface

These tools matter because they feed runtime behavior, not just local file or shell execution.

## Design Notes

- Tool schemas are derived from Python functions, but model-facing descriptions are curated afterward.
- Permission logic is attached per tool and enforced by a wrapper, not scattered across executors.
- Tool failures are converted into structured results so the loop can continue safely.
- Session-scoped wrapping lets the runtime inject context-sensitive tools and behaviors without polluting the global registry.
