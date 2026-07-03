<p align="center">
  <img src="assets/firstcoder-logo.png" alt="FirstCoder logo" width="156">
</p>

<h1 align="center">FirstCoder</h1>

<p align="center">
  <strong>A local Python coding agent built to make agent internals visible.</strong>
</p>

<p align="center">
  <a href="#quickstart"><img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white"></a>
  <a href="#tui"><img alt="Textual TUI" src="https://img.shields.io/badge/Textual-TUI-5B5BD6?style=flat-square"></a>
  <a href="#providers"><img alt="OpenAI compatible" src="https://img.shields.io/badge/OpenAI-Compatible-111827?style=flat-square"></a>
  <a href="#development"><img alt="pytest" src="https://img.shields.io/badge/pytest-tested-0A9EDC?style=flat-square&logo=pytest&logoColor=white"></a>
</p>

<p align="center">
  <a href="#why-firstcoder">Why</a>
  · <a href="#quickstart">Quickstart</a>
  · <a href="#tui">TUI</a>
  · <a href="#task-aware-compaction">Compaction</a>
  · <a href="#commands">Commands</a>
  · <a href="#architecture">Architecture</a>
</p>

---

FirstCoder is a learning-first coding agent. It is not trying to beat mature tools by adding another chat box. It exists to answer a more useful question:

> What actually happens inside a coding agent when it streams, calls tools, asks for permission, compacts context, and resumes a session?

It is a real runnable agent with a Textual TUI, tool calling, permissions, sessions, OpenAI-compatible providers, and a context compaction layer. The code is intentionally organized so you can read one subsystem at a time and explain it in an interview, a portfolio review, or your own study notes.

![FirstCoder TUI](docs/images/tui-chat.png)

> [!NOTE]
> FirstCoder is a learning and portfolio project. It is usable locally, but the goal is clarity and experimentation rather than replacing mature coding agents.

## Why FirstCoder

Most coding-agent demos show the surface: a prompt goes in, code changes come out. FirstCoder focuses on the machinery in between.

| If you want to learn... | Read this part |
| --- | --- |
| How model responses become tool calls | `firstcoder/agent`, `firstcoder/providers` |
| How tools touch files, shell, git, and the network | `firstcoder/tools` |
| How an agent pauses before risky actions | `firstcoder/permissions` |
| How long sessions are stored, compacted, and resumed | `firstcoder/context`, `firstcoder/session` |
| How a terminal UI streams state without hiding the loop | `firstcoder/app` |
| How to evaluate a tiny coding-agent workflow locally | `benchmark/local_pytest` |

The standout experiment is **task-aware context compaction**: FirstCoder does not only compact when the context window gets too full. It can detect semantic task boundaries, generate program-owned task hashes, and use those hashes to decide when old task content should be compressed.

## Quickstart

Install from source:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Start the TUI:

```sh
firstcoder
```

Run one message without opening the TUI:

```sh
firstcoder --message "Summarize this repository in one paragraph"
```

Use line-oriented interactive mode:

```sh
firstcoder --interactive
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
firstcoder
```

## Configuration

Create a starter config:

```sh
firstcoder config init
firstcoder config path
firstcoder config show
```

Default config locations:

```text
global:  ~/.config/firstcoder/config.toml
project: ./firstcoder.toml
```

Example:

```toml
model = "yurenapi/gpt-5.5"

[provider]
type = "openai-compatible"
name = "yurenapi"
base_url = "https://example.com/v1"
api_key_env = "FIRSTCODER_API_KEY"

[permissions]
mode = "ask"

[ui]
theme = "default"
```

Keep secrets in environment variables:

```sh
export FIRSTCODER_API_KEY="your-api-key"
```

Config precedence:

```text
CLI --provider
> environment variables / .env
> project firstcoder.toml
> global ~/.config/firstcoder/config.toml
> defaults
```

## TUI

FirstCoder's TUI is designed to expose the agent loop instead of hiding it. You can see the current session, provider/model, permission mode, activity state, streamed assistant output, tool calls, tool results, and permission prompts.

Empty session:

![FirstCoder empty TUI](docs/images/tui-empty.png)

Tool calls appear in the conversation flow:

![FirstCoder tool calls](docs/images/tui-tools.png)

Permission requests pause the agent until the user decides:

![FirstCoder permission request](docs/images/tui-permission.png)

The activity line is intentionally visible. When the model is thinking, streaming, running a tool, waiting for permission, or reading tool results, the UI should make that state obvious.

## Task-Aware Compaction

Many agents summarize or truncate history when token pressure gets high. FirstCoder also handles token pressure, but its more interesting path is semantic:

