# Context 上下文管理与压缩架构说明

本文档说明 `firstcoder/context` 当前这一层为什么存在、解决什么问题、内部怎么分层，以及它和 session、agent loop、provider 请求之间的关系。

当前 `context` 不只是“压缩文本”的工具包。它已经是 FirstCoder 的 **会话事实存储、上下文投影、上下文压缩、checkpoint resume 边界** 的底层模块。

## 一句话概括

FirstCoder 的 context 层用 append-only 事件日志保存完整会话事实，再根据 checkpoint、任务边界、工具输出规模和 token 预算，把完整会话投影成一份适合发给模型的短上下文。

```text
完整 session event log
  -> 重放成 SessionView
  -> 根据 checkpoint 得到 effective context
  -> L1-L4 分层压缩
  -> 写回压缩事件和 checkpoint
  -> 下一轮 ContextBuilder 投影给 provider
```

## 设计背景

coding agent 的上下文管理不能只做“截断最后 N 条消息”。

原因：

- 工具输出可能非常大，例如 grep、git diff、pytest、shell log。
- tool calling 有严格序列要求，不能留下孤立 tool result。
- 用户可能频繁切换任务，旧任务上下文会污染新任务。
- 会话需要 resume，压缩不能破坏可恢复性。
- 后续需要只读分享 transcript，原始会话事实不能随便丢。
- 不同 provider 的上下文窗口不同，压缩策略需要与 provider 解耦。

所以当前方案是分层压缩，而不是单点策略：

- L1：轻量压缩旧任务内容。
- L2：归档特别大的工具结果，在上下文中放 placeholder。
- L3：按内容类型路由压缩冷内容。
- L4：当程序化压缩不够时，用 LLM 生成 checkpoint summary。
- L5：手动 compact 入口，方便用户观察和干预。

## 架构边界

`context` 负责：

- 保存 session 事件。
- 重放 session 事件为 `SessionView`。
- 保存和选择 checkpoint。
- 判断是否需要压缩。
- 执行 L1-L4 压缩。
- 归档大工具输出。
- 验证 tool call 序列。
- 构造 provider messages。
- 提供 `/context`、`/compact status` 需要的 inspection 信息。

`context` 不负责：

- 调用主模型完成普通对话。
- 执行工具。
- 展示 TUI。
- 管理 resume 列表。
- 生成只读分享 transcript。

建议边界：

```text
firstcoder/agent
  单个运行期 AgentSession 和一轮 agent loop

firstcoder/context
  session event log、context projection、compaction、checkpoint、archive

firstcoder/session
  resume catalog、session metadata、只读 transcript share、redaction

firstcoder/app
  Textual UI 与命令入口
```

## 文件树

```text
firstcoder/context
├── ARCHITECTURE.md
├── __init__.py
├── archive.py              # L2：大工具结果归档和 placeholder
├── checkpoint.py           # L4：checkpoint 数据结构和 latest 选择
├── compaction.py           # L1-L3 程序化压缩 pipeline
├── context_builder.py      # SessionView -> provider ChatMessage 投影
├── events.py               # append-only session event 模型
├── fallback.py             # L4 失败后的 fallback 策略
├── identity.py             # id、fingerprint、hash helper
├── inspector.py            # /context、/compact status 只读报告
├── llm_compact.py          # L4：LLM summary compact 和 checkpoint 写入
├── manager.py              # 压缩触发与 L1-L4 编排入口
├── models.py               # AgentMessage、MessagePart、SessionView
├── retry_policy.py         # L4 retry 策略
├── runtime_replay.py       # 从事件日志恢复 runtime state
├── runtime_state.py        # task hash、compact 熔断、checkpoint 状态
├── store.py                # JSONL session store
├── system_prompt.py        # stable system prefix 构造与缓存
├── task_boundary.py        # 任务边界观察和 task hash 切换
├── token_budget.py         # token 估算和阈值预算
├── tool_result.py          # 工具结果转上下文 part 的标准化
├── tool_sequence.py        # tool_call/tool_result 序列校验
├── triggers.py             # auto compact 触发条件
├── versions.py             # context/checkpoint/compaction schema version
├── writer.py               # session event 写入门面
└── content
    ├── __init__.py
    ├── build.py            # build/test 输出压缩
    ├── code.py             # 源码压缩
    ├── compressors.py      # 通用文本压缩和 L1 old task compact
    ├── detector.py         # L1-L3 part eligibility 判断
    ├── diff.py             # git diff 压缩
    ├── html.py             # HTML 压缩
    ├── json.py             # JSON 压缩
    ├── router.py           # L3 内容类型检测和路由
    └── search.py           # grep/search result 压缩
```

