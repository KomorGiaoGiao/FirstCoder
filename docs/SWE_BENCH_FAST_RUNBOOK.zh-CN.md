# SWE-bench-fast 运行手册

[English](SWE_BENCH_FAST_RUNBOOK.md)

## 范围：只负责评测

`swe-bench-fast` 评测已有的 predictions JSONL，**不会**调用 FirstCoder，也不会生成 patch。生成与评测要分开，避免 harness 挂了却被误判成模型生成失败。

```text
FirstCoder 或其他生成器 -> predictions JSONL
ARM64 兼容 dataset + Docker -> swe-bench-fast
-> 每个 instance 状态与 JSON report
```

## 本仓库的本地环境

工作区有一个 macOS ARM64 二进制：

```sh
.tools/swe-bench-fast/swe-bench-fast
```

它由 `greynewell/swe-bench-fast` 构建。大跑前先验证 binary 与 Docker：

```sh
file .tools/swe-bench-fast/swe-bench-fast
docker version
```

本机 Colima 常用 Docker socket：

```sh
export DOCKER_HOST=unix:///Users/x/.colima/default/docker.sock
```

只有它确实是当前机器活跃 socket 才能这样设；不要把这行当作宇宙真理复制到别处。

仓库配置是 `swe-bench-fast.toml`，ARM64 image registry 为 `docker.io/greynewell/swe-bench-arm64`。

## 三类输入必须对得上

运行前确认：

1. dataset 行是 ARM64 兼容的，且含要评分的 `instance_id`（如 `data/swebench_fast_arm64_two.jsonl`）；
2. predictions JSONL 含完全相同 id 和合法 patch；
3. Docker 能拉起配置的 ARM64 镜像。

id 不匹配经常看似“模型失败”，实际是评测输入就不合法。

## 小型 Smoke Eval

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

首跑建议 `--workers 1`，便于排错。只有 smoke 确认正常、并检查过磁盘/CPU/Docker 余量后再加并发；`--timeout` 是本次 evaluator 配置的一部分，报告结果时要一起留下。

## 怎样正确读结果

report 才是证据，至少保存：

- 精确 binary/version 与 `swe-bench-fast.toml`；
- dataset、predictions 的 hash 或不可变副本；
- command、Docker socket/environment、timeout、worker 数；
- 每个 instance 的状态和完整 JSON report；
- image pull/setup 失败与 patch 失败要分开。

过去本机 smoke 的耗时只能作性能背景，不能当当前承诺：此前一个 Astropy instance 冷跑约 66 秒，镜像缓存后约 10 秒，另一个未 resolved。报时延或分数时请重测，别拿老黄历当实时仪表盘。

## 失败分流

| 现象 | 先做什么 |
| --- | --- |
| 连不上 Docker | 确认 Colima/Docker 在跑，socket 和环境变量一致 |
| 镜像拉取失败 | registry 权限、架构、磁盘、retry log |
| 没有 instance 被评 | 精确比 dataset/prediction id |
| patch apply/test 失败 | 保存 report，再查该 prediction/transcript |
| 很慢 | 分开看冷镜像拉取和真实测试执行 |

## 与 FirstCoder 测试的关系

这是外部集成评测。单元测试能证明 adapter 和 prediction 生成格式，不能证明 Docker harness resolved。代码改动先跑 `pytest tests`；要声称 SWE-bench-fast 成绩时，再单独跑本手册。

关联：[SWE-bench Lite 手册](SWE_LITE_RUNBOOK.zh-CN.md)。
