# FirstCoder 架构准备计划

本文档记录在实现 `context-compact-plan.md` 之前需要先搭好的架构地基。

目标不是提前实现完整 agent，而是先把后续不应该写散的边界定清楚。当前工作区已经删除旧的 `agent/app/memory/context` 试验实现，只保留 `providers`、`tools`、`config` 和 `utils` 基础层。

## 设计原则

- provider message 只是本轮请求格式，不作为长期会话事实模型。
- tool result 不能只以纯文本存储，必须有结构化 metadata。
- 压缩、resume、archive、checkpoint 都依赖同一套内部消息协议。
- 存储层保存事实，投影层决定本轮给 provider 看什么。
- 第一版保持简单，不引入 Claude Code 的 `parentUuid` graph。

## P0：内部消息协议

先定义 FirstCoder 自己的会话事实格式。

建议模块：

```text
firstcoder/context/models.py
```

核心对象：

```python
@dataclass(slots=True)
class AgentMessage:
    id: str
    session_id: str
    role: str  # user / assistant / tool / system_meta
    parts: list[MessagePart]
    created_at: str
    metadata: dict[str, Any]


@dataclass(slots=True)
class MessagePart:
    id: str
    message_id: str
    kind: str  # text / tool_call / tool_result / checkpoint / compaction / attachment
    content: str
    metadata: dict[str, Any]
```

第一版需要支持的 part：

```text
text
tool_call
tool_result
checkpoint_summary
compaction_event_ref
archive_placeholder
```

不要把 `firstcoder.providers.types.ChatMessage` 当存储模型。`ChatMessage` 只用于 provider 请求投影。

## P1：SessionEvent 事件日志

采用 append-only 事件作为最小可靠存储模型。第一版可以用 JSONL，后续再迁移 SQLite。

建议模块：

```text
firstcoder/context/events.py
firstcoder/context/store.py
```

核心事件：

```text
session_created
user_message
assistant_message
tool_call
tool_result
archive_created
compaction_event
checkpoint_created
task_hash_event
runtime_state_updated
```

事件日志的作用：

```text
方便 resume
方便 debug
方便重放构造当前 SessionView
避免压缩逻辑直接修改原始历史
```

第一版不需要复杂查询，只要能：

```text
append_event(session_id, event)
list_events(session_id)
rebuild_session_view(session_id)
```

## P2：Projection 层

Projection 层负责把内部事实账本转换为 provider 请求。

建议模块：

```text
firstcoder/context/context_builder.py
```

职责：

```text
读取 SessionView
读取 latest checkpoint
拼接 stable system prefix
拼接 checkpoint summary
拼接 recent tail
把 tool_call / tool_result part 转成 provider ChatMessage
过滤内部事件和不可见 metadata
```

不负责：

```text
不压缩
不总结
不落盘 archive
不判断任务边界
不生成 checkpoint
```

这层等价于 Claude Code 的 `normalizeMessagesForAPI()` 思路，但 FirstCoder 第一版保持简单。

## P3：Tool Result Normalizer

所有工具执行结果进入会话前，先统一加工成结构化结果。

建议模块：

```text
firstcoder/context/tool_result.py
```

结构化字段：

```text
tool_name
tool_call_id
ok
content
data
error
token_estimate
content_fingerprint
display_preview
archive_id
task_hash
compaction_state
created_turn
```

`compaction_state` 第一版枚举：

```text
raw
archived
micro_compacted
route_compacted
checkpointed
pinned
```

后续 L1/L2/L3/L4 都只看这些 metadata，不从自然语言里猜状态。

## P4：ID 与 Fingerprint 工具

统一生成 ID 和稳定 hash，避免各模块自己拼字符串。

建议模块：

```text
firstcoder/context/identity.py
```

函数：

```text
new_session_id()
new_message_id()
new_part_id()
new_event_id()
new_archive_id()
new_checkpoint_id()
stable_json_hash(value)
content_fingerprint(text)
```

使用场景：

```text
task hash
system prompt fingerprint
tool schema hash
content fingerprint
checkpoint source_fingerprint
CompactionEvent input_fingerprint
```

## P5：TokenBudgetService

token 预算不要散落在各模块。

建议模块：

```text
firstcoder/context/token_budget.py
```

职责：

```text
读取 provider context window
计算 reserved_output_tokens
计算 warning / auto / blocking threshold
估算 text / part / message / provider request tokens
给 compaction pipeline 返回目标预算
```

第一版可以继续使用字符近似估算，后续再按 provider 接 tokenizer。

## P6：Provider Error Taxonomy

统一 provider 错误分类，服务重试、兜底和自动压缩。

建议模块：

```text
firstcoder/providers/errors.py
```

错误类型：

```text
prompt_too_long
timeout
rate_limit
auth_error
api_error
user_abort
network_error
unknown
```

用途：

```text
prompt_too_long -> 触发更强压缩后重试
timeout/network_error/rate_limit -> 退避重试
auth_error -> 不重试，直接提示
user_abort -> 不重试
api_error/unknown -> 按 provider 策略处理
```

## P7：SessionRuntimeState

运行时状态不要塞进自然语言消息里。

建议模块：

```text
firstcoder/context/runtime_state.py
```

字段：

```python
@dataclass(slots=True)
class SessionRuntimeState:
    session_id: str
    active_task_hash: str | None
    candidate_task_hash: str | None
    task_hash_stable_count: int
    latest_checkpoint_id: str | None
    auto_compact_failure_count: int
    auto_compact_disabled_until: str | None
    system_prompt_fingerprint: str | None
    last_compaction_input_fingerprint: str | None
```

用途：

```text
任务 hash 稳定窗口
自动 compact 熔断
system prompt cache 复用
no-op compact 防抖
resume 后恢复当前上下文状态
```

## P8：版本字段

所有会影响缓存和压缩可信度的策略都要有版本。

建议常量：

```text
SYSTEM_PROMPT_VERSION
COMPACTION_STRATEGY_VERSION
TASK_BOUNDARY_TOOL_VERSION
TOOL_RESULT_NORMALIZER_VERSION
CONTEXT_PROJECTION_VERSION
```

需要记录的位置：

```text
system_prompt_fingerprint 输入
Checkpoint.strategy_version
CompactionEvent.strategy_version
ToolResult metadata
SessionRuntimeState
```

## 实现顺序

第一轮只做：

```text
P0 内部消息协议
P1 SessionEvent 事件日志
P2 Projection 层接口
P4 ID 与 Fingerprint 工具
```

第二轮做：

```text
P3 Tool Result Normalizer
P5 TokenBudgetService
P7 SessionRuntimeState
```

第三轮做：

```text
P6 Provider Error Taxonomy
P8 版本字段
接入 context-compact-plan.md 的 Archive / Checkpoint / Task Hash / Pipeline
```

## 验收标准

最小验收：

```text
内部 AgentMessage 可以投影为 provider ChatMessage
事件日志可以重建 SessionView
tool result 进入历史前有 token_estimate 和 content_fingerprint
ContextBuilder 不依赖具体 provider 实现
同一段输入的 stable_json_hash 在多次运行中一致
普通消息追加不会改变 system prompt fingerprint
```

不在本计划内：

```text
不实现完整 Textual UI
不实现 L1-L4 压缩 pipeline
不实现复杂 parentUuid graph
不实现 Claude Code 式 partial compact / middle snip
```