## 三层核心结构

当前 context 可以理解成三层。

### 1. 会话事实层

核心文件：

- `events.py`
- `store.py`
- `writer.py`
- `models.py`
- `runtime_replay.py`
- `runtime_state.py`

这一层回答：

> 一个 session 里到底发生过什么？如何 resume？

`JsonlSessionStore` 把所有可恢复事实写入：

```text
.firstcoder/sessions/{session_id}.jsonl
```

每一行是一个 `SessionEvent`：

```python
SessionEvent(
    id=...,
    session_id=...,
    type=...,
    payload=...,
    created_at=...,
)
```

常见事件：

- `session_created`
- `user_message`
- `assistant_message`
- `tool_result`
- `checkpoint_created`
- `compaction_completed`
- `llm_compaction_completed`
- `task_boundary_observed`

`rebuild_session_view(session_id)` 会把完整事件日志重放成：

```python
SessionView(
    session_id=...,
    messages=[...],
    checkpoints=[...],
    metadata={...},
)
```

重要设计：

- 原始事件不删除。
- 压缩通过追加事件表达。
- `compaction_completed` 中的 replacements 会在 rebuild 时应用到视图。
- resume 读取完整 event log，不是只从 checkpoint 后读取。

### 2. 上下文投影层

核心文件：

- `context_builder.py`
- `checkpoint.py`
- `tool_sequence.py`
- `system_prompt.py`

这一层回答：

> 当前 session 应该怎样变成 provider 请求 messages？

内部 `SessionView` 不是 provider 请求格式。`ContextBuilder` 负责投影：

```text
SessionView
  -> 选择 latest checkpoint
  -> 插入 checkpoint summary
  -> 取 tail_start_message_id 之后的 tail messages
  -> 校验 tool_call/tool_result 序列
  -> 转成 ChatMessage
```

如果没有 checkpoint：

```text
provider context = system prefix + 全量 messages
```

如果有 checkpoint：

```text
provider context = system prefix + checkpoint summary + tail messages
```

checkpoint 的职责是记录：

- 旧历史摘要。
- 最近 tail 从哪条 message 开始。
- 哪条 message 之前已经被 summary 覆盖。
- source fingerprint。

checkpoint 不负责：

- 保存完整会话。
- 删除旧历史。
- 直接 resume。
- 自动移动边界。

### 3. 压缩执行层

核心文件：

- `triggers.py`
- `manager.py`
- `compaction.py`
- `archive.py`
- `content/*`
- `llm_compact.py`
- `fallback.py`
- `retry_policy.py`

这一层回答：

> 当前上下文太大时，应该怎么缩短？

`ContextWindowManager` 是压缩统一入口。它不实现具体压缩算法，只负责编排：

```text
触发判断
  -> L1-L3 程序化压缩
  -> 写 compaction_completed
  -> 重新估算 token
  -> 如果还超预算，进入 L4
  -> 写 checkpoint_created 和 llm_compaction_completed
  -> rebuild SessionView
```

## 完整数据流

一轮 agent 对话中的 context 数据流：

```text
用户输入
  |
  v
AgentSession.append_user_message()
  |
  v
SessionEventWriter -> user_message event
  |
  v
AgentLoop 准备调用模型
  |
  v
SessionStore.rebuild_session_view()
  |
  v
ContextBuilder.build_provider_messages()
  |
  v
provider.chat(messages, tools)
  |
  v
AgentSession.append_assistant_response()
  |
  v
如有 tool call，执行工具并写 tool_result event
  |
  v
ContextWindowManager.compact_if_needed()
  |
  v
必要时追加 compaction/checkpoint 事件
```

