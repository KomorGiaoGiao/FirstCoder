# 上下文管理设计

[English](CONTEXT_MANAGEMENT_DESIGN.md)

## 问题是什么

长编码会话同时需要两件互相拉扯的事：完整保留每个关键事实以便审计/恢复，又要给模型一个有限且协议合法的工作上下文。FirstCoder 把**持久事实**和**本次 provider 投影**分开：压缩改变后者，不破坏性重写前者。

它不是泛用聊天摘要器。下一步改代码需要的精确源码、tool-call transaction、错误证据，必须被保住。

## 一个 Session 有三层视图

```text
append-only JSONL event
  -> replay -> SessionView（FirstCoder 的有效事实）
  -> ContextBuilder -> ChatMessage[]（本次 provider 请求）
  -> provider
```

- `JsonlSessionStore` 读写 `<data-root>/sessions/<id>.jsonl`；
- replay 将追加的 replacement/checkpoint event 应用成 `SessionView`，raw event 仍可读；
- 只有 `ContextBuilder` 能把 `SessionView` 投影成 provider messages。存在 checkpoint 时先插摘要，再发 tail，并校验工具顺序。

`system_meta` 是内部状态，故意不会作为普通对话消息投影；稳定 system prefix 由 session 另外提供。

## 一个长会话故事

1. agent 读了源码，并运行一条很长的测试命令。
2. assistant 调用和 tool result 都追加为 event，`SessionView` 同时拥有两者。
3. `ContextWindowManager` 发现 token/输出压力。
4. 确定性的 L1–L3 裁剪/归档旧材料，并追加 `compaction_completed` replacement event。
5. 若仍超预算，L4 请求模型生成 coding handoff，追加 `checkpoint_created`。
6. 下一次请求中，builder 发 checkpoint 摘要加一段合法的未压缩 tail，而非全量 raw log。
7. resume 重新 replay 同一批 event，不依赖内存里一份神秘 transcript。

## 不可违反的约束

1. 事实和 archive 原文 append-only。
2. provider 投影不能以孤立的 `role=tool` result 开头。
3. 每个 tool result 保持原有 `tool_call_id` 配对。
4. 最新用户意图和 fresh 的已识别源码读取，不能进入有损 L1–L3。
5. L2 有损输出在 replacement 落盘前必须先 archive 原文。
6. L1–L3 是确定性的；L4 才是唯一语义模型摘要层。
7. compaction replay/resume 必须幂等，不能重复 archive、重复 replacement 或将 archive 展回去。

破坏第 2/3 条会让 provider API 拒绝消息历史；破坏第 4 条更隐蔽：模型可能在陈旧或缺失源码上下文下写错 patch。

## 压缩分层

```text
effective tail -> lifecycle classification
  -> L1 裁掉旧任务文本
  -> L2 按类型、可恢复地压缩工具输出
  -> L3 用 archive placeholder 移出上下文
  -> 仍不够才 L4 coding-handoff checkpoint
```

### L1：裁掉确认属于旧任务的文本

L1 只标记旧 task 的安全普通文本，不动 tool transaction、最新用户输入、含 tool call 的 assistant message。投影时跳过 trimmed text，整段 tail 最多加一条 `[Earlier dialogue trimmed]`。

### L2：压缩 derived 输出，并保留可恢复副本

L2 只处理 lifecycle 允许的 derived output：log、search、diff、JSON、HTML、列表等。按类型压缩时要保留有用形状（失败测试/error block、路径/行号、diff header、JSON status），候选必须严格比原文小；随后 `ToolResultArchive` 先存全量原文。

### L3：以 placeholder 替换旧材料

L3 用有界 placeholder 替换选中的 tool result，绝不移除整个 tool transaction。已知 mutation 后的 stale source read、被后续读取覆盖的 source read、重复 derived output、旧/大 derived output 都可能进入；fresh recognized source read 和当前 turn retrieval 受保护。

