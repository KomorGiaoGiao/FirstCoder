# Local Pytest Benchmark

[中文版本](LOCAL_PYTEST_BENCHMARK.zh-CN.md)

## What It Measures

This is FirstCoder's fast local coding-agent probe. Each JSONL row becomes a
small clean Git repository; the agent receives a task, edits that repository,
and the runner executes that task's declared pytest command. It measures the
full practical loop—read, edit, test, stop—not just whether a provider returns
plausible text.

It is not a leaderboard substitute for SWE-bench: tasks are small, local, and
the runner does not emulate every real-world repository condition.

## Prerequisites

- install this repository's dependencies and use its virtual environment;
- configure a FirstCoder provider through project configuration/environment;
- have `git` and pytest available to the Python that runs the benchmark;
- use a disposable output directory: the runner creates task repositories.

For example:

```sh
export FIRSTCODER_PROVIDER=openai
export FIRSTCODER_BASE_URL=...
export FIRSTCODER_API_KEY=...
export FIRSTCODER_MODEL=...
```

Do not commit credentials or generated `runs/` artifacts.

## Smallest Real Run

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

If the work directory already contains that task id, rerun with `--force` only
when you deliberately want to discard that generated task repository:

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1 --force
```

The process exits `0` only when every selected task passes; `2` means the run
completed but at least one task's pytest command failed.

## Task Contract

The default dataset is `benchmark/local_pytest/tasks.sample.jsonl`. Each JSONL
row has this shape:

```json
{
  "id": "string_normalize",
  "title": "Normalize Usernames",
  "files": {"src/text_tools.py": "...", "tests/test_text_tools.py": "..."},
  "problem_statement": "Fix src/text_tools.py ...",
  "test_command": "python -m pytest -q"
}
```

`id` becomes the generated directory name. All `files` paths are checked to
stay inside that directory. The runner initializes a Git repository and records
the agent's final diff, including untracked files, so patch evidence remains
available even when tests fail.

## Outputs to Inspect

The JSON summary contains, per task:

| Field | Why inspect it |
| --- | --- |
| `passed`, `returncode`, `pytest_output` | the actual score evidence |
| `repo_path`, `test_command` | reproduce the evaluator locally |
| `transcript_path`, `raw_response` | diagnose model/loop behavior |
| `model_patch` | see what was actually changed |
| `elapsed_seconds` | detect regressions or stalled turns |

Never report “passed” from agent prose alone. Open the summary, rerun the shown
test command in `repo_path`, and inspect `model_patch` when a result surprises
you.

## Useful Options

| Option | Effect |
| --- | --- |
| `--tasks PATH` | use another JSONL task set |
| `--max-tasks N` | select the first N rows |
| `--provider NAME` / `--model-name NAME` | label/configure adapter use |
| `--session-root PATH` | separate benchmark session logs |
| `--force` | recreate existing generated repos |

## Debugging a Failure

1. Read `pytest_output`; distinguish agent failure from an invalid task fixture.
2. `cd` to `repo_path` and run the recorded `test_command` yourself.
3. Inspect `model_patch` and the transcript. Did it edit the correct file, run
   tests, or stop at an approval/tool failure?
4. Rerun one task with a new workdir/session root before changing the agent.
5. Convert the discovered failure into a focused unit/integration test.

## Runner Tests

```sh
.venv/bin/python -m pytest tests/test_local_pytest_benchmark.py tests/test_eval_adapter.py -q
```

Related: [SWE-bench Lite Runbook](SWE_LITE_RUNBOOK.md) for a more realistic
external evaluator.