压缩不是构造 prompt 时偷偷发生，而是在 manager 中显式发生，并写回事件日志。

## 触发逻辑

当前上下文压缩的触发逻辑可以总结为：

> 两套判断入口，一个统一执行入口。

两套判断入口：

- 上下文压力判断：判断 effective context 是否因为 token、tail 或工具输出过大需要压缩。
- 任务边界判断：判断 active task hash 是否已经确认切换，需要整理旧任务上下文。

统一执行入口：

```python
ContextWindowManager.compact_if_needed(...)
```

所有触发源最后都会进入 `ContextWindowManager`，由它执行同一套 L1-L4 pipeline。

### 触发源总览

| 触发源 | 谁判断 | 是否经过 `triggers.py` 阈值判断 | 是否强制执行 |
| --- | --- | --- | --- |
| `AUTO` | `triggers.py` | 是 | 否 |
| `TASK_HASH_CHANGED` | `task_boundary` 工具 + `AgentLoop` | 否 | 是 |
| `MANUAL` | TUI 命令 | 否 | 是 |
| `PROMPT_TOO_LONG` | provider 错误恢复路径 | 否 | 是 |

这里最容易混淆的是：`triggers.py` 不是所有触发的总入口。它只负责 `AUTO` 自动压缩里的上下文压力判断。

### 统一执行入口

四类触发源最终都会调用：

```python
ContextWindowManager.compact_if_needed(
    ContextCompactRequest(
        view=...,
        runtime_state=...,
        trigger=...,
        mode=...,
        current_turn=...,
    )
)
```

manager 收到请求后做两件事：

- 判断这个触发源是否真的应该执行压缩。
- 如果要执行，就按 L1-L4 顺序跑压缩，并把事件写回 session log。

`ContextWindowManager._should_compact()` 当前规则：

```text
AUTO:
  只有 evaluate_context_triggers().should_compact 为 true 才执行

TASK_HASH_CHANGED:
  强制执行

MANUAL:
  强制执行

PROMPT_TOO_LONG:
  强制执行
```

## 第一套判断：上下文压力触发

上下文压力触发只服务 `AUTO`。

入口是 `triggers.py` 里的：

```python
evaluate_context_triggers(view, config)
```

它只回答一个问题：

> 当前 effective context 是否因为体积压力需要自动 compact？

它判断：

- `token_threshold`：估算 token 达到自动压缩阈值。
- `large_tool_result`：tail 中存在过大的 tool result。
- `turn_tool_results`：单轮工具结果过大。
- `tail_message_count`：tail message 数过多。
- `tail_token_count`：tail token 数过多。

它的判断范围是 effective context：

```text
无 checkpoint:
  全量 messages

有 checkpoint:
  checkpoint summary + checkpoint tail messages
```

这样设计是为了让触发判断和真正发给模型的上下文范围一致。已经被 checkpoint summary 覆盖的旧 raw messages，不应该继续让自动压缩误判为超阈值。

`AUTO` 流程：

```text
一轮 agent 写完 user/assistant/tool 事件
  -> AgentLoop 调 compact_if_needed(trigger=AUTO)
  -> manager 调 evaluate_context_triggers()
  -> should_compact=false: skipped
  -> should_compact=true: 执行 L1-L4
```

`AUTO` 还受自动压缩熔断影响：

```text
L4 或自动压缩连续失败
  -> runtime_state.auto_compact_disabled_until 被设置
  -> 后续 AUTO 触发返回 skipped(circuit_open)
```

## 第二套判断：任务边界触发

任务边界触发只服务 `TASK_HASH_CHANGED`。

它不在 `triggers.py` 里判断，因为它不是“上下文是否太大”的问题，而是“用户是否切换到新任务”的运行期语义判断。

流程：

