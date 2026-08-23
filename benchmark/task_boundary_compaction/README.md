# 任务边界压缩 Benchmark

这个目录只实现基准测试，不修改 `firstcoder/` 的生产控制流、用户全局配置或正常 `.firstcoder/` 会话。每个 `(case, arm, repetition)` 会在输出目录下创建独立的 `project/` 和 `data/` 根目录，结果只保存 JSONL 事件、token/耗时指标、verifier 退出码与输出哈希，不保存 prompt、模型回答正文、工具参数或凭证。

## 三臂定义

| Arm | 隐藏分类器 | `TASK_HASH_CHANGED` 压缩 | 普通 `AUTO` |
| --- | --- | --- | --- |
| `auto_only` | 关闭 | 关闭 | 开启 |
| `classifier_only` | 开启 | 屏蔽 | 开启 |
| `full` | 开启 | 开启 | 开启 |

比较 `full - classifier_only` 得到已经扣除分类器是否存在这一因素后的边界压缩效果；比较 `classifier_only - auto_only` 得到隐藏分类器成本；比较 `full - auto_only` 得到整套功能净效果。

## 运行前准备

在隔离 worktree 中安装开发依赖：

```bash
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

模型配置沿用已有 FirstCoder Model Catalog。不要把任何 API key 写入 manifest、命令历史、结果 JSON 或 README。若需要加载环境变量，可使用 Git 忽略的 `.env.task-boundary-benchmark`；它只能包含 shell 已管理的 provider 环境变量，运行前由 shell 显式加载。

## 先运行无 provider smoke

先执行以下聚焦测试。它使用 fake provider，验证三臂的边界事件、L1 压缩、结果隔离和报告，不产生模型费用：

```bash
.venv/bin/python -m pytest \
  tests/test_task_boundary_compaction_models.py \
  tests/test_task_boundary_compaction_loop.py \
  tests/test_task_boundary_compaction_seed.py \
  tests/test_task_boundary_compaction_provider_observer.py \
  tests/test_task_boundary_compaction_runner.py \
  tests/test_task_boundary_compaction_report.py -q
```

确认生成的 fake-provider `summary.json` 中：正例 full 有一个 `task_hash_changed`，负例没有该事件，`usage_complete` 不缺失，且没有 `confounded_auto` 后，再调用真实模型。

## 受控集：32K 模拟预算试运行

```bash
.venv/bin/python -m benchmark.task_boundary_compaction.runner \
  --suite controlled \
  --model Yuren/gpt-5.6-terra \
  --context-window 32768 \
  --repetitions 1 \
  --max-tool-rounds 6 \
  --max-provider-calls 12 \
  --max-turn-seconds 90 \
  --output benchmark/runs/task-boundary-compaction/pilot-32k

.venv/bin/python -m benchmark.task_boundary_compaction.report \
  --input benchmark/runs/task-boundary-compaction/pilot-32k \
  --output benchmark/runs/task-boundary-compaction/pilot-32k/summary
```

`--context-window 32768` 只传给这次 benchmark 的 `BenchmarkAgentLoop`，是 `simulated_budget_window`，不会写入 `~/.config/firstcoder/config.toml`、项目 `firstcoder.toml` 或正常应用状态。每个受控 trial 都将旧任务 A 注入到当前高水位的 80%，因此运行 B 续接轮时尚未到 AUTO 高水位；如果在预期边界前已经发生 AUTO 压缩，结果会标为 `confounded_auto`，并从因果聚合排除。

每个 B 用户轮固定上限为 6 个工具轮、12 次 provider 调用和 90 秒。三个 arm 使用同一组 benchmark 专属限制；这些值会写入每个 `result.json`，不会改变生产 `AgentLoopLimits` 默认值。达到任一上限的轮会停止并保留结果，避免微型题无限工具循环。

## 历史真实任务：200K 试运行

```bash
.venv/bin/python -m benchmark.task_boundary_compaction.runner \
  --suite historical \
  --model Yuren/gpt-5.6-terra \
  --context-window 200000 \
  --repetitions 1 \
  --max-tool-rounds 6 \
  --max-provider-calls 12 \
  --max-turn-seconds 90 \
  --output benchmark/runs/task-boundary-compaction/real-200k

.venv/bin/python -m benchmark.task_boundary_compaction.report \
  --input benchmark/runs/task-boundary-compaction/real-200k \
  --output benchmark/runs/task-boundary-compaction/real-200k/summary
```

历史集会使用 `git archive <base>` 生成独立项目，再仅以 target commit 覆盖该改动引入的聚焦测试。任务 B 是真实提交，而不是人工编造的 bug；验证器始终运行原始测试，任务 A 只写入隔离的 JSONL 上下文，不会改动 B 工作树。

## 解读与扩样规则

- `summary.json` 同时保留所有原始状态；`confounded_auto`、`invalid_boundary`、`provider_error`、`verifier_error` 和 `usage_incomplete` 不参与 token/延迟聚合。
- `verifier_failed` 是有效的质量结果：保留在通过率与成本统计中，不能当作基础设施异常隐藏。
- 只有当全部 full 正例观察到任务变化、负例无误触发、B verifier 质量不退化、且至少 90% trial 的 usage 完整时，才将两套集扩展到三次重复。
- 只有 full 相对 `auto_only` 没有实质 verifier 通过率退化，并且真实长上下文 case 中 full 的全 provider token 中位数低于 `classifier_only`，才可以称任务边界压缩有收益。32K 与 200K 结果必须并列报告，不能将模拟窗口外推为生产结论。
