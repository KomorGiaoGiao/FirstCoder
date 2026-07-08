# CLI / TUI Design

[中文版本](CLI_TUI_DESIGN.zh-CN.md)

## Overview

FirstCoder exposes the same agent runtime through three user-facing modes:

- Textual TUI for interactive terminal use
- line-oriented interactive CLI (`--interactive`)
- single-turn CLI (`--message` or stdin)

The entrypoint is `firstcoder/cli.py`. The TUI and CLI share the same core runtime pieces, but they do not use identical control paths. The TUI uses async streaming through `AgentChatRunner`, while the single-turn CLI path is synchronous.

## Key Files

- `firstcoder/cli.py`: top-level CLI parser, mode routing, config commands, REPL, single-turn execution
- `firstcoder/app/factory.py`: assembles the runtime graph for the app
- `firstcoder/app/runtime.py`: `CurrentSessionState` and `AgentChatRunner`
- `firstcoder/app/tui.py`: `FirstCoderApp` Textual application
- `firstcoder/app/tui_state.py`: transcript-oriented TUI state model
- `firstcoder/app/commands.py`: slash command handling contracts
- `firstcoder/app/session_commands.py`: session-related slash commands
- `firstcoder/app/permission_commands.py`: permission mode slash commands
- `firstcoder/app/router.py`: command composition helpers
- `firstcoder/config/settings.py`: config loading and precedence

## Runtime Assembly

There is no `AppFactory` or `RuntimeAssembly` class in the current implementation. Runtime assembly is function-based.

`create_firstcoder_app(...)` in `firstcoder/app/factory.py` builds the main object graph:

1. create the JSONL-backed session store
2. create sandbox access and builtin tool registry
3. create the provider
4. create the permission grant store and project permission manager
5. create or resume `AgentSession`
6. create `CurrentSessionState`
7. create context compaction services
8. create session catalog, resume, and share services
9. create slash command handlers
10. create `AgentChatRunner`
11. create `FirstCoderApp`

This makes the TUI a thin shell around a pre-assembled runtime instead of a place where subsystems are created lazily.

## CLI Modes

`firstcoder/cli.py` currently routes into these modes:

- `config` commands:
  - `firstcoder config path`
  - `firstcoder config show`
  - `firstcoder config init`
- TUI mode:
  - `firstcoder`
  - `firstcoder --tui`
- line REPL:
  - `firstcoder --interactive`
- single-turn execution:
  - `firstcoder --message "..."`
  - stdin when no explicit message is provided
- benchmark mode:
  - `firstcoder --benchmark`

Important runtime override flags include:

- `--project`
- `--data-root`
- `--session-id`
- `--provider`
- `--auto-approve`
- `--max-tool-rounds`

## TUI Structure

The Textual app is implemented mainly inside `FirstCoderApp` in `firstcoder/app/tui.py`.

The UI is built from a small set of concrete widgets rather than a hierarchy of subsystem-specific classes. The main layout includes:

- a top bar for session and provider state
- a scrollable transcript area
- a todo panel
- an activity line
- an input widget

The actual TUI state model is transcript-oriented and lives in `firstcoder/app/tui_state.py`:

- `TuiTranscript`
- `TuiTranscriptEntry`
- `TuiToolActivity`
- `TuiTodoItem`
- `TuiEntryKind`

This is more concrete than the older conceptual `TUIState` model that appeared in previous docs.

## Streaming And User Input

The TUI path uses async streaming methods on `AgentChatRunner`:

- `arun_user_turn(...)`
- `aresume_with_user_input(...)`

Streaming behavior is implemented in `firstcoder/app/tui.py` through app methods that:

- install stream event handlers
- buffer text deltas
- flush the stream periodically into the transcript
- interleave tool activity and final assistant text

When a tool execution pauses for permission or user input, the loop returns a `pending_input` request. The TUI surfaces that request and later resumes the turn with `aresume_with_user_input(...)`.

## Slash Commands

The current TUI command surface is assembled from dedicated handlers and includes:

- `/sessions`
- `/session <session_id>`
- `/new [title]`
- `/fork [title]`
- `/help`
- `/resume <session_id>`
- `/share [session_id] [--tool-results]`
- `/rename <title>`
- `/skills`
- `/skill <name>`
- `/context`
- `/compact status`
- `/compact`
- `/mode`
- `/mode <conservative|standard|aggressive|bypass>`

These are real handlers, not just help text.

## Config Precedence

Config loading is implemented in `firstcoder/config/settings.py`.

The precedence is field-specific rather than a universal “CLI beats everything” rule.

Examples:

- provider selection prefers explicit CLI override, then `FIRSTCODER_PROVIDER`, then project config, then global config, then defaults
- provider credentials and base URL prefer environment variables where mapped
- top-level values like `model` are mostly resolved from project config, then global config, then defaults

This behavior is important because the current code does not implement a single generic merge layer for all CLI flags.

## Design Notes

- TUI and CLI share the same session, provider, tools, permissions, and context machinery.
- The TUI path is more capable than the single-turn CLI path because it supports async streaming, interruption, and permission resumption.
- Runtime assembly is intentionally centralized in factory functions, which keeps tests and alternate entrypoints simpler.
