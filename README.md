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
  <a href="#configuration"><img alt="OpenAI Compatible" src="https://img.shields.io/badge/OpenAI-Compatible-111827?style=flat-square"></a>
  <a href="#development"><img alt="pytest" src="https://img.shields.io/badge/pytest-tested-0A9EDC?style=flat-square&logo=pytest&logoColor=white"></a>
  <a href="https://deepwiki.com/KomorGiaoGiao/FirstCoder"><img alt="Ask DeepWiki" src="https://img.shields.io/badge/Ask-DeepWiki-0F7BBF?style=flat-square&labelColor=2B2B2B"></a>
</p>

<p align="center">
  English
  · <a href="README.zh-CN.md">简体中文</a>
</p>

---

FirstCoder is a real, runnable local coding agent with a Textual TUI, tool calling, permissions, sessions, and context compaction. It is designed to be useful in daily work and easy to study in code.

If you want to understand how coding agents actually work, FirstCoder keeps the moving parts visible instead of hiding them behind a black box.

- Learn the agent loop, tool calling, permissions, sessions, and context handling.
- Build on a small Python codebase with clear module boundaries.
- Use a local coding agent while still being able to inspect how it works.

![FirstCoder planning, requesting permission, and completing a local task](docs/images/firstcoder-demo.gif)

## Why FirstCoder

Most coding-agent demos show the surface: a prompt goes in, code changes come out. FirstCoder focuses on the machinery in between.

Compared with larger projects like OpenCode, FirstCoder is intentionally smaller in scope.

| Dimension | FirstCoder | Larger projects like OpenCode |
| --- | --- | --- |
| Primary goal | Make agent internals readable and teachable | Deliver a broader production-style coding-agent platform |
| Codebase shape | Roughly 25k lines of Python under `firstcoder/` (174 files) | Roughly 575k lines of TS/JS across a much larger multi-surface codebase |
| Engineering tradeoff | Drops some extra platform surface area to stay inspectable | Accepts more complexity to support a broader product surface |
| Best fit | Learning, modification, interview prep, portfolio projects, and local experimentation | Users who want a larger, more full-surface coding-agent environment |

The goal is not to out-feature a bigger coding agent. The goal is to keep the system real enough to use, but small enough that you can still read it end to end and understand why each subsystem exists.

That also makes FirstCoder a practical repo to study deeply, adapt for your own workflow, and turn into a resume-worthy or portfolio-friendly project after you have extended it.

Compared with more tutorial-first or lightweight learning repos, FirstCoder also tries to stay closer to a small but testable engineering system.

| Dimension | FirstCoder | Many learning-oriented agent repos |
| --- | --- | --- |
| Learning value | Readable subsystem boundaries and explicit docs | Often optimized for a single tutorial path or demo flow |
| Practical surface | Real TUI, tools, permissions, sessions, provider adapters | Often focused on a narrower loop or a simpler proof of concept |
| Verification | Broad pytest coverage plus one Harbor evaluation integration | Often lighter on testing and benchmark integration |
| Extension path | Easier to adapt into a portfolio or resume project | Often better for following along than for long-term extension |

In this repo, the learning goal is important, but it is paired with enough runtime structure, tests, and a Harbor evaluation path to make the project useful after the first read-through.

It is built for people who want to:

- study how a coding agent is assembled
- modify or extend a local Python implementation
- understand the architecture well enough to explain it in an interview

Detailed subsystem design lives in the docs, not in this README.

## Quickstart

Install with `pipx`:

```sh
pipx install firstcoder
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

## What You Get

- Local Python coding agent
- Textual TUI that exposes agent activity instead of hiding it
- Tool calling with highlighted diffs before direct file mutations
- Session persistence, resume flow, and context compaction
- Skills, provider adapters, and clean modules for study and modification

## Configuration

Create a starter config:

```sh
firstcoder config init
firstcoder config path
firstcoder config show
```

Keep secrets in environment variables:

```sh
export FIRSTCODER_API_KEY="your-api-key"
```

Default config locations:

```text
global:  ~/.config/firstcoder/config.toml
project: ./firstcoder.toml
```

Provider support is centered on the OpenAI Chat Completions-compatible path and a native Anthropic Messages API adapter. Both paths implement the same internal complete/streaming contracts (text deltas, tool-call accumulation, forced `tool_choice`, usage, and `PROMPT_TOO_LONG`-style error classification). The TUI's native multimodal input can stage pasted file paths and clipboard images; images and small text files then travel through the session/context pipeline to providers that support vision. Model and provider vision capability still matters. Anthropic-only extras such as prompt caching remain optional future work. FirstCoder does not use the OpenAI Responses API yet.

### Multiple models and request options

You can keep several provider/model profiles in one global or project TOML file. Project values are merged over global values, so a project can override only the fields it needs:

```toml
default_model = "yuren/gpt-5.6-terra"

[providers.yuren]
type = "openai-compatible"
base_url = "https://yurenapi.cn/v1"
api_key_env = "YURENAPI_API_KEY"

[models."yuren/gpt-5.6-terra"]
label = "Yuren Terra"
context_window = 128000

[models."yuren/gpt-5.6-terra".request]
temperature = 0.2
max_tokens = 8192
reasoning_effort = "high"
extra_body = { reasoning_summary = "auto" }
```

Use `firstcoder --model provider/model` to choose the initial profile for a run. In the TUI, `/models` opens the configured model picker and `/model provider/model` switches immediately. `firstcoder config show` prints the configured model references and labels, but never prints API keys, environment-variable values, request bodies, or model-state contents.

`temperature`, `max_tokens`, and `extra_body` are sent with the main model request. `reasoning_effort` is represented as a request extension and passed through when the selected provider supports it; providers that do not recognize it may reject or ignore it. Internal classifiers and compact summarization keep their own bounded token budgets.

> A small detail for observant users: some provider/model combinations give the
> TUI topbar a little more character.

## TUI

FirstCoder's TUI is designed to expose the agent loop instead of hiding it. You can see session state, streamed assistant output, tool calls, tool results, and permission prompts in one place.

Before `write`, `edit`, `apply_patch`, or `delete` changes local files, FirstCoder builds a trusted unified diff with red removals, green additions, per-file statistics, and bounded expansion controls. In standard mode it accompanies the normal permission confirmation; an existing grant or aggressive mode still requires a review-only Apply confirmation. Approve the reviewed operation, deny it, or reply with `reject: <feedback>` so the model can revise the proposed change. FirstCoder rechecks reviewed file snapshots immediately before dispatch and blocks stale operations, reducing accidental overwrites from concurrent changes; this is a guard, not a filesystem-level atomic transaction. In bypass mode the review is shown as a non-blocking event and the operation proceeds immediately; the Harbor adapter disables that event for its non-interactive task turn. Shell commands follow the permission policy for the active mode because arbitrary command effects cannot be precomputed safely.

Ready state:

![FirstCoder ready state](docs/images/firstcoder-ready.png)

Conversation flow:

![FirstCoder conversation flow](docs/images/tui-empty.png)

## Documentation

- [Technical Docs Index](docs/README.md)
- [Chinese Docs Index](docs/README.zh-CN.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Codebase Reading Guide](docs/CODEBASE_READING_GUIDE.md)
- [Multimodal Input Design](docs/MULTIMODAL_INPUT_DESIGN.md)
- [MCP Client Configuration](docs/MCP.md)
- [Harbor Evaluation](benchmark/harbor/README.md)

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

Run a focused test file:

```sh
.venv/bin/python -m pytest tests/test_app_tui.py -q
```

## Philosophy

FirstCoder was built to answer a question most coding agents do not address:

> What actually happens inside when an agent streams, calls tools, asks for
> permission, compacts context, and resumes a session?

It is a real runnable agent, but it is also a readable Python project you can learn from one subsystem at a time.
