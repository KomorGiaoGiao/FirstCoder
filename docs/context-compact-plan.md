# FirstCoder 上下文压缩与恢复计划

本文档记录 FirstCoder 当前采用的上下文管理方案。目标是先做一个简单、可验证、方便学习的版本，而不是一开始实现 Claude Code 那种复杂的 `parentUuid` 链和中间 snip。

当前决策：采用类似 opencode 的顺序会话存储模型。

```text
Session 顺序存事实；
Message 表示一轮消息；
Part 表示消息内容块；
SystemPromptBuilder 生成稳定系统前缀；
PromptPrefixCache 用 fingerprint 判断是否复用；
Archive 存大工具结果；
Checkpoint 代表旧历史；
ContextBuilder 每次投影当前上下文；
Resume 加载最新 checkpoint + recent tail。
```

## 设计目标

- 系统提示词不参与压缩，但也不需要每轮无脑完全重建；用 fingerprint 判断稳定前缀是否可复用。
- 对话历史顺序存储，不做复杂 graph / parent relink。
- 第一版不做中间 snip。确认换话题后会立即触发自动压缩 pipeline，但这只是一次“维护动作”，不等价于固定画一条 compact 边界线。
- 大工具结果尽早落盘，prompt 中只保留 preview、summary 和 archive_id。
- 自动压缩走四层 pipeline，达到预算目标就停止，尽量不调用 LLM。
- hash 只作为任务边界的辅助信号，不作为唯一压缩依据。
- hash 由工具统一生成，模型不能在正文里自由输出 hash。
- resume 时恢复主要工作链路，而不是展开所有旧工具输出。
- 区分任务边界和压缩投影边界：换话题不强制画线，但 L4 checkpoint 覆盖过的旧历史必须有投影边界，避免后续重复压缩同一段原文。

## 总体架构

```text
用户输入
  -> AgentLoop
  -> SessionStore append Message/Part
  -> TaskBoundaryTool 判断任务边界候选
  -> SystemPromptBuilder 计算 system prompt fingerprint
  -> PromptPrefixCache 复用或重建稳定系统前缀
  -> ContextWindowManager 判断是否需要压缩
  -> CompactionPipeline 按层压缩
  -> ContextBuilder 构造 provider messages
  -> Provider.chat
  -> assistant/tool result 继续 append
```

核心模块建议：

```text
firstcoder/context/models.py
firstcoder/context/store.py
firstcoder/context/system_prompt.py
firstcoder/context/context_builder.py
firstcoder/context/token.py
firstcoder/context/archive.py
firstcoder/context/task_boundary.py
firstcoder/context/compaction.py
firstcoder/context/checkpoint.py
firstcoder/context/inspector.py

firstcoder/context/content/router.py
firstcoder/context/content/detector.py
firstcoder/context/content/compressors.py
```

## 存储模型

第一版可以继续用 JSONL，后续如果需要查询性能，再迁移到 SQLite。逻辑模型保持不变。

### Session

```python
@dataclass(slots=True)
class Session:
    id: str
    title: str | None
    active_task_hash: str
    system_prompt_fingerprint: str | None
    created_at: str
    updated_at: str
```

`system_prompt_fingerprint` 记录上一轮稳定系统前缀的指纹。它不是压缩边界，只用于判断本轮是否能复用 system prompt 构造结果。

### Message

`Message` 只表示一轮消息，不直接承载复杂内容。

```python
@dataclass(slots=True)
class Message:
    id: str
    session_id: str
    role: str  # user / assistant / tool / system_meta
    created_at: str
    metadata: dict[str, Any]
```

### Part

`Part` 表示消息里的内容块。

```python
@dataclass(slots=True)
class Part:
    id: str
    session_id: str
    message_id: str
    kind: str  # text / tool_call / tool_result / file_reference / compaction
    content: str
    metadata: dict[str, Any]
```

常见 part：

```text
text
  普通用户文本或 assistant 文本。

tool_call
  保存 tool_call_id、tool_name、arguments。

tool_result
  保存 tool_call_id、status、summary、archive_id、preview。

compaction
  保存压缩标记、checkpoint 引用或 placeholder。
```

### Checkpoint

`Checkpoint` 是 resume 的核心。它代表旧历史的压缩结果，也代表一个压缩投影边界。