```text
assistant 调用 task_boundary 工具
  -> 模型只提交 decision 和 basis_message_id
  -> task_boundary 工具在程序侧生成 candidate task hash
  -> 稳定窗口确认 active_task_hash 切换
  -> 工具返回 should_trigger_compaction=true
  -> AgentLoop 调 compact_if_needed(trigger=TASK_HASH_CHANGED)
  -> manager 强制执行 L1-L4
```

`TASK_HASH_CHANGED` 不看 `evaluate_context_triggers().should_compact`。

原因：

- 新任务开始时，即使 token 没超阈值，旧任务上下文也可能污染新任务。
- L1 需要尽快压缩 `task_hash != active_task_hash` 的旧任务内容。
- L3 后续也依赖 active task hash 判断当前任务中的冷内容。

所以任务边界触发是语义触发，不是体积触发。

## 任务哈希与结构化输出约束

任务哈希不是模型自由生成的。

当前设计刻意把模型输出限制成一个很小的结构化协议：

```json
{
  "decision": "same | new | uncertain",
  "basis_message_id": "msg_xxx"
}
```

这个协议由 `task_boundary` 工具的手写 `ToolDefinition` 约束：

- `decision` 是 enum，只能是 `same`、`new`、`uncertain`。
- `basis_message_id` 必须是字符串。
- required 字段只有 `decision` 和 `basis_message_id`。
- 工具明确拒绝 `task_hash` 或 `hash` 这类模型传入字段。

设计原因：

- 不让模型发明 hash，避免不同模型输出格式不稳定。
- 不让模型决定最终任务 ID，避免 prompt 里出现伪造或漂移的 task hash。
- 用 enum 限制模型判断空间，减少自由文本解析。
- 用 `basis_message_id` 把“为什么认为是新任务”锚定到当前 session 的真实消息。

程序侧生成 task hash：

```text
candidate_hash = stable_json_hash({
  session_id,
  basis_message_id,
  TASK_BOUNDARY_TOOL_VERSION
})
```

最终格式类似：

```text
task_<digest>
```

### 稳定窗口

任务切换不是模型说一次 `new` 就一定生效。

默认策略：

```text
第一次 new:
  生成 candidate_task_hash
  stable_count=1
  confirmed_change=false
  should_trigger_compaction=false

第二次相同 basis_message_id 的 new:
  candidate hash 稳定
  active_task_hash 切换
  confirmed_change=true
  should_trigger_compaction=true
```

如果模型返回 `same` 或 `uncertain`：

```text
清空 candidate_task_hash
stable_count=0
不触发 compact
```

这样做是为了防止任务边界抖动。模型偶尔误判一次 `new`，不会立刻导致 active task hash 切换，也不会立刻触发旧任务压缩。

### 单次确认策略

部分明确场景可以允许单次确认。

`TaskBoundaryPolicy.single_observation_basis_message_ids` 可以指定某些 message id 使用 `required_stable_count=1`。

这不会改变工具 schema。模型仍然只能提交：

```text
decision
basis_message_id
```

是否允许单次确认由程序侧 policy 决定，不交给模型。

### known_message_ids 校验

`task_boundary` 还会校验 `basis_message_id` 是否属于当前 session。

resume 后，`AgentSession.resume()` 会把历史 message id 注入 `known_message_ids`，否则模型引用旧消息时工具会拒绝。

这个校验的意义：

- 防止模型引用不存在的 message id。
- 防止任务 hash 基于伪造 basis 生成。
- 保证 task hash 和 session event log 里的真实消息绑定。

### 对压缩层的影响

task hash 影响压缩有三处：

- `TASK_HASH_CHANGED`：确认任务切换后强制触发 compact。
- L1：压缩 `task_hash != active_task_hash` 的旧任务 text part。
- L3：只压缩当前任务里已经变冷的 text part。

所以任务哈希既是触发信号，也是压缩选择依据。

## 其他强制触发

### 手动触发

手动触发来自 TUI `/compact`。

流程：

```text
用户输入 /compact
  -> ContextCommandHandler._manual_compact()
  -> compact_if_needed(trigger=MANUAL, mode=MANUAL)
  -> manager 强制执行 L1-L4
```

用途：

