# Tools Design

[中文版本](TOOLS_DESIGN.zh-CN.md)

## Problem and Non-Goals

Tools let a model request local actions without giving provider code direct
filesystem or shell access. This layer owns three things: a model-visible
definition, a local executor, and optional permission metadata. It does not
decide policy; the permission wrapper does that.

## End-to-End Example: `view` a File

```text
create_builtin_registry(project_root)
  -> Tool(definition, executor, permission spec)
  -> create_session_tool_registry injects task_boundary/retrieve_archive
  -> PermissionAwareToolRegistry wraps dispatch
  -> AgentLoop puts registry.definitions() into ChatRequest.tools
  -> provider returns ToolCall(name="view", arguments=...)
  -> registry executes/preflights -> ToolResult
  -> AgentLoop appends role=tool result and asks the provider again
```

The JSON Schema travels as `ChatRequest.tools`; provider adapters turn it into
their native `tools` representation. The schema is not appended to the system
prompt, so it is neither duplicated conversation text nor a security boundary.

## Core Contract

`tools/types.py` defines concrete dataclasses:

| Type | Meaning |
| --- | --- |
| `ToolDefinition` | name, description, JSON-Schema-like parameters visible to the model |
| `Tool` | definition + local executor + optional `ToolPermissionSpec` |
| `ToolResult` | normalized `name`, `ok`, `content`, `data`, and `error` |
| `ToolPermissionSpec` | how to derive a permission request from concrete arguments |

An executor returns `ToolResult` rather than leaking exceptions into the agent
loop. Consequently unknown names, invalid arguments, and executor failures can
be returned to the model as a structured tool message and the session stays
replayable.

## Building and Wrapping a Registry

`create_builtin_registry` in `tools/builtin.py` assembles groups: inspection,
mutation, execution, network, git, and interaction tools. Function signatures
become a baseline schema through `utils/introspection.py`; curated descriptions
are then applied so tool instructions are useful to a model rather than raw
Python docstrings.

The raw builtins are never the whole runtime registry. `create_session_tool_registry`
adds `task_boundary`, optionally `retrieve_archive`, and wraps the base
`ToolRegistry` in `PermissionAwareToolRegistry` whenever a manager exists.
Session injection is essential because those tools need session state and must
not be globally stateless.

## Execution Rules

`ToolRegistry.execute(name, arguments)` resolves exactly one name and
normalizes failures. `PermissionAwareToolRegistry.execute` first derives a
`PermissionRequest` from the tool's spec, then obtains an allow/ask/deny
decision. `ASK` returns a structured signal instead of executing; `AgentLoop`
stores the original call and resumes it after user input.

The agent loop guarantees legal conversation ordering:

```text
assistant(tool_call id=call_1) -> tool(tool_call_id=call_1)
```

Denied, skipped, and failed calls still receive the second message. Never
invent a tool result in UI code or remove one during context compaction.

## Special Tools

- `todo` persists the visible plan protocol used by the UI.
- `think` records internal structured reasoning without mutating the workspace.
- `task_boundary` reports whether a user message begins a task; hashes are
  generated program-side, not accepted from the model.
- `retrieve_archive` reads bounded archived output only from the current
  session.
- `web_search` prefers Parallel MCP and may fall back to Exa when configured.

These are runtime participants, not merely convenience commands. Treat a
change to their output schema as a compatibility change for context and tests.

## Add a Tool Safely

1. Write a small executor returning a truthful `ToolResult`.
2. Derive/validate its schema and give it a curated description.
3. Declare `ToolPermissionSpec` at registration time when it touches local or
   network resources.
4. Add it to the correct builtin group; do not add loop-only special cases.
5. Test success, invalid arguments, denied/ask behavior, and provider-visible
   sequence when relevant.

```sh
.venv/bin/python -m pytest tests/test_tools.py tests/test_schema.py \
  tests/test_introspection.py tests/test_permission_registry.py -q
```

## Failure Diagnosis

| Observation | Likely owner |
| --- | --- |
| tool not offered to model | builtin/session registry or provider capabilities |
| model sees wrong parameters | schema generation or curated definition |
| executor ran without expected confirmation | permission spec/wrapper/policy |
| provider rejects history after a call | missing/mismatched tool result id |
| tool works in a unit test but not a session | session-scoped registry assembly |

Related: [Permissions](PERMISSIONS_DESIGN.md) and [Providers](PROVIDERS_DESIGN.md).
