# 上下文管理设计

[English Version](CONTEXT_MANAGEMENT_DESIGN.md)

> 状态：这是已落地的上下文压缩 v2 行为契约。运行时按下述 lifecycle gate 执行 L1-L4 压缩。为保证 session resume 兼容，旧 archive record 与 placeholder 仍可读取；它们不定义新的 compaction 写入行为。

## 目的

FirstCoder 必须在长编码任务中减少上下文占用，同时保留下一步真正需要的精确源码、诊断信息和 tool-call 结构。因此上下文系统承担两件事：

1. 保存可审计、可恢复的 session 事实；
2. 从这些事实投影出更小、且 provider 协议合法的工作上下文。

v2 实现按工具结果的生命周期压缩：对当前源码读取保持保守；让有损工具输出压缩可恢复；只有确定性步骤无法满足预算时，才升级到 L4 的 LLM checkpoint。

它不是通用的对话摘要器。

## 架构基础

### Append-only 事实与有效视图

`JsonlSessionStore` 是持久化事实的唯一来源。原始消息事件绝不原地修改：压缩通过追加 `compaction_completed` replacement event 表达，L4 通过追加 `checkpoint_created` event 表达。重放这些事件后得到 `SessionView`，即有效会话视图。

`SessionRuntimeState` 保存不属于自然语言消息、但可由事件回放的运行时事实，包括 active task hash、任务边界稳定性、checkpoint 标识、压缩历史和自动压缩熔断状态。

`ContextBuilder` 是将 `SessionView` 投影为 provider messages 的唯一组件，必须维持 provider 所要求的序列：

```text
assistant(tool_call) -> tool(tool_result)
```

无论压缩还是 checkpoint，都不能在可投影 tail 中留下孤立 tool result 或未完成的 tool call。

### 职责边界

| 组件 | 负责什么 | 不负责什么 |
|---|---|---|
| `context/tool_lifecycle.py` | 确定性地将 tool result 判为 fresh、stale、superseded、derived 或 duplicate。 | archive I/O、token 策略、内容压缩。 |
| `context/compaction.py` | L1-L3 顺序、生命周期 gate、token 预算与 replacement event。 | provider 请求、各内容类型的具体解析。 |
| `context/content/*` | 内容类型检测与确定性压缩候选。 | 生命周期推断、session 存储、取回。 |
| `context/archive.py` | session-local 原文 backing store、archive metadata 与 placeholder。 | 工具注册、压缩选择。 |
| `tools/retrieve_archive.py` | 模型可见、受限的 archive 恢复。 | 直接文件路径访问、生命周期判定。 |
| `context/manager.py` | trigger 策略、target 选择、L1-L4 升级与结果落盘。 | 工具执行、checkpoint 摘要生成。 |
| `context/llm_compact.py` | L4 checkpoint 生命周期与合法 tail 校验。 | 确定性 L1-L3 变换。 |
| `context/context_builder.py` | provider 投影、checkpoint summary 插入、tool sequence 校验。 | 持久化修改、压缩决策。 |

这些边界用于避免出现第二套 archive、第二条 compaction pipeline，或把 provider 细节泄漏到 session 存储层。

## 不可违反的约束

- 原始 JSONL event 和 archive 原文保持 append-only。
- replacement 必须保留 `message_id`、`part.id`、`tool_call_id`、工具名、顺序和成功/错误 metadata。
- system/developer 指令、最新用户需求、稳定 system prefix、当前任务的精确源码读取，不能成为有损 L1-L3 输入。
- 任一被接受的有损 L2 tool-result 变换，在 replacement 持久化前必须已有原文 backing record。
- 未知 read/mutation 形状必须 fail-open：绝不能借此把源码读取标记为 stale 或 superseded。
- 模型只能在当前 session（或 fork 后复制出的 session）中读取 archive 原文。
- L1-L3 不调用模型；L4 是唯一的语义摘要层。
- resume 后再次 compact 必须幂等：不得生成第二个 archive、重复 replacement 或展开 archive 内容。

## 压缩流水线

```text
effective tail
  -> lifecycle index
  -> L1：安全裁掉旧任务普通对话
  -> L2：按类型、可逆地压缩 tool result
  -> L3：选择并替换为 archive placeholder
  -> L4：仍超预算时才生成 coding handoff checkpoint
```

pipeline 只处理最新 checkpoint 之后的 effective tail；已经被 checkpoint 覆盖的 raw history 不得被再次压缩。

### L1：旧任务普通对话裁切

L1 是有意遗忘，不是摘要。它只裁切已确认属于旧任务的普通 `text` part；不会触及 tool result、tool-call transaction、最新用户消息，或包含 tool call 的 assistant message。

被裁掉的 part 在 raw event 中仍保留，但在有效视图中标记 `compaction_state="trimmed"`。`ContextBuilder` 不投影 trimmed text，并且对整个 tail 最多插入一次 `[Earlier dialogue trimmed]`；绝不能每个 trimmed part 各插一条提示。

L1 不能靠关键词启发式删掉“当前任务中看似很旧”的对话。当前任务语义需要收缩时，L4 才是安全兜底。