- 用户主动观察压缩效果。
- 学习和复盘压缩前后的 token 变化。
- 自动压缩没有触发时，也可以手动运行。

### Prompt Too Long 触发

`PROMPT_TOO_LONG` 是 provider 拒绝请求后的修复路径。

语义：

```text
ContextBuilder 已经构造 provider messages
  -> provider 返回 prompt too long
  -> agent loop 用 trigger=PROMPT_TOO_LONG 请求 compact
  -> compact 目标可以使用 blocking_target_tokens
  -> compact 后重建上下文再重试 provider 调用
```

`ContextCompactionConfig.target_for_trigger()` 对它有特殊处理：

```python
if trigger == "prompt_too_long" and blocking_target_tokens is not None:
    return blocking_target_tokens
```

当前代码里同步和 streaming agent loop 都已经接入该恢复路径：只有
`ProviderErrorKind.PROMPT_TOO_LONG` 会触发 `PROMPT_TOO_LONG` compact，且压缩成功后只重试一次
provider 请求。

## L1-L5 分层策略

README 里的上下文压缩计划是：

- L1 Micro Compact
- L2 Archive + Placeholder
- L3 Content-Routed Compress
- L4 Session Summary Compact
- L5 Manual Compact

当前代码里：

- L1-L3 在 `CompactionPipeline`。
- L4 在 `LlmCompactService`。真实 TUI 默认路径通过 `ProviderLlmCompactSummarizer`
  接入当前 provider 来生成摘要；测试或特殊场景仍可以注入自定义 summarizer。
- L5 表现为 `ContextWindowTrigger.MANUAL` 和 TUI `/compact` 命令入口。

### L1 Micro Compact

目标：

> 低成本压缩旧任务内容。

当前实现压缩的是旧任务 `text` part，而不是大工具输出。判断条件：

- part 未压缩。
- part.kind 是 `text`。
- part 有 `task_hash`。
- part 的 `task_hash` 与当前 active task hash 不一致。

输出：

- 保留 preview。
- 保留原始 token 数。
- 标记 `compaction_state=micro_compacted`。
- 只在 replacement 更短时生效。

设计思考：

- 旧任务不是完全没用，但通常不值得保留完整原文。
- 用 task hash 区分任务，比单纯按时间截断更符合多任务对话。
- L1 不能依赖 LLM，否则会把最常见压缩路径变贵。

### L2 Archive + Placeholder

目标：

> 大工具结果完整落盘，prompt 中只保留可追踪占位符。

判断条件：

- part 未压缩。
- part.kind 是 `tool_result`。
- token 数超过 `large_tool_result_tokens`。

落盘位置：

```text
.firstcoder/archives/{session_id}/{archive_id}.txt
.firstcoder/archives/{session_id}/{archive_id}.json
```

上下文 placeholder：

```text
[Tool result archived]
archive_id=...
summary=...
original_tokens=...
preview_tokens=...
preview=...
```

设计思考：

- 工具结果可能包含排查所需证据，不能直接丢。
- provider 不需要看到几万 token 的原始 log。
- archive id 让后续调试、分享策略、恢复检查都有引用点。
- 已归档 part 再次压缩时直接返回，避免重复落盘。

### L3 Content-Routed Compress

目标：

> 对当前任务中已经变冷的文本，按内容类型使用专门策略压缩。

判断条件：

- part 未压缩。
- part.kind 是 `text`。
- part 属于当前 active task。
- `current_turn - created_turn >= cold_turn_distance`。

路由类型：

- `search_results`
- `git_diff`
- `build_output`
- `json_array`
- `json_object`
- `source_code`
- `html`
- `plain_text`

设计思考：

- grep 结果应该保留文件路径和命中行。
- git diff 应该保留文件、hunk、增删重点。
- build output 应该保留 error、traceback、warning。
- JSON 应该保留结构、关键字段和数量。
- 源码压缩不能等同普通文本截断。

因此 L3 分成两步：

```text
detect_route_content_type()
  -> 找到内容类型
  -> 调用对应 RouteCompressor
  -> 验证 replacement 更短
  -> 写入统一 metadata
```