这里的边界不是“话题边界”，而是“这段旧历史已经由 summary 代表”。后续 ContextBuilder 和下一次 L4 compact 都应该从最新 checkpoint 的 `tail_start_message_id` 之后继续投影，避免把同一段原始历史反复送进 summarizer。

```python
@dataclass(slots=True)
class Checkpoint:
    id: str
    session_id: str
    summary: str
    tail_start_message_id: str
    covered_until_message_id: str
    active_task_hash: str
    open_tasks: list[str]
    touched_files: list[str]
    decisions: list[str]
    next_step: str | None
    source_message_ids: list[str]
    source_fingerprint: str
    strategy_version: str
    created_at: str
```

字段语义：

```text
tail_start_message_id
  resume 和普通请求从这里之后保留原文 tail。

covered_until_message_id
  表示 checkpoint summary 覆盖到哪一条旧消息。
  第一版可以等于 tail_start_message_id 的前一条消息。

source_message_ids / source_fingerprint
  用于判断同一批历史是否已经被同一策略压缩过。

strategy_version
  压缩 prompt 或规则升级时，允许重新生成 checkpoint。
```

### Archive

大工具结果落盘。

```python
@dataclass(slots=True)
class ArchiveRef:
    id: str
    session_id: str
    part_id: str
    path: str
    original_tokens: int
    preview_tokens: int
    summary: str
    created_at: str
```

目录建议：

```text
.firstcoder/
  sessions/
    <session_id>.jsonl
  archives/
    <session_id>/
      <archive_id>.txt
      <archive_id>.json
```

## 系统提示词与稳定前缀

系统提示词是程序配置，不是普通会话历史。压缩层不能修改它。

但系统提示词也不需要每轮都完整重建。应该把系统提示词拆成稳定块，并计算 fingerprint。

### SystemPromptBuilder

负责生成稳定系统前缀：

```text
base agent rules
AGENTS.md 内容
tool calling rules
permission rules
provider capability rules
enabled tools schema summary
```

不应该进入稳定系统前缀的内容：

```text
当前 token 统计
最近工具输出
当前用户消息
临时 checkpoint summary
任务 hash 候选状态
动态时间戳
```

这些属于 conversation projection 或 request metadata。

### PromptPrefixCache

缓存上一轮 system prompt 构造结果：

```python
@dataclass(slots=True)
class PromptPrefixCacheEntry:
    fingerprint: str
    messages: list[ProviderMessage]
    token_estimate: int
    created_at: str
```

本轮请求时：

```python
fingerprint = compute_system_prompt_fingerprint(inputs)

if cache.fingerprint == fingerprint:
    system_prefix = cache.messages
else:
    system_prefix = build_system_prompt(inputs)
    cache.update(fingerprint, system_prefix)
```

### Fingerprint 输入

fingerprint 应由这些稳定输入计算：

```text
base_prompt_version
AGENTS.md content hash
enabled tool names + schema hashes
provider name + provider capability version
permission policy version
output style / mode
relevant config version
```

示例：

```python
fingerprint = sha256(json.dumps({
    "base_prompt_version": BASE_PROMPT_VERSION,
    "agents_md_hash": agents_md_hash,
    "tools_schema_hash": tools_schema_hash,
    "provider_caps_hash": provider_caps_hash,
    "permission_policy_hash": permission_policy_hash,
    "mode": mode,
}, sort_keys=True).encode()).hexdigest()
```

### 失效条件

这些变化应该让 system prompt cache 失效：

```text
AGENTS.md 内容变化
工具 schema 变化
启用/禁用工具变化
provider 能力变化
权限策略变化
agent mode / output style 变化
基础系统提示词版本变化
```

这些变化不应该让 system prompt cache 失效：

```text
普通 user/assistant 消息追加
tool result 追加
archive placeholder 更新
micro compact
checkpoint 更新
task hash 候选变化
token 统计变化
```

### 压缩时的规则

compact summary prompt 必须要求：

```text
不要总结 system prompt、tool schema、provider policy。
只总结用户目标、执行过程、关键决策、文件、错误和下一步。
```

最终请求结构：

```text
stable system prefix
conversation projection
```

其中 stable system prefix 可通过 fingerprint 复用，conversation projection 每轮重新投影。

## Task Hash 工具

