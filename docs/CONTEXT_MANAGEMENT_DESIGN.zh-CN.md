# 上下文管理设计

[English Version](CONTEXT_MANAGEMENT_DESIGN.md)

## 概述

FirstCoder 的上下文管理建立在 append-only 的 JSONL session log 和 runtime replay 之上。上下文压缩不会原地修改历史，而是把 compaction 事实、checkpoint 和 task-boundary 观察写成事件，然后从这些事件重建“有效会话视图”。

当前系统明确分成两层：

- 可持久化重建的 `SessionView`
- 仅运行时重建的 `SessionRuntimeState`

## 关键文件

- `firstcoder/context/store.py`：JSONL 持久化和 session view 重建
- `firstcoder/context/writer.py`：结构化事件写入辅助逻辑
- `firstcoder/context/context_builder.py`：provider 可见消息投影
- `firstcoder/context/manager.py`：`ContextWindowManager`
- `firstcoder/context/compaction.py`：确定性的 L1-L3 compaction pipeline
- `firstcoder/context/llm_compact.py`：provider 驱动的 L4 checkpoint 摘要
- `firstcoder/context/checkpoint.py`：checkpoint 模型
- `firstcoder/context/archive.py`：tool result 归档
- `firstcoder/context/triggers.py`：压缩触发逻辑
- `firstcoder/context/runtime_state.py`：运行时状态
- `firstcoder/context/runtime_replay.py`：从 session events 回放运行时状态

## Durable View 与 Runtime State

可持久化回放的会话视图是 `SessionView`，其中包含：

- messages
- checkpoints
- metadata

这是运行时能够从 append-only 日志里稳定重建出来的对话事实。

运行时专属状态是 `SessionRuntimeState`，它会跟踪：

- active task hash
- task-boundary candidate 的稳定窗口状态
- latest checkpoint id
- auto-compaction failure counters
- circuit breaker 状态
- 最近的 compaction 历史

这个分层很重要，因为不是所有运行时事实都适合建模成用户可见消息。

## 上下文投影

agent 不会把原始 JSONL 历史直接发给 provider。`ContextBuilder` 会把重建后的 session state 投影成 provider 可见消息。

checkpoint 改变的是“投影方式”，不是把历史删掉：

- 原始事件历史仍然保留在磁盘上
- provider 侧看到的上下文可能是 checkpoint 摘要 + 边界后的原始 tail

所以 compaction 本质上是“基于持久化事实的投影策略”。

## 压缩层级

当前实现不是一个简单的“截断、摘要、再摘要”流水线。

它分为：

- L1-L3：确定性的程序化压缩
- L4：provider 驱动的 checkpoint 摘要

### L1-L3

这一层在程序化 compaction pipeline 中完成，不依赖模型调用。

当前行为包括：

- 基于任务边界压缩旧任务材料
- 把超大的 tool result 归档到磁盘并替换成 placeholder
- 对冷内容或强制路由内容做按类型压缩，例如 diff、HTML、JSON、search result 等

这是一套“内容感知 + 结构感知”的压缩，而不是通用摘要文本生成。

### L4

L4 由 `LlmCompactService` 执行。

当确定性压缩不够时，它会创建 provider 驱动的 checkpoint 摘要。

关键性质包括：

- 摘要会被写成 checkpoint 事件
- checkpoint 边界会经过校验，保证 tool-call / tool-result 序列仍然合法
- L4 写入 checkpoint 后，运行时会再次从磁盘重建 session view

## 触发模型

触发判断在 `firstcoder/context/triggers.py` 中完成。

当前系统会综合多个启发式条件，包括：

- 估算 token 总量
- 超大的 tool result
- 单轮过多的 tool-result tokens
- tail 消息过多
- tail tokens 过多

`ContextWindowManager` 使用的 trigger 包括：

- `AUTO`
- `TASK_HASH_CHANGED`
- `PROMPT_TOO_LONG`
- `MANUAL`

这些阈值目前主要是具体 token 数值，而不是简单百分比。

## 与任务边界的集成

任务感知压缩依赖一个稳定的 task-boundary 流程。

运行时不会让模型自由发明 task identity，而是：

1. 模型 / 工具只给出结构化 task-boundary signal
2. 运行时计算 candidate hash
3. 通过稳定窗口确认
4. 一旦确认，记录 task-boundary 事件
5. `ContextWindowManager` 在 `TASK_HASH_CHANGED` 下压缩旧任务材料

因此，任务感知压缩是 runtime-owned 的机制，而不是自由的模型行为。

## Fallback 与 Circuit Breaking

如果 L4 失败，manager 可以在 manager 层应用 fallback policy，而不是把所有 retry 细节都藏在 L4 service 里。

当前 fallback 行为包括：

- 先尝试更强的确定性压缩，再考虑重新进入 L4
- 把失败写回 runtime state
- 在连续失败后打开 auto-compaction circuit breaker

这很重要，因为 auto compaction 不应该在每一轮都盲目重复昂贵或已经失效的 L4 尝试。

## 事件模型

与 compaction 相关的事实会被持久化成 session events，而不是只放在易失内存里。

关键事件类型包括：

- `task_boundary_observed`
- `compaction_completed`
- `llm_compaction_completed`
- `checkpoint_created`

这些事件之后会被 replay 到 `SessionView` 和 `SessionRuntimeState` 中。

## 设计说明

- 上下文管理是 event-backed、replay-driven 的，而不是原地修改历史。
- 程序化压缩是常规路径，L4 是昂贵的升级路径。
- checkpoint 改变的是 provider 投影，不是删除原始历史事实。
- 任务感知压缩由 runtime 持有，并在真正影响历史投影前做稳定确认。