`retrieve_archive` 是按 session 注入的工具，只接收 archive id、可选字面 query、受限 `max_chars`；它不能变成任意文件系统读取或跨 session 调取。

### L4：模型生成 handoff

确定性压缩仍不够时，`LlmCompactService` 生成结构化 handoff：目标、约束、决定、文件、命令、错误、下一步。尾边界由本地代码校验后才能写 checkpoint。摘要是工作线索，不是 append-only 证据的替代品。

## 生命周期判定必须保守

判定器看结构化 tool arguments/result data，不从展示文本猜。

| 状态 | 含义 | 处理 |
| --- | --- | --- |
| `fresh` | 已知源码读取仍是当前版本 | 保留精确内容 |
| `stale` | 后续已知成功 mutation 动过同一路径 | L3 候选 |
| `superseded` | 后续已知读取覆盖了它 | L3 候选 |
| `derived` | log/search/diff 等非源码输出 | 先 L2，必要时 L3 |
| `duplicate` | 旧 derived 与后一个结果完全相同 | 复用 backing/L3 候选 |

未知工具、模糊 metadata、部分读取、shell 输出都 fail-open：不猜它们是 source mutation/read。压缩率低一点没关系，把唯一正确源码证据拿走才是真寄。

## Trigger 与失败处理

`ContextWindowManager` 负责时机与升级：

| Trigger | 含义 |
| --- | --- |
| `AUTO` | token/tail/output heuristic 到阈值 |
| `TASK_HASH_CHANGED` | 确认任务切换，强制清理旧 derived context |
| `MANUAL` | 用户主动 compact/inspect |
| `PROMPT_TOO_LONG` | provider 拒绝请求，阻塞式恢复后做有界 retry |

manager 先跑确定性层，记录结果，必要时才调用 L4。自动压缩 circuit breaker 会防止昂贵失败反复触发；manual、task-boundary、overflow recovery 不会被它静默跳过。

## 源码地图

| 关注点 | 从这里开始 |
| --- | --- |
| JSONL replay/effective view | `context/store.py`、`context/models.py` |
| provider 投影/tool 合法性 | `context/context_builder.py`、`context/tool_sequence.py` |
| 确定性 L1–L3 | `context/compaction.py`、`context/tool_lifecycle.py`、`context/content/` |
| archive/retrieval | `context/archive.py`、`tools/retrieve_archive.py` |
| trigger 与 L4 升级 | `context/manager.py`、`context/triggers.py` |
| checkpoint/retry | `context/llm_compact.py`、`context/provider_summarizer.py` |
| 可回放 runtime facts | `context/runtime_state.py`、`context/runtime_replay.py` |

## 最小验证

```sh
.venv/bin/python -m pytest tests/test_context_builder_new.py \
  tests/test_context_compaction_pipeline.py tests/test_context_window_manager.py \
  tests/test_context_llm_compact.py tests/test_context_resume.py \
  tests/test_context_archive.py -q
```

改一个层级时，同时测它节省了什么和绝不能改什么：fresh source protection、archive recovery 边界、tool-call 序列合法、replay/resume 幂等。

## 常见错误

- **为了省 token 直接删 JSONL：** 审计性没了；应追加 replacement/checkpoint event。
- **先摘要、后确定性压缩：** 无谓花模型钱，还更易丢结构。
- **checkpoint 切断 tool pair：** provider 历史立刻不合法。
- **把 placeholder 当数据丢失：** 应使用 session-scoped retrieval tool，原文仍在 archive。
- **源码长就压缩：** 那可能正是 coding agent 写 patch 需要的证据。

关联：[Agent 主循环护栏](AGENT_LOOP_GUARDRAILS.zh-CN.md)、[工具设计](TOOLS_DESIGN.zh-CN.md)、[Provider 设计](PROVIDERS_DESIGN.zh-CN.md)。