```text
user message
  -> model calls task_boundary(decision, basis_message_id)
  -> program generates candidate task_hash
  -> stable window confirms the task switch
  -> TASK_HASH_CHANGED triggers compaction
  -> old task content is micro-compacted
  -> session events preserve the transition for resume
```

The model never invents the hash. It only submits:

```json
{
  "decision": "same | new | uncertain",
  "basis_message_id": "msg_xxx"
}
```

Then the program generates a stable hash from the session id, the basis message id, and the task-boundary strategy version. A stable window prevents one bad model guess from immediately switching tasks.

Why this matters:

- **Less context pollution**: a new task does not have to carry every raw detail from the previous one.
- **Better than pure recency**: old content is compressed because it belongs to a previous task, not merely because it is old.
- **Provider-independent**: task identity lives in runtime state and events, not in one provider's prompt format.
- **Resume-friendly**: task-boundary observations are stored as events, so the active task can be replayed.

This is the part of FirstCoder that is most worth studying if you already understand basic tool calling.

## Core Features

| Feature | What it demonstrates |
| --- | --- |
| Agent loop | Multi-round model calls, tool calls, final answers, and loop limits |
| Streaming | OpenAI-compatible 流式 text, tool-call delta assembly, and basic `reasoning_delta` forwarding |
| Tools | File reading/writing, shell, git, diagnostics, web fetch/search, todo, and user questions |
| Permissions | Local `ALLOW / ASK / DENY` decisions plus long-lived grants |
| Sessions | Append-only JSONL events, catalog, resume, rename, and share/export |
| Context | Checkpoints, archives, task hashes, L1-L4 compaction, and `PROMPT_TOO_LONG` recovery |
| TUI | Markdown rendering, live activity state, tool entries, permission prompts, and slash commands |
| Evaluation | A small local pytest benchmark for checking whether the agent can solve tiny coding tasks |

## Providers

The current mainline is **OpenAI Chat Completions-compatible**. That path supports normal messages, function tools, streaming text, tool-call delta assembly, and a basic `reasoning_delta` event path when compatible providers emit it.

When a provider returns `PROMPT_TOO_LONG`, FirstCoder attempts context compaction and retries the request once.

Common provider environment variables:

| Provider | API key | Model | Default model |
| --- | --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` | `gpt-4.1-mini` |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` | `deepseek-chat` |
| `qwen` | `DASHSCOPE_API_KEY` | `QWEN_MODEL` | `qwen-plus` |
| `moonshot` | `MOONSHOT_API_KEY` | `MOONSHOT_MODEL` | `moonshot-v1-8k` |
| `zhipu` | `ZHIPUAI_API_KEY` | `ZHIPU_MODEL` | `glm-4-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` | `openai/gpt-4.1-mini` |
| `ollama` | `OLLAMA_API_KEY` | `OLLAMA_MODEL` | `qwen2.5-coder:7b` |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` | `claude-sonnet-4-5` |

DeepSeek example:

```sh
export FIRSTCODER_PROVIDER="deepseek"
export DEEPSEEK_API_KEY="your-api-key"
export DEEPSEEK_MODEL="deepseek-chat"
```

Any OpenAI-compatible service:

```sh
export FIRSTCODER_PROVIDER="openai-compatible"
export FIRSTCODER_API_KEY="your-api-key"
export FIRSTCODER_BASE_URL="https://example.com/v1"
export FIRSTCODER_MODEL="your-model"
```

Local Ollama:

```sh
export FIRSTCODER_PROVIDER="ollama"
export OLLAMA_BASE_URL="http://localhost:11434/v1"
export OLLAMA_MODEL="qwen2.5-coder:7b"
```

Anthropic support is experimental / 实验性. It does not yet cover full Anthropic 原生 thinking/cache/streaming behavior. FirstCoder also does not currently claim support for OpenAI Responses API, complete reasoning persistence/display, or multimodal / 多模态 input/output.

## Commands

CLI:

| Command | Description |
| --- | --- |
| `firstcoder` | Start the TUI in an interactive terminal |
| `firstcoder --tui` | Start the Textual TUI explicitly |
| `firstcoder --message "..."` | Run a single user turn |
| `firstcoder --interactive` | Start a line-oriented REPL |
| `firstcoder --project <path>` | Set the project root |
| `firstcoder --data-root <path>` | Set the session/permission data root |
| `firstcoder --session-id <id>` | Create or reuse a session id |
| `firstcoder --provider <name>` | Override the provider |
| `firstcoder --auto-approve` | In REPL mode, answer permission prompts with `allow_once` |
| `firstcoder --max-tool-rounds <n>` | Override the per-turn tool round limit |
| `firstcoder config init` | Create a starter global config |
| `firstcoder config path` | Show config paths |
| `firstcoder config show` | Show effective provider config without secrets |

TUI slash commands:

| Command | Description |
| --- | --- |
| `/sessions` | List session summaries |
| `/session <session_id>` | Show one session |
| `/resume <session_id>` | Resume a session |
| `/share [session_id] [--tool-results]` | Export a Markdown transcript |
| `/rename <title>` | Rename the current session |
| `/context` | Show context status |
| `/compact status` | Show compaction status |
| `/compact` | Manually compact context |
| `/mode` | Show the current permission mode |
| `/mode conservative` | Use the most cautious permission behavior |
| `/mode standard` | Use the default balanced behavior |
| `/mode aggressive` | Allow more common local development actions |
| `/mode bypass` | Bypass policy checks for controlled local experiments |

Planned UX work includes `/help`, `/new`, picker-style `/resume`, grant inspection, and grant revocation.

## Permissions

FirstCoder separates "the model wants to do this" from "the program is allowed to do this."

Permission actions include:

- `read_path`
- `write_path`
- `delete_path`
- `execute_shell`
- `network_request`
- `git_operation`
- `read_env`

Decisions:

```text
ALLOW -> execute immediately
ASK   -> pause and ask the user
DENY  -> block the action
```

Modes:

| Mode | Behavior |
| --- | --- |
| `conservative` | More confirmations, safer defaults |
| `standard` | Balanced default |
| `aggressive` | More willing to run common project-local actions |
| `bypass` | Skip policy checks for controlled experiments |

Long-lived grants are created when the user chooses `allow_always_same_scope`. They are stored under the current data root in `permissions.json`.

## Sessions

FirstCoder stores session facts as append-only JSONL events. Checkpoints and compaction events change the effective context sent to the provider, but they do not replace the underlying event log.

Default data root:

```text
<project-root>/.firstcoder/
```

It stores:

- session event logs
- session catalog data
- context checkpoints and archives
- compaction events
- long-lived permission grants
- exported transcripts

Resume rebuilds state from the event log, including task-boundary observations and active task hash state.

## Architecture

```text
user input
   |
   v
