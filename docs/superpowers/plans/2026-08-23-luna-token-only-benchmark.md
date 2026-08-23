# Luna Token-Only Benchmark Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让隔离 benchmark 的隐藏分类器可选地使用 Luna，并在不改生产行为的情况下报告完整 provider token。

**Architecture:** `RunConfig` 增加可选分类模型和测试工厂。未配置时复用主 provider；配置时主模型与分类模型各建一个 provider。L4 继续只使用主 provider，两个记录器的 content-free 指标在写入 `TrialResult` 前合并。

**Tech Stack:** Python 3.12、pytest、FirstCoder Model Catalog、现有 `AgentLoop` classifier injection。

---

### Task 1: 持久化实际分类模型

**Files:**
- Modify: `benchmark/task_boundary_compaction/models.py:139-251`
- Test: `tests/test_task_boundary_compaction_models.py:70-111`

- [ ] **Step 1: 写失败的序列化测试**

在已有 round-trip fixture 中传入 `classifier_model="Yuren/gpt-5.6-luna"`，断言：

```python
encoded = result.to_dict()
assert encoded["classifier_model"] == "Yuren/gpt-5.6-luna"
del encoded["classifier_model"]
assert TrialResult.from_dict(encoded).classifier_model == "Yuren/gpt-5.6-terra"
```

- [ ] **Step 2: 验证 RED**

Run: `.venv/bin/python -m pytest tests/test_task_boundary_compaction_models.py -q`

Expected: `TrialResult` 不接受 `classifier_model` 或 JSON 缺少该字段。

- [ ] **Step 3: 最小实现**

在 `TrialResult` 的默认字段区加入 `classifier_model: str = ""`。`to_dict()` 写入 `self.classifier_model or self.model`；`from_dict()` 使用 `data.get("classifier_model", data["model"])`，让旧产物视为主模型分类。只拒绝非空白但全空格的值。

- [ ] **Step 4: 验证 GREEN 并提交**

Run: `.venv/bin/python -m pytest tests/test_task_boundary_compaction_models.py -q`

Expected: PASS。

```bash
git add benchmark/task_boundary_compaction/models.py tests/test_task_boundary_compaction_models.py
git commit -m "Record benchmark classifier model"
```

### Task 2: 为 benchmark 接入独立 classifier provider

**Files:**
- Modify: `benchmark/task_boundary_compaction/runner.py:47-210,472-516`
- Test: `tests/test_task_boundary_compaction_runner.py:18-76`
- Test: `tests/test_task_boundary_compaction_report.py:20-185`

- [ ] **Step 1: 写失败的 provider 分离测试**

创建 Terra/Luna 两个假 profile 与 provider。给 `RunConfig` 传：

```python
classifier_model="Yuren/gpt-5.6-luna"
classifier_provider_factory=luna_factory
```

断言隐藏的 512-token 请求只出现在 Luna fake，主请求及 L4 保持 Terra fake，结果同时包含 `main` 与 `classifier` metrics，且 `result.classifier_model` 是 Luna。再让 verifier 返回失败，断言 `build_report()` 的 eligible token 仍包含两类 metrics。

- [ ] **Step 2: 验证 RED**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_task_boundary_compaction_runner.py \
  tests/test_task_boundary_compaction_report.py -q
```

Expected: `RunConfig`、CLI 和 runner 都没有分类模型参数。

- [ ] **Step 3: 实现独立解析与注入**

新增：

```python
classifier_model: str | None = None
classifier_provider_factory: ProviderFactory | None = None
```

用私有 `_ResolvedProviders` 返回 `main_provider`、`classifier_provider`、两份 `MainRequestOptions` 与有效分类模型名。真实路径调用 Model Catalog 的 `require()` 两次；未给 `classifier_model` 时只创建一次主 provider。fake 路径同样只有在提供 classifier factory 时才创建第二实例。

在 `run_case()` 中分别包装 provider。若 underlying provider 是同一实例，复用一个 `RecordingProvider`；否则创建两个。对两个不同底层 provider 都应用现有 timeout。构造 loop 时明确传：

```python
provider=main_recording_provider,
classifier_provider=classifier_recording_provider,
request_options=resolved.main_options,
classifier_request_options=resolved.classifier_options,
```

L4 summarizer 仍传 `main_recording_provider`。写 result 前合并唯一 recording wrapper 的 metrics，基于合并结果计算 `usage_complete`，并写入有效 classifier model。

增加 CLI：

```python
parser.add_argument("--classifier-model")
```

并将 `arguments.classifier_model` 交给 `RunConfig`。

- [ ] **Step 4: 验证 GREEN 并提交**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_task_boundary_compaction_models.py \
  tests/test_task_boundary_compaction_runner.py \
  tests/test_task_boundary_compaction_report.py -q
```

Expected: PASS，且全是 fake provider。

```bash
git add benchmark/task_boundary_compaction/runner.py \
  tests/test_task_boundary_compaction_runner.py \
  tests/test_task_boundary_compaction_report.py
git commit -m "Use configured classifier model in benchmark"
```

### Task 3: 固化 token-only 运行与回归

**Files:**
- Modify: `benchmark/task_boundary_compaction/README.md:5-91`

- [ ] **Step 1: 写入实际命令和口径**

记录：

```bash
.venv/bin/python -m benchmark.task_boundary_compaction.runner \
  --suite historical \
  --model Yuren/gpt-5.6-terra \
  --classifier-model Yuren/gpt-5.6-luna \
  --context-window 200000 \
  --repetitions 1 \
  --output benchmark/runs/task-boundary-compaction/token-only-luna
```

说明 `AUTO` 三臂均开启；`full - classifier_only` 是压缩本身的 token 差，`full - auto_only` 包含 Luna 成本；`verifier_failed` 仍计 token，但不作质量解读。

- [ ] **Step 2: 完整无网络回归并提交**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_task_boundary_compaction_models.py \
  tests/test_task_boundary_compaction_loop.py \
  tests/test_task_boundary_compaction_seed.py \
  tests/test_task_boundary_compaction_provider_observer.py \
  tests/test_task_boundary_compaction_runner.py \
  tests/test_task_boundary_compaction_report.py -q
git diff --check
git add benchmark/task_boundary_compaction/README.md
git commit -m "Document Luna token-only benchmark"
```

Expected: 所有测试通过、无 whitespace 错误，且不暂存 `firstcoder/`。

### Task 4: 运行真实 token-only matrix

**Files:**
- Create at runtime only: `benchmark/runs/task-boundary-compaction/token-only-luna/`

- [ ] **Step 1: 保护既有结果**

Run: `test ! -e benchmark/runs/task-boundary-compaction/token-only-luna`

Expected: success；若已有目录，换新 run ID，绝不自动删除。

- [ ] **Step 2: 执行并生成报告**

执行 Task 3 的 runner 命令，然后：

```bash
.venv/bin/python -m benchmark.task_boundary_compaction.report \
  --input benchmark/runs/task-boundary-compaction/token-only-luna \
  --output benchmark/runs/task-boundary-compaction/token-only-luna/summary
```

Expected: 4 个历史 case × 3 个 arm；只保留脱敏结果，无 trial 私有 project/data 目录。

- [ ] **Step 3: 仅报告测量值**

读取 `summary.json` 的三个 paired delta、eligible trial count 和 excluded status count。负差值代表左边的全 provider token 更少；不报告质量或金额收益。