hash 生成不让模型自由输出。模型必须调用工具。

工具参数保持极简：

```python
propose_task_boundary(
    decision: Literal["same", "new", "uncertain"],
    basis_message_id: str,
)
```

模型只负责判断：

```text
same      仍是当前任务
new       明确进入新任务
uncertain 不确定
```

工具内部负责：

```text
校验 basis_message_id 是否存在
读取当前 session_id / active_task_hash / task_index
如果 same 或 uncertain，沿用 active_hash
如果 new，生成 candidate_hash
维护稳定窗口
确认切换后触发 compaction pipeline
记录 TaskHashEvent
```

hash 生成建议：

```python
task_hash = sha256(f"{session_id}:{task_index}:{basis_message_id}").hexdigest()[:8]
```

这不是跨会话语义 hash，而是本 session 内稳定的任务边界 hash，足够用于压缩和调试。

### 稳定窗口

不要一次 `new` 就立刻切换。

```text
new 候选连续稳定 2 轮
或 decision=new 且当前用户消息明确表示“换个问题/先不做/另一个任务”
才确认 active_task_hash 切换。
```

确认切换后立即触发自动压缩 pipeline：

```text
触发 compaction pipeline
reason = task_hash_changed
old_hash = ...
new_hash = ...
```

注意：这里的“触发压缩”不是“必须立刻写 checkpoint”，也不是“必须把历史从这里切成前后两段”。

```text
如果 L1/L2/L3 已经把上下文降到目标预算：
  只记录 CompactionEvent，不写 checkpoint。

如果仍然超预算，或者需要把旧话题语义折叠成 resume 状态：
  才进入 L4，写 checkpoint。
```

换句话说，hash 确认切换只是自动压缩的触发器，不等价于固定裁切线。自动压缩可以只处理已经明显陈旧的工具结果、旧任务片段和大结果 placeholder；是否写 checkpoint、是否调整 tail_start_message_id，由压缩后的 token 预算和 resume 需要决定。

### 换话题触发语义

第一版采用事件驱动，而不是边界驱动：

```text
确认换话题
  -> 立即运行 compaction pipeline
  -> 每层压缩后重新估算 token
  -> 达标就停止
  -> 记录 CompactionEvent
```

这个事件可以产生这些结果：

```text
只更新旧 tool_result placeholder
只把大结果 archive
只生成 route compact part
生成 checkpoint summary
什么都不用改，只记录一次 no-op CompactionEvent
```

它不要求：

```text
不要求固定写 checkpoint
不要求固定移动 tail_start_message_id
不要求把换话题那一轮永久当作硬边界
不要求删除或隐藏所有旧话题消息
```

这样设计的好处是：换话题后可以尽早回收上下文，但历史结构仍然保持顺序存储，resume 时也不会被过早的边界设计绑死。

## 压缩触发条件

硬触发：

```text
provider 返回 prompt_too_long
估算 token 超过 blocking threshold
单个 tool result 太大
同一轮 tool results 合计太大
resume 时 checkpoint + tail 仍然过大
```

软触发：

```text
task hash 确认切换
估算 token 超过 auto compact threshold
旧工具结果不属于当前 task_hash
距离上次模型请求太久，prompt cache 大概率过期
checkpoint 后 tail messages 太多
用户手动 /compact
```

建议第一版阈值：

```python
reserved_output_tokens = min(provider.max_output_tokens, 16_000)
effective_window = context_window - reserved_output_tokens

warning_threshold = effective_window * 0.70
auto_compact_threshold = effective_window * 0.82
blocking_threshold = effective_window * 0.95

max_inline_tool_chars = 20_000
max_turn_tool_chars = 50_000
max_tail_messages = 60
max_tail_tokens = effective_window * 0.35
```

## 四层压缩 Pipeline

所有自动压缩都走同一个 pipeline。hash 变化只触发 pipeline，不直接触发 LLM summary。

```text
L1 Micro Compact
  清理旧任务或跨任务的 tool_result，保留 placeholder、summary、archive_id。

L2 Archive Compact
  大结果落盘，prompt 中保留 preview 和 archive_id。

L3 Route Compact
  程序化整理仍属于本次任务、但相对冷的上下文信息，例如中间日志、搜索结果、旧测试输出、阶段性 diff。

L4 LLM Compact
  最后兜底，生成 checkpoint summary，保留 recent tail。
```

