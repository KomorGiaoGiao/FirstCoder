# Repository Guidelines

## Project Structure & Module Organization

FirstCoder is a Python local coding-agent project. Source code lives in `firstcoder/`, with clear domain modules: `agent/` for orchestration, `app/` for the Textual TUI, `context/` for session context and compaction, `providers/` for model adapters, `tools/` for tool execution, `permissions/` for policy and grants, and `session/` for persistence and resume flows. Tests live in `tests/` and generally mirror the source domains. Design notes and implementation plans are in `docs/`; benchmarks and learning sandboxes are under `benchmark/`.

## Build, Test, and Development Commands

Create and populate the local environment:

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Run the full test suite:

```sh
.venv/bin/python -m pytest
```

Run a focused test file while iterating:

```sh
.venv/bin/python -m pytest tests/test_context_store.py -q
```

Start the app locally:

```sh
.venv/bin/python -m firstcoder
```

## Coding Style & Naming Conventions

Use standard Python style with 4-space indentation, explicit names, and small functions that stay close to their module’s responsibility. Prefer dataclasses or Pydantic models for structured data already represented that way. Keep provider-specific fields inside provider adapters, and keep UI concerns inside `firstcoder/app`. Test files use `test_*.py`; test functions should describe behavior, for example `test_resume_without_id_requests_picker`.

## Testing Guidelines

This repository uses pytest. Add or update tests for behavior changes, especially around agent loops, context recovery, permissions, providers, and tool execution. Run the narrowest relevant tests first, then the full suite for broad or cross-module changes. Avoid tests that depend on real API keys or network access; use fakes and fixtures instead.

## Commit & Pull Request Guidelines

Commit history uses concise imperative messages such as `Add permission confirmation protocol` and `Wire permissions into project tool execution`. Keep commits focused and describe the behavior changed. Pull requests should include a short summary, tests run, linked issues when applicable, and screenshots only for visible TUI changes.

## Security & Configuration Tips

Configure providers through environment variables such as `FIRSTCODER_PROVIDER`, `FIRSTCODER_API_KEY`, `FIRSTCODER_BASE_URL`, and `FIRSTCODER_MODEL`. Do not commit secrets, local session data, virtual environments, or machine-specific configuration.

## Benchmark Evidence and Recovery

Harbor is the repository's maintained external benchmark path. The canonical completed Aider Polyglot result package is `benchmark/runs/harbor/aider-polyglot-feedback-retry-20260726/2026-07-26__12-07-27/`: 225 tasks, 213 passes from 221 explicit rewards (96.38% reward-only pass@1), and 94.67% end-to-end Harbor mean. Read its `README.md` before making claims about the result or rerunning it.

Keep benchmark credentials only in Git-ignored `.env.harbor`; never commit actual API keys, agent session JSONL, task workspaces, or raw provider logs. Treat `reward=0`, `RewardFileNotFoundError`, network/provider errors, and verifier timeouts as separate categories. Aider Polyglot may use `benchmark.harbor.aider_feedback_plugin:AiderFeedbackPlugin` for its permitted verifier-feedback repair turn; do not apply that plugin to benchmarks whose protocol does not permit test-feedback interaction.

When resuming an interrupted Harbor job, first confirm no other `harbor run` or `harbor job resume` process exists. Preserve completed trials, target only the relevant exception types, and inspect `result.json`, verifier output, and the job lock after recovery. A verifier/task-cache change updates the resolved lock; do not silently mix artifacts produced under incompatible task definitions.