### L4 Session Summary Compact

目标：

> 当 L1-L3 仍无法把上下文压到预算内时，用 LLM 总结旧历史，生成 checkpoint。

L4 不直接替换原始消息，而是写入 `checkpoint_created`。

L4 输入：

```text
无旧 checkpoint:
  conversation messages

有旧 checkpoint:
  synthetic checkpoint summary message + 当前 tail messages
```

L4 不总结：

- system prompt
- tool schema
- provider capabilities
- permission policy

原因：

- 这些属于 stable prefix，不是会话历史。
- 把系统提示和权限策略折进 summary 会污染边界。

summarizer 返回：

```python
LlmCompactSummary(
    summary=...,
    tail_start_message_id=...,
    covered_until_message_id=...,
)
```

写入 checkpoint 前必须校验：

- `tail_start_message_id` 在当前 tail 内。
- `covered_until_message_id` 在当前 tail 内。
- covered message 在 tail start 之前。
- 新 tail 不破坏 tool_call/tool_result 序列。

设计思考：

- L4 是最强但风险最高的压缩层。
- 摘要可能丢信息，也可能 provider 失败。
- 所以前面必须先跑便宜、确定性的 L1-L3。
- L4 只写 checkpoint，不覆盖原始事件，降低不可逆风险。

### L5 Manual Compact

目标：

> 给用户一个显式观察和触发压缩的入口。

当前表现：

- `ContextWindowTrigger.MANUAL`
- TUI `/compact`
- TUI `/compact status`
- `ContextInspector`

设计思考：

- 自动压缩是系统行为，用户不一定知道发生了什么。
- 手动 compact 可以帮助学习和复盘压缩前后 token 变化。
- 失败时用户可以看到状态，而不是只收到 provider prompt too long。

## 压缩编排流程

`ContextWindowManager.compact_if_needed()` 的主流程：

```text
ContextCompactRequest
  |
  v
evaluate_context_triggers()
  |
  +-- 不需要压缩 -> skipped
  |
  +-- auto 且熔断打开 -> skipped(circuit_open)
  |
  v
CompactionPipeline.compact()
  |
  +-- L1 old task compact
  +-- L2 archive large tool result
  +-- L3 content-routed compact
  |
  v
append compaction_completed event
  |
  v
重新估算 token
  |
  +-- 已达 target -> success
  |
  +-- 未达 target 且无 L4 service -> failed
  |
  v
LlmCompactService.compact()
  |
  +-- build L4 source
  +-- summarize
  +-- validate boundary
  +-- append checkpoint_created event
  |
  v
append llm_compaction_completed event
  |
  v
rebuild SessionView
  |
  v
返回 ContextCompactResult
```

manager 层负责“什么时候、按什么顺序、失败怎么办”。具体压缩算法不写在 manager 里。

## 事件如何表达压缩

程序化压缩的结果写成 `compaction_completed`。

事件里有：

- input fingerprint
- before/after tokens
- levels attempted
- stopped_at
- changed parts
- replacements
- strategy version

replacements 结构表达：

```text
message_id
source_part_id
replacement_part
```

`store.rebuild_session_view()` 看到 compaction event 后，会在重建视图时把对应 part 替换掉。

设计原因：

- JSONL 日志仍是 append-only。
- 原始消息事件还在。
- 当前视图看到的是压缩后的 part。
- 以后可以审计“哪次压缩替换了什么”。

L4 压缩会写两类事件：

- `checkpoint_created`：真正改变投影边界。
- `llm_compaction_completed`：记录 L4 状态、失败原因、retry/fallback 信息。

## Resume 与 Checkpoint 的关系

这是最容易混淆的地方。

resume 不是从 checkpoint 后恢复。

resume 做的是：

```text
读取完整 session event log
  -> rebuild SessionView
  -> replay SessionRuntimeState
  -> 创建 AgentSession
```

checkpoint 做的是：

```text
下一轮构造 provider messages 时:
  用 checkpoint summary 表示旧历史
  从 tail_start_message_id 开始保留最近原文
```