每层执行后重新估算 token：

```python
for level in levels:
    result = level.run()
    if result.after_tokens <= target_tokens:
        stop
```

不同触发原因的目标预算：

```text
task_hash_changed
  立即运行 pipeline；降到 warning_threshold 即可。
  优先 L1/L2/L3，能达标就不写 checkpoint。

auto_threshold
  降到 warning_threshold。

blocking
  降到 auto_compact_threshold。

prompt_too_long
  降到 auto_compact_threshold，并预留输出空间。
```

## 压缩边界与防重复

FirstCoder 不采用复杂 graph，也不把“换话题”当成硬裁切线。但为了避免同一段内容被反复压缩，需要引入两类边界：

```text
任务边界
  由 task hash 辅助判断。
  用来决定 L1 旧任务清理、L3 当前任务冷信息整理。
  不直接决定 resume 从哪里开始。

压缩投影边界
  由 latest checkpoint 决定。
  表示 checkpoint.summary 已经覆盖了哪些旧历史。
  用来决定 ContextBuilder 和下一次 L4 compact 的输入范围。
```

### L1/L2/L3 的防重复

程序化压缩使用 part 级状态，而不是反复扫描同一份 raw 内容。

建议 metadata 字段：

```text
compaction_state = raw | archived | micro_compacted | route_compacted | checkpointed | pinned
archive_id
compacted_by
compacted_at
compaction_strategy_version
source_fingerprint
```

规则：

```text
L1 只处理 task_hash != active_task_hash 且 compaction_state in [raw, archived] 的旧任务内容。
L2 看到已有 archive_id 就不重复落盘。
L3 只处理 task_hash == active_task_hash 且 compaction_state in [raw, archived] 的当前任务冷信息。
已经 micro_compacted / route_compacted / checkpointed / pinned 的 part 默认跳过。
策略版本变化时，可以允许重新压缩，但必须写新的 CompactionEvent。
```

### L4 的防重复

L4 LLM compact 借鉴 Claude Code 的 compact boundary 思路，但在 FirstCoder 里落成 `Checkpoint`。

规则：

```text
下一次 L4 的输入不应该是全量原始历史。
如果存在 latest checkpoint：
  L4 输入 = latest_checkpoint.summary + messages_after(tail_start_message_id)
否则：
  L4 输入 = 当前有效 messages/parts
```

这样允许“summary 再 summary”，但不会把已经被 checkpoint 覆盖的原始历史反复送进 LLM。

`tail_start_message_id` 必须单调向后移动：

```text
新 checkpoint.tail_start_message_id 不能早于旧 checkpoint.tail_start_message_id。
已经被 checkpoint 覆盖的 part 可以标记为 checkpointed。
checkpointed part 不再参与 L1/L3 候选选择。
```

### no-op 防抖

换话题、软阈值或手动命令都可能触发 pipeline，但不一定有可压缩内容。no-op 也要记录：

```text
CompactionEvent(
  success=true,
  stopped_at="noop",
  before_tokens=...,
  after_tokens=...,
  input_fingerprint=...
)
```

短时间内如果触发原因、active_task_hash、latest_checkpoint_id、候选 part fingerprint 都相同，可以直接跳过自动压缩，避免重复触发。

## L1 Micro Compact

处理旧任务、跨任务或明显不再属于当前任务链路的工具结果。L1 的重点是“任务归属已经变旧”，不是压缩本次任务内部的信息。

候选：

```text
task_hash != active_task_hash
距离当前超过 N 轮
不是最近失败原因
不是最近修改文件的关键证据
```

替换为：

```text
[Old tool result compacted]
tool=...
status=...
summary=...
archive_id=...
reason=stale_task_context
```

注意：第一版只做旧历史/旧任务 micro compact，不做中间 snip，也不要求每次话题切换都写 checkpoint。

## L2 Archive Compact

工具结果刚产生时就应该检查是否过大。

```text
tool_result 太大
  -> 保存完整内容到 archive
  -> part.content 替换为 preview
  -> metadata 写 archive_id、summary、original_tokens
```

placeholder 示例：

```text
[Archived tool result]
tool=shell
archive_id=ar_001
original_tokens≈18420
summary=pytest 失败，主要错误在 tests/test_config.py::test_load_env
preview:
...
```

