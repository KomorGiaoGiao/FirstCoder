# SWE-bench-fast Runbook

[中文版本](SWE_BENCH_FAST_RUNBOOK.zh-CN.md)

## Scope: Evaluation Only

`swe-bench-fast` evaluates an existing predictions JSONL. It does **not** call
FirstCoder or generate patches. Keep generation and evaluation separate so an
evaluation failure is not mistaken for a model-generation failure.

```text
FirstCoder or another generator -> predictions JSONL
ARM64-compatible dataset + Docker -> swe-bench-fast
-> per-instance status and JSON report
```

## Local Setup in This Repository

The workspace has a local macOS ARM64 binary:

```sh
.tools/swe-bench-fast/swe-bench-fast
```

It was built from `greynewell/swe-bench-fast`. Verify the binary and Docker
before committing to a larger run:

```sh
file .tools/swe-bench-fast/swe-bench-fast
docker version
```

On this machine Colima commonly exposes Docker through:

```sh
export DOCKER_HOST=unix:///Users/x/.colima/default/docker.sock
```

Use that only when it is the active local Docker socket; do not copy it blindly
to another machine.

The repository configuration is `swe-bench-fast.toml` and sets the ARM64 image
registry to `docker.io/greynewell/swe-bench-arm64`.

## Inputs Must Agree

Before running, validate three things:

1. Dataset rows are ARM64-compatible and have the `instance_id`s you intend to
   score (for example `data/swebench_fast_arm64_two.jsonl`).
2. Predictions JSONL contains exactly those ids and valid patch text.
3. Docker can pull/run the configured ARM64 images.

Mismatched ids often look like a model failure but are actually an invalid
evaluation input.

## Small Smoke Evaluation

```sh
DOCKER_HOST=unix:///Users/x/.colima/default/docker.sock \
  .tools/swe-bench-fast/swe-bench-fast run \
  --dataset data/swebench_fast_arm64_two.jsonl \
  --predictions runs/firstcoder_swe_lite_two_predictions.jsonl \
  --workers 1 \
  --timeout 900 \
  --run-id firstcoder-fast-two \
  --output runs/swe_bench_fast_two_report.json
```

`--workers 1` makes the first run easier to diagnose. Increase concurrency only
after a known-good smoke run and after checking disk, CPU, and Docker capacity.
The `--timeout` is per evaluator configuration; record its value with results.

## Read Results Correctly

The report is the evidence. Preserve:

- exact binary/version and `swe-bench-fast.toml`;
- dataset and predictions file hashes or immutable copies;
- command, Docker socket/environment, timeout, and worker count;
- per-instance statuses and full JSON report;
- image-pull/setup failures separately from patch failures.

Past local smoke observations are useful performance context, not a current
guarantee: one Astropy instance previously resolved in roughly 66 seconds on a
cold run and roughly 10 seconds after image caching, while another did not
resolve. Re-measure when reporting timing or score.

## Failure Triage

| Symptom | First action |
| --- | --- |
| cannot connect to Docker | confirm Colima/Docker is running and socket matches environment |
| image pull failure | registry access, architecture, disk, and retry logs |
| no instance evaluated | compare dataset/prediction ids exactly |
| patch application/test failure | preserve report then inspect that prediction/transcript |
| evaluation is slow | distinguish cold image pull from actual test execution |

## Relationship to FirstCoder Tests

This command is external integration evaluation. Unit tests prove adapter and
prediction generation formats; they do not prove Docker harness resolution.
Run `pytest tests` for code changes, then run this separately when you need an
evidence-backed SWE-bench-fast claim.

Related: [SWE-bench Lite Runbook](SWE_LITE_RUNBOOK.md).
