# Agent 主循环护栏

[English](AGENT_LOOP_GUARDRAILS.md)

## 目的与边界

`AgentLoop` 是一轮用户任务的事务协调器：记录事实、投影合法 provider 请求、调用模型、执行模型返回的工具、再循环。它不负责解析 OpenAI chunk，也不负责具体 shell 怎么执行。

护栏给这笔事务设上限，避免模型绕圈、provider 太慢、工具过多时无限续杯。它们是 `firstcoder/agent/loop.py` 里的代码检查，不是 system prompt 里喊一句“请不要循环”就能实现的。

## 一轮任务状态机

```text
用户文本 + 可选附件
  -> 暂存附件、写入 user fact -> 构建请求 -> provider 调用
  -> 普通 assistant text ----------------------------> 完成
  -> assistant tool calls -> tool registry 执行
       -> ALLOW/result -> 写 tool result -> provider 调用
       -> DENY         -> 写 denied result -> provider 调用
       -> ASK          -> 保存 pending execution -> 等待用户输入
  -> 用户回答 -> 解析 pending tool -> 继续
```

每条分支都保持一个关键约束：assistant 的 tool call 一定会有配对的 tool result，即使被拒绝或用户拒绝授权。也因此权限确认是“暂停的 turn”，不是把异常直接抛出对话。

## 限制与默认值

`AgentLoopLimits` 是唯一的限制配置入口。

| 字段 | 默认值 | 何时停止 |
| --- | ---: | --- |
| `max_tool_rounds` | 200 | 模型到工具的完成轮次超预算 |
| `max_provider_calls` | 400 | provider 请求超预算 |
| `max_turn_seconds` | 3600 | 单轮单调时钟耗时超预算 |

`swe_lite()` 为 60 轮、100 次调用、1800 秒；`summary()` 为 1、3、120。数值设为 `None` 只代表关闭对应的一个上限，绝不代表关闭权限检查或 tool-result 配对校验。

显式 stop reason 是 `tool_round_limit`、`provider_call_limit`、`turn_timeout`。取消是另一条机制：`CancellationToken`（定义在 `firstcoder.runtime.cancellation`，并由 `agent.cancellation` 再导出）让用户/UI 主动中断，不能假装成某一种 budget 命中。

## 普通工具轮之前发生什么

首条用户消息由程序直接初始化 active task。之后每条消息在可见 agent 请求之前，`TaskBoundaryClassifier` 都会发起一次隐藏的 provider 请求：要求返回锚定当前真实 message ID 的精确 JSON（`same`、`new` 或 `uncertain`），无效或失败最多重试 3 次，仍无有效结果就记录 `uncertain`。程序再把结果经 session 注入的内部控制工具写入既有状态机。这个隐藏请求不转发给 TUI，但会消耗同一用户轮次的 provider 调用次数与 turn 时间预算；benchmark 断言必须把它计入。边界确认后可按 task-switch trigger 压缩 context。

随后 loop 构造稳定 system prefix，并经 `ContextBuilder` 投影会话历史。生成的 `ChatRequest` 有两个独立通道：`messages` 放指令/历史，`tools` 放原生工具定义；工具 JSON Schema 不会再复制到 system message。

## `agent/` 内模块地图

`AgentLoop` 仍是协调器；若干 helper 拆开轮次职责：

| 模块 | 作用 |
| --- | --- |
| `loop.py` | 轮次事务、压缩触发、停止/暂停编排 |
| `loop_limits.py` | 预算与 stop-reason 枚举 |
| `tool_execution.py` | 执行/记录 tool call |
| `tool_flow.py` / `tool_settlement.py` | 批次流控与 settle |
| `todo_policy.py` | 当前任务 Todo 读取与一次性结束对账 |
| `task_boundary_classifier.py` | 任务边界分类辅助 |
| `ports.py` | loop 用的最小 `ContextManagerLike` |

压缩调用点优先用 loop 上的具名 helper（`_auto_compact`、
`_compact_for_prompt_too_long`、`_compact_after_task_hash_changed`），让意图可读。

tools/permissions/utils 共用的 DTO 在 `firstcoder.runtime`，不在 loop 包里。
详见 [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md)。

## 工具调度与质量提醒

`view`、`grep`、`git_diff` 等只读调用在响应允许时可以并发；bypass mode 中还有一份明确的更宽并发名单。写入顺序不会被随手并行。

`todo` 每次提交完整的当前列表，成功更新还会追加 session 范围的 `todo_updated` 事件；`SessionView.todos`、resume、fork 和 TUI 都读取这份持久快照。loop 不再周期性注入合成 user reminder。模型自然准备结束而当前任务仍有未完成 Todo 时，loop 最多发送一次不落盘的临时 system 对账指令。

## 恢复路径

- `ProviderError` 的 prompt-too-long 触发 context 恢复和有界重试，不能对同一个超长请求原地打转。
- 畸形/未知 tool call 变成结构化 `ToolResult` error。
- 权限 `ASK` 生成 `PendingPermissionExecution`；用户回答后恢复原始调用。
- 写前 review 会在真正调度前重验文件快照；快照已变化时阻止执行，模型必须重新生成 diff。
- 取消通过 runner/UI 边界报告。

## 最小验证证据

```sh
.venv/bin/python -m pytest \
  tests/test_agent_loop_limits.py tests/test_agent_context_loop.py \
  tests/test_agent_tool_flow.py tests/test_context_system_prompt.py \
  tests/test_multimodal_input.py tests/test_prewrite_review.py -q
```

改之前先定位你要动的断言：

```sh
rg -n "TOOL_ROUND_LIMIT|max_provider_calls|prompt too long|PendingPermission" tests firstcoder
```

## 常见误解

**“200 就是最多 200 个 tool call。”** 不完全是，它限制的是 tool round；同一轮可能有多个符合条件的并发只读调用。

**“测试成功就必定立即结束。”** 不是。验证输出只是回给模型的证据，不会移除工具权限，也不会强制提前生成最终回答。

**“bypass 把 wrapper 删了。”** 没有。它改的是 policy decision；session registry、事件记录、结构化结果、loop limit 都还在。

**“每次可见回复前都是模型调用 `task_boundary`。”** 不是。首个任务由 loop 初始化；后续轮次先跑不可见 classifier 请求，再由程序通过内部控制工具记录决策。主模型既看不到也不能执行该工具。

## 安全改法

护栏配置改 `loop_limits.py`，执行改 `loop.py`，并加一条同时断言 stop reason 与对话形状的测试。别把隐藏 timer 塞到 provider adapter：限制属于用户 turn 语义，应归协调器所有。

关联：[架构说明](ARCHITECTURE.zh-CN.md)、[工具设计](TOOLS_DESIGN.zh-CN.md)、[权限设计](PERMISSIONS_DESIGN.zh-CN.md)、[上下文管理](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)。