## L3 Route Compact

L3 处理的是“仍属于本次任务，但已经相对冷”的信息。它和 L1 的边界不同：

```text
L1
  目标是旧任务、跨任务、已经脱离当前 task_hash 的内容。

L3
  目标是当前 task_hash 内部的信息。
  信息仍然可能对后续有用，但不需要全文保留在热上下文里。
```

尽量不用 LLM，通过内容检测、路由和专用 compressor 整理本次任务里的冷信息。

候选：

```text
task_hash == active_task_hash
属于本次任务的较早工具结果
已经被后续结果覆盖的测试日志、构建日志、搜索结果
已经完成阶段的中间 diff 或中间诊断输出
不是当前正在处理的失败栈
不是最近一轮用户明确要求保留/查看的内容
```

不应该进入 L3 的内容：

```text
当前最近 tail
当前失败原因的唯一证据
刚产生、还没被 assistant 消化的 tool result
用户刚要求逐字查看的输出
跨任务旧内容，因为这应该由 L1 处理
```

这部分借鉴 Headroom 的 transforms 设计：

```text
D:\Komor_Code\agent-learning\headroom\headroom\transforms\content_detector.py
D:\Komor_Code\agent-learning\headroom\headroom\transforms\content_router.py
D:\Komor_Code\agent-learning\headroom\headroom\transforms\search_compressor.py
D:\Komor_Code\agent-learning\headroom\headroom\transforms\log_compressor.py
D:\Komor_Code\agent-learning\headroom\headroom\transforms\diff_compressor.py
D:\Komor_Code\agent-learning\headroom\headroom\transforms\code_compressor.py
```

FirstCoder 第一版不要直接照搬完整 Headroom。先实现轻量版：

```text
ContentDetector
  判断 tool result 属于哪类内容。

ContentRouter
  根据 detector 结果选择 compressor。

Compressor
  返回 compressed、summary、stats、original_tokens、compressed_tokens。
```

### 内容类型

路由类型：

```text
json_array
  JSON 数组或对象列表。后续可借鉴 SmartCrusher，保留错误项、首尾项、异常项和相关项。

source_code
  源码。后续可借鉴 CodeAwareCompressor，保留结构和签名，压缩函数体。

search_results
  grep / ripgrep 输出，例如 path:line:content。

build_output
  pytest、npm、cargo、lint、编译器、shell 日志。

git_diff
  unified diff、git diff、combined diff。

html
  网页内容。优先抽取正文，不做普通文本压缩。

plain_text
  兜底文本。
```

### 检测顺序

第一版按 Headroom 的思路做确定性检测：

```text
1. JSON
   能 json.loads，且是 list/dict。

2. Diff
   命中 diff --git、--- a/、+++ b/、@@ hunk header。

3. HTML
   命中 <!doctype html>、<html>、<body>、大量结构标签。

4. Search
   多行符合 path:line:content 或 path-line-content。

5. Build / Log
   包含 ERROR、FAILED、Traceback、npm ERR、pytest summary、stack trace。

6. Source Code
   命中 def/class/import、function/const/export、fn/struct/impl、package/func 等。

7. Plain Text
   其他内容。
```

### Compressor 策略

第一版 compressor 保持简单。

```text
SearchCompressor
  按文件分组。
  每个文件保留 first/last 和少量高价值命中。
  保留 path、line_number、match_count、omitted_count。

LogCompressor
  保留错误、失败、traceback、summary、首个错误、最后错误。
  warning 去重。
  普通 INFO/DEBUG 大量省略。

DiffCompressor
  保留文件列表、hunk header、增删行统计、关键 +/- 行。
  大 diff 只保留每个文件前几个 hunk。

JsonCompressor
  输出 top-level shape、key 列表、item_count。
  对数组保留 first/last、error-like item、异常 item。

CodeCompressor
  第一版不做 AST。
  保留 imports、class/function 签名、注释摘要、关键 TODO/error 行。
  函数体可用 placeholder 表示。

TextCompressor
  保留标题、列表、路径、错误行、TODO、首尾少量内容。
```

### Route Compact 输出

Route compact 输出可以写入 checkpoint 的结构字段，也可以作为 compaction part。

