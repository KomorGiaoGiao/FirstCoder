# Agent Loop Guardrails

[English Version](AGENT_LOOP_GUARDRAILS.md)

## 概述

agent loop guardrails 用来限制“单条用户消息”最多能跑多远。当前实现中，这些限制由 `AgentLoopLimits` 建模，并直接在 loop 内部执行。

当前护栏主要关注三个边界：

- 最大工具轮数
- 最大 provider 调用次数
- 单轮消息的最长运行时间

它们比旧的“抽象安全预算”模型更窄，也更贴近当前真实实现。

## 关键文件

- `firstcoder/agent/loop_limits.py`：限制字段和停止原因
- `firstcoder/agent/loop.py`：loop 中的实际执行与检查
- `firstcoder/agent/cancellation.py`：取消 token 支持
- `firstcoder/agent/verification.py`：验证成功后的提前结束逻辑

## 限制模型

当前 `AgentLoopLimits` 包含：

- `max_tool_rounds`
- `max_provider_calls`
- `max_turn_seconds`
- `successful_verification_stop`

当前代码里并没有 `Age` 这样的耗时跟踪结构，也没有独立的“最大验证次数”或“工具总耗时上限”字段。

`AgentLoopStopReason` 当前包括：

- `tool_round_limit`
- `provider_call_limit`
- `turn_timeout`

## 默认值

当前默认值相对宽松：

- `max_tool_rounds = 200`
- `max_provider_calls = 400`
- `max_turn_seconds = 3600`
- `successful_verification_stop = True`

此外还提供了更窄的预设：

- `default()`
- `swe_lite()`
- `summary()`

这些不是文档概念，而是当前真实存在的运行时 preset。

## 在 Loop 中的执行位置

`AgentLoop` 拥有当前真实的主控制流。

一轮消息里，loop 当前会完成：

1. 追加用户消息
2. 必要时先做 context compaction
3. 构造 provider 可见消息
4. 调用 provider
5. 追加 assistant 输出或 assistant tool calls
6. 执行工具
7. 追加 tool results
8. 必要时再次 compact
9. 重复，直到回合结束、等待用户输入或命中护栏

所以这些 guardrails 不是一个独立监督进程，而是 loop 内部直接检查的边界。

## 与其他运行时行为的关系

有几个运行时行为会直接影响 guardrails 的实际效果：

- provider 返回 prompt-too-long 时，loop 会进入 compaction + retry 路径
- 某些只读工具可以并行执行
- 验证成功后可以提前结束 tool looping，并要求模型给出最终回答
- 权限确认可以暂停当前回合，稍后恢复，同时保持 tool-call / tool-result 序列合法

这些不是独立的 guardrail 子系统，但都会影响一轮消息何时继续、何时停止。

## 取消机制

取消机制和 limit 系统并列存在。它不是 `AgentLoopStopReason` 里的计数项，但仍然属于运行时边界的一部分。

loop 使用 cancellation 支持来中断：

- 长时间运行的工具执行
- 流式回合
- 恢复后的继续执行

这让运行时既有自动停止条件，也有用户主动中断能力。

## 设计说明

- 当前 guardrail 系统是 loop-centric 的，而不是 supervisor-centric 的。
- 真实实现比早期概念文档更简单：provider 调用次数比抽象验证预算更重要。
- 验证仍然重要，但目前它体现为“验证成功后提前停止”，而不是“最多验证 N 次”。
- prompt-too-long 恢复逻辑也属于这套实际安全边界的一部分，因为它能避免一轮消息在超窗请求上反复失败。
