# SWE-bench Lite 运行手册

[English](SWE_LITE_RUNBOOK.md)

## 这次运行实际做了什么

SWE-bench Lite 有两个彼此独立的阶段：

```text
准备好的 instance 仓库 + issue JSONL
  -> FirstCoder 生成 prediction
  -> predictions JSONL（patch）
  -> 官方 SWE-bench Docker harness
  -> resolved/not-resolved 报告
```

FirstCoder 的命令只生成 patch，官方 harness 才负责判 patch。生成了 JSONL 不等于评测完成；agent 自己跑的测试也不等于官方分数。

## 开始前

- Docker 必须能被 harness 使用；准备下载镜像、占磁盘、耗时间。
- 在 `--repos-root` 下为每个 `instance_id` 准备一个干净仓库，且 checkout 到指定 base commit。生成器会在 agent 跑前检查干净状态。
- instances JSONL 要有 `instance_id`、`repo`、`base_commit`、`problem_statement`。
- 配好 FirstCoder provider，session/prediction 放可丢弃的 `runs/`。

先只跑一个 instance、一个 worker。并发评测又慢又难排，不要上来就召唤赛博炼丹炉。

## 生成 Prediction

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

该命令会为每个选中 instance 创建 `CodingTask`、要求仓库干净、运行 `FirstCoderCodingAgentAdapter`、并写出每项一行的官方格式：

```json
{"instance_id":"...","model_name_or_path":"firstcoder","model_patch":"diff --git ..."}
```

花 Docker 成本前先检查文件：id 是否正确、预期改动时 patch 是否非空、是否意外混进仓库路径或凭证。

## 跑官方 Harness

在可用 Docker 的环境安装官方 SWE-bench harness，然后执行命令输出的 command 或等价命令：

```sh
python -m swebench.harness.run_evaluation \
  --dataset_name princeton-nlp/SWE-bench_Lite \
  --predictions_path runs/firstcoder_swe_lite_predictions.jsonl \
  --max_workers 1 \
  --run_id firstcoder-swe-lite
```

保留 harness report/log 与 predictions 文件。失败可能是 patch 错、base checkout 错、Docker/image 环境挂、prediction row 和 dataset instance 对不上；这是不同的失败类型，别一概扣到“agent 不行”。

## 评分证据清单

每次报告分数至少保留：

1. 输入 instances JSONL 与选中的 `instance_id`；
2. 每个准备仓库的 commit/base 状态；
3. predictions JSONL 与 agent transcript；
4. 精确 harness command、Docker 环境、日志/report；
5. resolved 分母和 exclusions/failures。

这样才可复跑，也避免一个偶然 resolved 被讲成无法追溯的神话。

## 常见失败

| 现象 | 先看 |
| --- | --- |
| generator 拒绝仓库 | 目录名、base commit、Git 是否干净 |
| 没有/空 prediction row | agent transcript、provider 配置、生成 diff |
| harness 起不来 | Docker daemon/socket、harness 安装、磁盘/镜像拉取 |
| prediction 被忽略 | `instance_id` 精确匹配、官方 JSONL 字段 |
| 很慢或不稳定 | 单 instance/worker，保留日志再重试 |

## 代码级 Smoke Test

```sh
.venv/bin/python -m pytest tests/test_eval_swebench.py tests/test_eval_swebench_smoke.py -q
```

它验证 FirstCoder 的生成契约，不代表真实 Docker 分数。
关联：[本地 Pytest 基准](LOCAL_PYTEST_BENCHMARK.zh-CN.md)、[SWE-bench-fast 手册](SWE_BENCH_FAST_RUNBOOK.zh-CN.md)。