```python
@dataclass(slots=True)
class RoutedCompressionResult:
    content_type: str
    compressed: str
    summary: str
    original_tokens: int
    compressed_tokens: int
    stats: dict[str, Any]
```

metadata 建议：

```json
{
  "compacted_by": "route_compact",
  "content_type": "build_output",
  "original_tokens": 18420,
  "compressed_tokens": 1200,
  "stats": {
    "errors_kept": 3,
    "warnings_kept": 5,
    "lines_omitted": 740
  }
}
```

### 保护规则

借鉴 Headroom 的 tag protector 思路，压缩前要保护结构化标签和内部标记。

不要压坏：

```text
archive_id
tool_call_id
XML-like internal tags
code fences
file paths
line numbers
traceback frame
diff hunk header
```

如果 compressor 不能保证这些信息不丢，应该降级为 Archive placeholder，而不是强行压缩。

## L4 LLM Compact

只有前三层达不到目标预算时才调用。

summary 必须包含：

```text
当前目标
用户约束
已完成
关键决策
相关文件
错误和修复
未完成任务
下一步
必要 archive_id
```

LLM compact 成功后写 `Checkpoint`：

```text
summary
tail_start_message_id
open_tasks
touched_files
decisions
next_step
```

## 重试、兜底、熔断

### 重试

```text
prompt_too_long
  扩大 prefix 裁切范围后重试，最多 3 次。

timeout / incomplete stream
  指数退避重试，最多 2 次。

no_summary
  换更严格 summary prompt 重试 1 次。

user_abort
  不重试。

api_error
  按 provider 错误类型决定是否重试。
```

### 兜底

如果高级压缩失败：

```text
先 archive 大结果
再 micro compact 旧结果
再缩短 tail
再生成 checkpoint
最后提示用户手动 clear/compact
```

原则：

```text
程序化压缩优先，LLM 压缩最后。
```

### 熔断

自动 compact 连续失败后停止自动尝试。

```python
if consecutive_auto_compact_failures >= 3:
    disable_auto_compact_for_session = True
```

记录：

```text
failure_count
last_failure_reason
disabled_until
```

手动 `/compact` 不受自动熔断影响，但需要显示失败原因。

## ContextBuilder

ContextBuilder 每次请求前投影当前上下文。

如果没有 checkpoint：

```text
stable system prefix
全部有效 messages/parts
```

如果有 checkpoint：

```text
stable system prefix
latest_checkpoint.summary
messages_after(tail_start_message_id)
archive placeholders
```

这等价于 Claude Code 的“从最新 compact boundary 之后投影”，但 FirstCoder 用 checkpoint 数据结构表达。注意：

```text
任务 hash 切换不会自动改变 tail_start_message_id。
只有 L4 checkpoint 成功后，才允许更新压缩投影边界。
L1/L2/L3 只更新 part 状态、placeholder、archive 或 compaction part，不移动 checkpoint tail。
```

ContextBuilder 不负责：

```text
不压缩
不总结
不落盘
不更新 checkpoint
不决定 system prompt fingerprint 是否失效
```

它只接收 `SystemPromptBuilder/PromptPrefixCache` 给出的稳定前缀，然后拼接 conversation projection。

## Resume

resume 走简单顺序恢复。

```text
1. 读取 session
2. 读取 messages + parts
3. 找到 latest checkpoint
4. 计算 system prompt fingerprint
5. 如果 fingerprint 命中，复用 stable system prefix；否则重建
6. 加载 checkpoint.summary
7. 加载 tail_start_message_id 之后的 messages
8. 恢复 archive placeholders
9. 继续 append 新消息到同一 session
```

第一版不做：

```text
不做中间 snip
不做 parentUuid relink
不做复杂多分支恢复
不自动展开 archive 原文
```

## CompactionEvent

每次压缩都写事件，方便调试和复盘。

```python
@dataclass(slots=True)
class CompactionEvent:
    id: str
    session_id: str
    reason: str
    levels_attempted: list[str]
    stopped_at: str | None
    before_tokens: int
    after_tokens: int
    target_tokens: int
    input_fingerprint: str
    source_part_ids: list[str]
    output_part_ids: list[str]
    checkpoint_id: str | None
    strategy_version: str
    llm_used: bool
    success: bool
    error: str | None
    created_at: str
```

示例：