Textual TUI / CLI
   |
   +--> slash commands
   |       sessions / context / compact / permission mode
   |
   +--> AgentChatRunner
           |
           +--> AgentLoop
                   |
                   +--> ChatProvider
                   |       OpenAI-compatible / Anthropic experimental
                   |
                   +--> ToolRegistry
                   |       file / shell / git / web / todo / ask_user
                   |
                   +--> PermissionManager
                   |       allow / ask / deny / grants
                   |
                   +--> ContextWindowManager
                           checkpoint / archive / compact / recovery
```

Project layout:

```text
firstcoder/
  agent/        agent loop, runtime session, user input recovery, loop limits
  app/          Textual TUI, command routing, runtime assembly
  config/       config files, .env, environment variable loading
  context/      event log, context projection, checkpoint, archive, compaction
  eval/         benchmark adapter, patch extraction, prediction generation
  permissions/  policies, grants, project-level permission manager
  providers/    provider abstraction and vendor adapters
  session/      catalog, resume, transcript, share, redaction
  tools/        built-in tools, schemas, results, permission metadata
  utils/        JSON, schema, sandbox, subprocess, git helpers
benchmark/      local pytest benchmark and experiments
docs/           design notes, implementation plans, screenshots
tests/          pytest suite
```

## Local Benchmark

The lightweight local pytest benchmark lives in:

```text
benchmark/local_pytest/
```

It creates tiny Python task repositories, lets FirstCoder modify them, and grades the result with pytest. It is not a leaderboard. It is a local probe for the loop:

```text
read task -> inspect files -> edit code -> run tests -> stop
```

Run a smoke benchmark:

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

See [docs/LOCAL_PYTEST_BENCHMARK.md](docs/LOCAL_PYTEST_BENCHMARK.md) for details.

## Development

Install dev dependencies:

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Run all tests:

```sh
.venv/bin/python -m pytest
```

Run focused tests:

```sh
.venv/bin/python -m pytest tests/test_app_tui.py -q
```

Build a package:

```sh
python -m pip install build
python -m build
```

Test a global install locally:

```sh
pipx install --force .
firstcoder
```

Tests should avoid real API keys and network calls. Provider, tool, context, permission, session, and benchmark behavior should use fakes, fixtures, or temporary directories whenever possible.

## Roadmap

- Better `/help`, `/new`, and picker-style `/resume`.
- Grant listing and revocation commands.
- More polished streaming Markdown in the TUI.
- Stronger agent-loop guardrails around verification, runtime, and tool rounds.
- More benchmark coverage for local coding tasks.
- Continued refinement of task-aware context compaction.
