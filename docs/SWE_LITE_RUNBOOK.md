# SWE-bench Lite Runbook

This project evaluates FirstCoder on SWE-bench Lite in two phases:

1. Generate `predictions.jsonl` with FirstCoder.
2. Feed that file to the official SWE-bench Docker harness.

## Generate Predictions

Prepare local task repositories under a shared root. Each repo directory name must match the SWE-bench `instance_id`.

```bash
python -m firstcoder.eval.swebench \
  --instances data/swebench_lite_instances.jsonl \
  --repos-root /tmp/firstcoder-swe-lite/repos \
  --out runs/firstcoder_swe_lite_predictions.jsonl \
  --provider openai \
  --model-name firstcoder \
  --max-instances 1 \
  --print-harness-command
```

The output JSONL uses the official SWE-bench fields:

```json
{"instance_id":"...","model_name_or_path":"firstcoder","model_patch":"diff --git ..."}
```

## Evaluate Predictions

Install the official harness in an environment with Docker available, then run:

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path runs/firstcoder_swe_lite_predictions.jsonl \
  --max_workers 1 \
  --run_id firstcoder-swe-lite
```

Start with `--max-instances 1` and `--max_workers 1` because SWE-bench evaluation can be slow and disk-heavy.