```json
{
  "reason": "task_hash_changed",
  "levels_attempted": ["micro", "archive"],
  "stopped_at": "archive",
  "before_tokens": 92000,
  "after_tokens": 61000,
  "target_tokens": 70000,
  "llm_used": false,
  "success": true
}
```

## TaskHashEvent

hash 工具每次调用都记录事件。

```python
@dataclass(slots=True)
class TaskHashEvent:
    id: str
    session_id: str
    decision: str
    basis_message_id: str
    active_hash: str
    candidate_hash: str | None
    stable_count: int
    confirmed_changed: bool
    triggered_compaction: bool
    created_at: str
```

## 开发优先级

### P0：顺序存储、系统前缀和上下文构造

```text
models.py
store.py
system_prompt.py
context_builder.py
token.py
```

目标：

```text
能 append Message/Part
能读取 session
能构造 stable system prefix
能用 fingerprint 判断是否复用 system prompt
能构造 provider messages
能估算 token
```

### P1：Archive 和大工具结果

```text
archive.py
tool_result size guard
archive placeholder
```

目标：

```text
大工具结果不会完整塞进上下文
archive_id 可追溯
```

### P2：Task Hash 工具

```text
task_boundary.py
propose_task_boundary 工具
稳定窗口
TaskHashEvent
```

目标：

```text
模型通过工具提出 same/new/uncertain
程序生成稳定 hash
确认切换后能触发 compaction pipeline
```

### P3：Micro / Archive / Route 三层程序化压缩

```text
compaction.py
content/router.py
content/detector.py
content/compressors.py
CompactionEvent
```

目标：

```text
自动压缩优先不调用 LLM
每层压缩后能重新估算并决定是否停止
```

### P4：Checkpoint 和 Resume

```text
checkpoint.py
latest checkpoint
tail_start_message_id
resume context projection
```

目标：

```text
旧历史由 checkpoint summary 代表
recent tail 原样保留
resume 能继续同一 session
```

### P5：LLM Compact 和工程保护

```text
LLM summary prompt
prompt_too_long retry
timeout retry
auto compact circuit breaker
fallback policy
```

目标：

```text
前三层失败时可兜底
自动 compact 不会死循环
失败原因可解释
```

### P6：调试视图

```text
inspector.py
/context
/compact status
```

报告：

```text
session_id
active_task_hash
system_prompt_fingerprint
system_prompt_cache_hit
latest_checkpoint
tail_message_count
estimated_tokens
archive_count
compaction_events
auto_compact_disabled
last_failure_reason
```

## 测试优先级

```text
test_store.py
test_system_prompt.py
test_context_builder.py
test_archive.py
test_task_boundary.py
test_compaction_pipeline.py
test_checkpoint.py
test_resume.py
test_retry_policy.py
test_circuit_breaker.py
test_inspector.py
```

关键测试：

```text
system prompt 不写入普通历史
system prompt fingerprint 不变时复用 stable prefix
AGENTS.md 变化会导致 fingerprint 变化
工具 schema 变化会导致 fingerprint 变化
普通消息追加不会导致 fingerprint 变化
大 tool result 会 archive
archive placeholder 包含 archive_id 和 summary
hash 工具 same/uncertain 不切换 active_hash
hash 工具 new 需要稳定窗口确认
确认 hash 切换后触发 compaction pipeline
pipeline L1 达标时不进入 L2/L3/L4
pipeline L3 达标时不调用 LLM
LLM compact 成功后写 checkpoint
resume 使用 latest checkpoint + tail
resume 时 fingerprint 命中则复用 stable prefix
自动 compact 连续失败 3 次后熔断
手动 compact 不受自动熔断影响
```

## 第一轮最小闭环

第一轮只做：

```text
Session / Message / Part JSONL 存储
SystemPromptBuilder
PromptPrefixCache
ContextBuilder
TokenEstimator
Archive 大结果落盘
Checkpoint resume 的数据结构
```

第一轮目标：

```text
system prompt fingerprint 稳定时可复用
工具输出很大时自动落盘
上下文只放 placeholder
resume 时不展开原文
```

第二轮再做：

```text
Task Hash 工具
稳定窗口
四层 compaction pipeline
CompactionEvent
```

第三轮做：

```text
LLM checkpoint compact
retry / fallback / circuit breaker
/context 调试视图
```