### L2：类型化、可逆的工具结果压缩

L2 只接受生命周期允许的 `tool_result`，通常是 `derived` 输出。它先做确实能变小的低风险格式整理，再让既有 `RouteCompactRouter` 生成一个内容类型候选；候选只有严格小于原文时才能被采用。

有损候选替换 raw tool result 之前，必须先由 `ToolResultArchive` 保存原始内容。provider 看到的是紧凑结果，而模型可稍后用 `retrieve_archive` 恢复原始结果。

路由层保留结构，不把内容改造成自由 prose：

| 内容 | 必须保留 |
|---|---|
| search result | 路径、行号、代表匹配、省略计数 |
| build/test log | 摘要、失败项、error block、traceback/stack 邻近上下文 |
| unified diff | 文件/hunk header、增删行、有限上下文 |
| JSON | 可解析的紧凑结构、error/status 字段、schema 与代表项 |
| HTML | title、heading、可见正文、链接 |
| 列表和目录输出 | item 类型/表头、总数、代表路径、截断信号 |

L2 不压缩已识别为 fresh 的源码读取。既有 SourceCodeRouteCompressor 仍可处理“长得像代码的 derived 输出”，但不能绕过 lifecycle gate。

### L3：从 prompt 移出，并支持取回

L3 将选中的 tool result 替换为短 archive placeholder；它不删除原文，也不将 tool result 从 transaction 中移除。

以下结果必须成为 L3 候选：

- 成功的已知 mutation 之后，同路径的 stale source read；
- 更晚已知 read 覆盖同一路径/范围之后的 superseded source read；
- effective tail 中与更晚 derived result 内容 hash 相同的旧 duplicate result。

如果 L2 未达到单条预算，或整体仍然超过 target，体积大或较旧的 derived result 也可进入 L3。fresh source read 与当前 turn 的 retrieval result 永远不是 L3 候选。

新的 placeholder 有严格大小限制，只包含 archive id、工具名、状态、lifecycle、原始 token 数、确定性的短 summary、有限 error 行和 retrieval 提示。它刻意不携带任意原文 preview，尤其不能暴露已经 archive 的源码开头。

### L4：coding handoff checkpoint

只有 L1-L3 仍不能满足当前 target，或 prompt-too-long 的 blocking pass 仍不能满足 blocking target 时才调用 L4。`LlmCompactService` 仍是唯一的 checkpoint writer。

摘要器生成固定的 coding handoff：

```text
## 当前目标
## 已知事实与硬约束
## 已确认的决定及理由
## 相关文件与当前实现状态
## 已运行命令及有效结果
## 当前错误与未解决事项
## 下一步（可立即执行）
```

标题是刻意固定的中文字符串；某项没有证据时写 `无`。模型只生成摘要正文。checkpoint tail 的选择与校验仍由本地代码完成，以确保从合法 tool-call transaction 边界开始。

## 工具结果生命周期

生命周期是在 effective tail 上进行的确定性扫描。它通过 `tool_call_id` 将 tool result 和前面的 tool call 关联；只使用结构化 tool arguments 与 `ToolResult.data`，不从展示文本猜路径。

| 状态 | 含义 | 允许动作 |
|---|---|---|
| `fresh` | 已知源码读取之后没有更晚覆盖读取，路径也没有已知成功 mutation。 | 保留精确内容；不进 L2/L3。 |
| `stale` | 已知成功 mutation 在读取之后触及了源码路径。 | 直接在 L3 archive。 |
| `superseded` | 更晚的已知源码读取覆盖了之前读取的路径/范围。 | 直接在 L3 archive。 |
| `derived` | search、log、diff、JSON、HTML、列表、未知工具等所有非源码读取输出。 | 先 L2，必要时 L3。 |
| `duplicate` | 一个较旧 derived result 与 effective tail 中较晚 derived result 的完整内容 hash 相同。 | 复用 backing，并在 L3 archive。 |

第一版支持范围刻意较窄：

- source read：成功的 `view`，以及带有效结构化文件 metadata、且未截断的成功 `read_multi`；
- mutation：成功的 `write`、`edit`、`delete` 和结构化 `apply_patch` 结果；
- shell command、未知工具、metadata 异常、部分 `read_multi`、范围不明确等，不推断为 source read 或 source mutation。

这个保守边界比压缩率更重要：错误地标记 stale 可能会移走模型写 patch 正需要的精确文件内容。

## Archive 与 Retrieval 契约

`ToolResultArchive` 是 session-local backing store 的唯一 owner，路径为：

```text
<store.root>/archives/<session-id>/<archive-id>.txt
<store.root>/archives/<session-id>/<archive-id>.json
```

新 archive id 由原始 UTF-8 完整 SHA-256 content-addressed 生成。同一 session 中相同内容复用同一份 backing record。backing metadata 保存 hash、大小、token 估算、创建时间和 archive schema version；某个 part 的 lifecycle/source 信息留在 replacement part metadata，不能写入被多处复用的 shared record。

