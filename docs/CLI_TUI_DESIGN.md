# CLI and TUI Runtime Design

[中文版本](CLI_TUI_DESIGN.zh-CN.md)

## What This Explains

This document explains how FirstCoder starts, assembles a usable agent session,
and turns runtime events into terminal output. It does not define tool safety or
vendor wire formats; follow the links at the end for those layers.

The important idea is that the terminal UI is a client of a pre-assembled
runtime. It is not the place where providers, permission rules, or session
storage are improvised.

## One Request, From Terminal to Screen

```text
firstcoder [flags]
  -> cli.py chooses TUI, REPL, or one-shot mode
  -> factory.py builds store + provider + tools + permissions + session
  -> AgentChatRunner starts AgentLoop for the current session
  -> stream/tool/input events become TUI transcript/activity updates
  -> durable events remain in <project>/.firstcoder
```

For example, `firstcoder --message "explain loop limits"` uses a synchronous
one-shot path. The Textual application uses `AgentChatRunner`'s asynchronous
streaming path. Both reuse the same session, tool registry, provider types, and
agent loop; their presentation and interruption behavior differ.

## Startup and Dependency Assembly

`firstcoder/app/factory.py:create_firstcoder_app` is the main composition root.
In order, it creates:

1. `JsonlSessionStore` under `<project>/.firstcoder` unless `data_root` is set.
2. `SandboxAccess` and the builtin tools from `create_builtin_registry`.
3. A configured `ChatProvider`.
4. `FilePermissionGrantStore` plus a project `PermissionManager`.
5. `AgentSession`, which creates the session-scoped registry.
6. `ContextWindowManager` with provider-backed L4 summarization.
7. session catalog/new/resume/fork/share services and slash-command handlers.
8. `AgentChatRunner`, `RuntimeModelSwitcher`, and finally `FirstCoderApp`.

This order is meaningful: a session needs concrete tools and a permission
manager; a runner needs both the session and context manager. Tests can pass a
fake provider or a small tool list to this factory without reaching the network.

## Interfaces and State That Cross the Boundary

| Object | Producer | Consumer | Why it matters |
| --- | --- | --- | --- |
| `AppConfig` | `config/settings.py` | factory/provider switcher | resolved configuration, not raw env access everywhere |
| `AgentSession` | factory/session services | runner and command handlers | current durable conversation and tool registry |
| `CurrentSessionState` | `app/runtime.py` | TUI and runner | replaceable pointer when `/new`, `/fork`, or resume changes session |
| `ChatStreamEvent` | provider | runner/TUI | normalized text, reasoning, and tool-call deltas |
| `ToolExecutionEvent` | agent loop | runner/TUI | local work is separate from model streaming |
| `UserInputRequest` | permissions or `ask_user` | interactive UI | explicit pause/resume contract |

## User-Facing Modes

`firstcoder/cli.py` routes these modes:

| Invocation | Intended use | Important limitation |
| --- | --- | --- |
| `firstcoder` or `--tui` | full interactive Textual UI | requires an interactive terminal |
| `--interactive` | line-oriented REPL | less visual than the Textual transcript |
| `--message "..."` | one request for scripts/CI | no long-lived interactive approval dialog |
| stdin with no message | pipe one request | same one-shot constraints |
| `config path/show/init` | inspect or initialize config | does not start an agent turn |
| `--benchmark` | benchmark entry route | benchmark setup has extra requirements |

Common runtime overrides include `--project`, `--data-root`, `--session-id`,
`--provider`, `--auto-approve`, and `--max-tool-rounds`. Read `cli.py` before
adding a flag: configuration precedence is field-specific, not a generic merge
where every CLI option wins.

## What the TUI Actually Renders

`FirstCoderApp` in `app/tui.py` renders a transcript-oriented state model from
`app/tui_state.py`: entries, tool activity, todo items, provider/session state,
and a pending input prompt. It buffers streaming text before flushing it so a
token stream does not cause a widget update per token.

There are two event lanes:

- provider events: reasoning/text/tool-call deltas and final response;
- local events: tool started, finished, skipped, denied, or permission asked.

Keeping them distinct lets the UI say “a shell command is running” even while
the provider has produced no further text. Do not represent a local tool run as
fake assistant prose.

## Commands Change State Through Services

Slash commands are composed in `CompositeCommandHandler`. The notable families
are session (`/new`, `/fork`, `/resume`, `/share`, `/rename`), model (`/model`),
context (`/context`, `/compact`), permission mode (`/mode`), and skills
(`/skills`, `/skill`). A command handler should call the owning service and
then update `CurrentSessionState`; it should not hand-edit JSONL files or TUI
state as a shortcut.

## Hands-On Checks

```sh
.venv/bin/python -m firstcoder --help
.venv/bin/python -m pytest tests/test_cli.py tests/test_app_factory.py tests/test_app_runtime.py -q
```

To inspect startup without credentials, read the fake-provider cases in
`tests/test_app_factory.py`. They demonstrate the actual object graph and prove
that `task_boundary` is session-injected before the first provider request.

## Troubleshooting

| Symptom | First place to inspect |
| --- | --- |
| wrong model/provider shown | resolved config, then `RuntimeModelSwitcher` |
| new session still displays old history | `CurrentSessionState` and session command callback |
| text appears only at the end | provider streaming capability and runner's streaming choice |
| approval cannot continue | pending `UserInputRequest` and `aresume_with_user_input` path |
| command is listed but does nothing | matching command handler and router registration |

## Extension Rules

- Add a presentation behavior in `app/`, not in provider adapters.
- Add reusable runtime construction in the factory, keeping constructors
  injectable for tests.
- Preserve the distinction between stream events and local tool events.
- Add a focused `test_app_*` or `test_cli.py` case before changing visible flow.

Related: [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.md),
[Permissions](PERMISSIONS_DESIGN.md), and [Providers](PROVIDERS_DESIGN.md).
