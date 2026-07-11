# SWE-bench Lite Runbook

[中文版本](SWE_LITE_RUNBOOK.zh-CN.md)

## What This Run Actually Does

SWE-bench Lite evaluation has two independent phases:

```text
prepared instance repositories + issue JSONL
  -> FirstCoder prediction generation
  -> predictions JSONL (patches)
  -> official SWE-bench Docker harness
  -> resolved/not-resolved report
```

The FirstCoder command generates patches. The official harness evaluates those
patches. A generated JSONL is not an evaluation result, and an agent's own test
output is not the official score.

## Before You Start

- Docker must be usable by the harness; expect image downloads, disk use, and
  long wall-clock time.
- Prepare one clean repository per `instance_id` beneath `--repos-root`, at the
  specified base commit. The generator checks cleanliness before the agent runs.
- Use an instances JSONL containing `instance_id`, `repo`, `base_commit`, and
  `problem_statement`.
- Configure the FirstCoder provider and put generated sessions/predictions in a
  disposable `runs/` location.

Start with one instance and one harness worker. Parallel evaluation is expensive
and makes diagnosis harder.

## Generate Predictions

```sh
.venv/bin/python -m firstcoder.eval.swebench \
  --instances data/swebench_lite_one.jsonl \
  --repos-root /tmp/firstcoder-swe-lite/repos \
  --out runs/firstcoder_swe_lite_predictions.jsonl \
  --provider openai \
  --model-name firstcoder \
  --max-instances 1 \
  --print-harness-command
```

The command creates a `CodingTask` per selected instance, requires a clean
repository, runs `FirstCoderCodingAgentAdapter`, and writes one official-format
row for each result:

```json
{"instance_id":"...","model_name_or_path":"firstcoder","model_patch":"diff --git ..."}
```

Before paying for Docker, inspect the file: it should contain the expected ids,
non-empty patches when a change is expected, and no accidental repository path
or credential content.

## Run the Official Harness

Install the official SWE-bench harness in a Docker-capable environment, then
run the emitted command or its equivalent:

```sh
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path runs/firstcoder_swe_lite_predictions.jsonl \
  --max_workers 1 \
  --run_id firstcoder-swe-lite
```

Keep the harness report/logs with the predictions file. A result can fail
because the patch is wrong, the base checkout is wrong, Docker/image setup
failed, or the prediction row did not match the dataset instance. Those are
different failure classes; do not collapse them into “agent failed.”

## Evidence Checklist

For every reported score, retain:

1. source instances JSONL and selected `instance_id`s;
2. commit/base state of each prepared repository;
3. predictions JSONL and agent session transcripts;
4. exact harness command, Docker environment, and logs/report;
5. resolved denominator and exclusions/failures.

This makes a run repeatable and prevents a single lucky resolved case from
becoming an untraceable claim.

## Common Failures

| Symptom | First check |
| --- | --- |
| generator rejects repo | directory name, base commit, and clean Git status |
| no/empty prediction row | agent transcript, provider config, generated diff |
| harness cannot start | Docker daemon/socket, harness installation, disk/image pull |
| prediction ignored | exact `instance_id` and expected official JSONL fields |
| slow or flaky run | use one instance/worker and preserve logs before retrying |

## Code-Level Smoke Tests

```sh
.venv/bin/python -m pytest tests/test_eval_swebench.py tests/test_eval_swebench_smoke.py -q
```

These validate FirstCoder's generation contract, not a live Docker score.
Related: [Local Pytest Benchmark](LOCAL_PYTEST_BENCHMARK.md) and
[SWE-bench-fast Runbook](SWE_BENCH_FAST_RUNBOOK.md).