所以：

- event log 是存储边界。
- checkpoint 是 prompt 投影边界。
- archive 是大工具输出的本地保留边界。
- runtime_state 是非自然语言状态的恢复边界。

## Runtime State 的作用

`SessionRuntimeState` 不写成自然语言消息。

它保存：

- active task hash
- candidate task hash
- latest checkpoint id
- auto compact failure count
- auto compact disabled until
- last auto compact failure reason
- system prompt fingerprint
- last compaction input fingerprint
- recent compaction events

`runtime_replay.py` 从事件恢复这些状态。

设计原因：

- task hash、熔断状态、fingerprint 不应该污染模型上下文。
- 但 resume 后又必须恢复它们。
- 所以它们通过事件日志重放，而不是写进 prompt。

## System Prompt 为什么单独处理

`system_prompt.py` 管：

- base rules
- AGENTS.md
- tool definitions
- provider capability
- permission policy
- mode

这些内容属于 stable prefix。

它不进入 L4 summary，因为：

- system prompt 是高优先级指令。
- 工具 schema 是协议输入。
- provider capability 是适配信息。
- permission policy 是安全边界。

L4 只总结 conversation history。

## Tool Calling 边界

tool calling 对上下文压缩影响很大。

不能出现：

```text
assistant tool_call 被 checkpoint 覆盖
tool_result 留在 tail 开头
```

因为 provider 会看到一个没有对应 tool call 的 tool result。

所以：

- `ContextBuilder` 投影前校验 tool sequence。
- L4 选择 tail 边界前校验 tool sequence。
- checkpoint tail 不能从 orphan tool result 开始。

这是上下文压缩层必须理解 provider 协议的少数地方。

## 当前架构的取舍

### 事件日志优先，而不是直接改内存

优点：

- resume 稳。
- 压缩可审计。
- 未来分享 transcript 有数据来源。
- 手动调试方便。

代价：

- store rebuild 要处理事件顺序和 replacement。
- session catalog 当前扫描 JSONL，未来如果会话数量变多再引入索引。

### 程序化压缩优先，而不是一上来 LLM 总结

优点：

- 便宜。
- 可预测。
- 不依赖 provider。
- 不容易编造摘要。

代价：

- 规则要逐步补。
- 对复杂语义的压缩能力有限。

### checkpoint 只影响投影，而不是删除历史

优点：

- 原始会话可恢复。
- 分享和调试可以回看完整事实。
- checkpoint 生成错误时不至于永久丢数据。

代价：

- JSONL 会持续增长。
- 需要区分“完整历史”和“effective context”。

### archive 保留原文，而不是丢弃工具输出

优点：

- 大输出不进 prompt。
- 原始证据仍可查。
- placeholder 可追踪。

代价：

- 分享时必须特别处理 archive，默认不能展开。
- 本地存储会增长。

## 当前仍然需要改进的点

- `context` 当前包含底层 session store，但用户可见的 session catalog/share 已经放在 `firstcoder/session`。
- token 估算仍是近似值，后续可以按 provider 接入真实 tokenizer。
- L4 summarizer 已有默认 provider adapter，但摘要 prompt、边界选择策略和结构化摘要格式仍可以继续增强。
- share 已经从 event log 派生只读 transcript，不导出可继续运行的 session snapshot。
- 压缩后的 archive 清理、过期策略、磁盘预算还没有设计。

## 阅读代码建议

建议按这个顺序读：

1. `models.py`：先理解内部消息结构。
2. `events.py`、`store.py`、`writer.py`：理解事件日志和 rebuild。
3. `context_builder.py`、`checkpoint.py`：理解 checkpoint 如何影响 provider context。
4. `triggers.py`、`manager.py`：理解压缩什么时候触发、如何编排。
5. `compaction.py`、`content/*`、`archive.py`：理解 L1-L3。
6. `llm_compact.py`、`fallback.py`、`retry_policy.py`：理解 L4 和失败处理。
7. `runtime_state.py`、`runtime_replay.py`：理解 resume 后如何恢复非自然语言状态。
