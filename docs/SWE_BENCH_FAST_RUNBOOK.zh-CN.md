# SWE-bench-fast Runbook

[English Version](SWE_BENCH_FAST_RUNBOOK.md)

`swe-bench-fast` 是一个对 ARM64 友好的 SWE-bench evaluator。它负责对已有的 prediction JSONL 进行打分，本身不负责生成 patch。

## 本地二进制

当前工作区保留了一份已编译好的本地二进制：

```bash
.tools/swe-bench-fast/swe-bench-fast
```

这份二进制来自 `greynewell/swe-bench-fast`，目标平台是本机使用的 `darwin/arm64`。

## Docker

运行时使用 Colima 的 Docker socket：

```bash
export DOCKER_HOST=unix:///Users/x/.colima/default/docker.sock
```

工作区配置文件是 `swe-bench-fast.toml`，ARM64 registry 目前配置为：

```toml
arm64_registry = "docker.io/greynewell/swe-bench-arm64"
```

## 数据集

尽量使用 `swe-bench-fast` 自带的 ARM64 兼容数据行。当前工作区中有一个两条样例实例的 smoke 文件：

```bash
data/swebench_fast_arm64_two.jsonl
```

## 运行

下面的命令会对两条已有的 FirstCoder predictions 进行评估：

```bash
DOCKER_HOST=unix:///Users/x/.colima/default/docker.sock \
  .tools/swe-bench-fast/swe-bench-fast run \
  --dataset data/swebench_fast_arm64_two.jsonl \
  --predictions runs/firstcoder_swe_lite_two_predictions.jsonl \
  --workers 1 \
  --timeout 900 \
  --run-id firstcoder-fast-two \
  --output runs/swe_bench_fast_two_report.json
```

## 已观察到的 Smoke 结果

在当前这台 Mac 上，第一条 smoke 实例首次运行大约需要 66 秒；镜像缓存好之后，第二次运行大约 10 秒。

两条实例的 smoke 结果：

```text
astropy__astropy-12907  RESOLVED_FULL
astropy__astropy-14182  RESOLVED_NO
```
