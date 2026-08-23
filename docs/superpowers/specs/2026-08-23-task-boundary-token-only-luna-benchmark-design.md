# 任务边界压缩的 Luna Token-Only Benchmark 设计

## 目的

本轮只观察任务边界压缩对 provider token 消耗的影响，不用 verifier 通过率判断功能质量。实验必须复现计划中的运行配置：主 Agent 使用调用方指定的主模型，隐藏任务边界分类使用独立的 `Yuren/gpt-5.6-luna` 模型，普通 `AUTO` 水位压缩在三个 arm 中都保持开启。

这不是质量收益结论。它只回答：在相同的任务、旧上下文、窗口、限制和主模型下，启用任务边界压缩后，全链路 provider token 是增加还是减少。

## 比较与口径

三臂保持原定义：

| Arm | 分类器 | TASK_HASH_CHANGED 压缩 | AUTO |
| --- | --- | --- | --- |
| `auto_only` | 关闭 | 关闭 | 开启 |
| `classifier_only` | Luna | 关闭 | 开启 |
| `full` | Luna | 开启 | 开启 |

主指标是每个配对 `(case_id, repetition)` 的所有 provider `total_tokens` 差值中位数：

- `full - classifier_only`：两边都有同一 Luna 分类调用，隔离任务边界压缩本身造成的 token 变化。
- `classifier_only - auto_only`：隐藏 Luna 分类器的 token 成本。
- `full - auto_only`：包含分类器成本后的整套净 token 变化。

所有 provider token 都计入：主模型请求、Luna 分类请求、以及 L4 生成请求。报告仍给出三项拆分，以防总量变化掩盖分类器或 L4 的成本。

`verifier_failed` 只作为诊断字段保留，仍计入 token；它不构成 token 聚合排除条件。只有 `invalid_boundary`、`confounded_auto`、`provider_error`、`verifier_error` 或 `usage_incomplete` 排除，因为它们无法代表一次可比较的 token 试验。

## Runner 设计

`RunConfig` 增加可选的 `classifier_model` 与测试用的独立 classifier provider factory。未指定 classifier model 时，保持现有的单 provider fallback，确保旧的 fake-provider 测试和命令兼容。

真实运行时 runner 分别从 Model Catalog 解析主模型与分类模型；两者使用同一已配置 provider profile 的凭证和 Base URL，但发往各自的 `model` 名称。两个 provider 都接受本 trial 的 HTTP 超时。主模型继续供 Agent 与 L4 使用；只有 `AgentLoop.classifier_provider` 使用 Luna，且继续由现有 `TaskBoundaryClassifier` 强制 512 token 输出上限。

两个 provider 都由 `RecordingProvider` 包装。结果写入前合并两者的 metrics；同一 provider 实例被复用时只采集一次，避免双计。`TrialResult` 记录主模型和实际 classifier 模型名称，使报告可以解释 token 来源，但不记录 prompt、响应正文、key、headers 或 SDK 原始对象。

CLI 增加可选 `--classifier-model`。这次运行显式传入 `Yuren/gpt-5.6-luna`；不会读取、写入或修改生产全局配置。

## 验证

先用两个 fake provider 写回归测试，断言：

1. `full` 与 `classifier_only` 的隐藏分类请求通过 classifier provider，而主请求和 L4 仍走主 provider；
2. 合并后的 metrics 包含两个 provider 的 token，且相同实例不会重复；
3. `verifier_failed` 仍出现在 token 中位数与配对差值内；
4. `invalid_boundary` 与 usage 缺失仍排除；
5. CLI 解析并写入有效的 classifier model 元数据。

随后跑现有 benchmark fake-provider 测试。真实 token-only run 只在这些回归通过后执行，使用 `--classifier-model Yuren/gpt-5.6-luna`，并在结果中明确标记为 token-only 观察，而不声称质量收益。

## 非目标

- 不更改 `firstcoder/` 的生产控制流、常规水位、上下文窗口或 `uncertain` 兜底。
- 不修改历史题题面、历史 fixture 或 verifier。
- 不把不同模型的 token 数直接折算为金额；若需要成本结论，另行按 provider 定价计算。
- 不依据本轮 verifier 结果发布质量或产品收益结论。