archive 读取只能由合法 session id 与 archive id 推导路径，绝不信任模型传入路径或历史 `archive_path` 字段。新写入要原子化，复用现有 content-addressed 文件前必须校验内容；旧 archive id 与旧 placeholder 仍要能被 resume 读取。

`retrieve_archive` 是 session-scoped 工具，不是普通 filesystem tool：

```text
retrieve_archive(archive_id, query=None, max_chars=6000, full=False)
```

- `max_chars` 限制为 1–12,000；
- query 返回字面匹配行及小范围相邻行，并带行号；
- `full=True` 只返回开头的受限片段，并明确截断状态；
- archive 不存在、id 非法、跨 session 或 hash 不一致时，安全报错且不泄露文件路径；
- retrieval 输出在产生它的当前 turn 内 pinned，避免 auto compact 立刻又把它收起。

该工具由 `create_session_tool_registry` 注入，因为它需要当前 session id 与 store root；不得注册进无状态 `create_builtin_registry`。

## Trigger 与预算策略

`ContextWindowManager` 仍是唯一的 trigger 与升级入口。

| Trigger | 必须行为 |
|---|---|
| `AUTO` | 沿用 token/tail/tool-output trigger；按 L1 -> L2 -> L3，仍超 target 才 L4。 |
| `TASK_HASH_CHANGED` | 使用更低 task-switch target，并强制执行 L2/L3，即使 L1 已达到普通 target；用于清理旧任务的 derived context。 |
| `MANUAL` | 使用普通 target 执行确定性压缩，但不能突破 fresh-source 保护。 |
| `PROMPT_TOO_LONG` | 执行一次 blocking deterministic pass；必要时 L4；原 provider 请求最多重试一次。 |

AUTO 可以遵守 circuit breaker；手动压缩、任务边界和 provider overflow 恢复不得被它跳过。

预算只定义在 `ContextCompactionConfig`：普通 target、可选 blocking target、task-switch target 与 L2 单条结果 target。compressor 和 archive/retrieve 工具都不应内嵌全局 token 策略。

## Resume、Fork 与可观测性

压缩事实继续通过 `compaction_completed` 写入，并重放成 replacement part。event 新字段必须保持加性兼容：旧 session 没有 lifecycle metadata 时，在运行时保守分类；旧 archive placeholder 仍可投影。

fork session 时，archive 目录复制到新 session id，与 JSONL 一起 fork。archive 是 session-local；不引入跨 session dedup 或共享可变 memory。

compaction event 应足以诊断 token 节省而无需重解析内容：按层 before/after token、变更计数、lifecycle 计数、archive ids/counts、trigger 与 target。既有 inspector、catalog、transcript 与 runtime replay 应将它们作为加性字段消费。

### 只读评估指标

评估层直接从 session transcript 派生指标（JSONL event、应用 replacement 后的有效视图，以及该 session 的 archive 目录）。它是只读的：不新增第二个 state store、不修改 transcript，也不维护独立的 archive index。

| 指标组 | 派生值 |
|---|---|
| 压缩节省 | 总 before/after token、总节省，以及 level metrics 存在时按 L1/L2/L3 的节省。 |
| Archive 占用 | 当前 session 的 archive 原文 `.txt` payload 数量与字节数。 |
| 恢复与 handoff | 成功的 `retrieve_archive` 执行次数，以及成功完成的 L4 event 次数。 |
| Tool-output 构成 | 按 transcript 顺序统计的 tool-result 工具名计数，以及有效 replacement 的 content-type 计数。 |
| Source reread | 按 transcript 顺序重复出现的、成功且结构化的 `view`/`read_multi` source target。 |

这些字段均为加性且向后兼容。旧 event 缺少字段、archive 不存在或 tool metadata 无法识别时，指标只返回可得的部分或零值；不要求迁移，也不改变 compaction 行为。

## 外部参考的边界

本设计只从 Headroom 借鉴三点：内容感知路由、压缩工具输出的可逆本地 backing、按需 retrieval。不会嵌入 Headroom，也不会引入它的 proxy、MCP server、ML/学习型压缩、SQLite、跨 agent memory、TTL/LRU 驱逐、provider request rewrite 或 provider cache 优化。

FirstCoder 已经拥有 agent loop、session event log、provider projection 和 tool registry；复用这些边界能保持设计 local、可 replay、provider-neutral。

## 验证范围

实现行为由聚焦的 context/session/agent 测试验证。全仓 pytest 与 benchmark 仍是独立的发布检查，因为它们可能依赖可选 benchmark fixture、provider 或外部任务环境。

核心验证条件为：

- fresh `view` / `read_multi` 源码读取不会只因内容大被有损压缩；
- stale 与 superseded 读取会 archive 且可恢复；
- derived output 先走类型化 L2，再按需要 L3 驱逐；
- 任一 L2 变换都有原文 backing，且 `retrieve_archive` 受限、session-safe；
- task switch 即使低于普通 auto threshold 也会执行 L2/L3；
- 每种 L1-L4 结果之后，provider projection 都保持 tool sequence 合法；
- resume/fork 幂等且向后兼容；
- 聚焦的 context/session/agent 测试保持 provider 合法的 tool transaction 与 resume/fork 兼容性。
