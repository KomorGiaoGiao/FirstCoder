# SWE-bench Lite Runbook

[English Version](SWE_LITE_RUNBOOK.md)

当前项目对 SWE-bench Lite 的评估分成两个阶段：

1. 用 FirstCoder 生成 `predictions.jsonl`
2. 把这个文件交给官方 SWE-bench Docker harness 进行评估

## 生成 Predictions

先在本地准备好任务仓库目录。每个 repo 目录名都必须和对应的 SWE-bench `instance_id` 一致。

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

输出 JSONL 使用官方 SWE-bench 所要求的字段：

```json
{"instance_id":"...","model_name_or_path":"firstcoder","model_patch":"diff --git ..."}
```

## 评估 Predictions

在一个安装好官方 harness 且 Docker 可用的环境里，运行：

```bash
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path runs/firstcoder_swe_lite_predictions.jsonl \
  --max_workers 1 \
  --run_id firstcoder-swe-lite
```

建议一开始用：

- `--max-instances 1`
- `--max_workers 1`

因为 SWE-bench 的评估通常比较慢，而且比较吃磁盘和 Docker 资源。
