# FirstCoder 面经：项目深挖问答

适用方向：Python / Go 后端、AI Agent / LLM 应用开发实习。内容以当前 FirstCoder 实现为准，回答时优先讲设计目标、关键取舍和边界；被追问时再展开实现细节。

## 1. 先用一分钟介绍项目

> FirstCoder 是一个本地运行的开源 Coding Agent，提供 Textual TUI、工具调用、文件写前 diff 审查、分级权限、会话恢复、多 Provider 和上下文压缩能力。我的核心设计是把完整的会话事实与每次发给模型的上下文分开：完整事实以 append-only JSONL 保存，保证可审计和可恢复；模型上下文则通过压缩、归档和 checkpoint 控制长度，并保证工具调用协议合法。

不要只说“我做了一个调用大模型的终端工具”；要先说明你解决的是长会话、工具副作用、安全确认和恢复这几个工程问题。

## 2. JSONL、SessionView、ContextBuilder 与 resume

### 问：为什么用 append-only JSONL，不用一份覆盖式 JSON？

> JSONL 记录会话的过程事实，例如用户消息、模型回复、tool call、tool result、权限确认、任务计划更新、压缩事件和 checkpoint。追加式日志保留了因果顺序，便于审计、恢复和排障；覆盖式状态文件容易丢掉中间过程，也难以判断一次工具调用究竟是否已经执行完成。

### 问：为什么不能把 JSONL 原样发回模型？

> JSONL 是完整审计账本，不是直接给 Provider 的上下文。一方面长会话会超出上下文窗口，旧任务的大工具输出也会干扰当前推理；另一方面 Provider 对工具调用的消息顺序有严格要求。因此我把完整事实和模型可见上下文分开。

### 问：SessionView 是什么？

> SessionView 是把 JSONL 回放后得到的“当前会话事实快照”。日志像流水账，SessionView 则统一告诉系统当前有哪些有效消息、工具调用与结果、TaskPlan、checkpoint、归档和压缩替换。TUI、resume、压缩、分享和 ContextBuilder 都基于它工作，避免各模块分别解释日志造成不一致。

类比：JSONL 是事件流水；SessionView 是事件回放得到的当前业务状态。

### 问：ContextBuilder 是什么？会发什么给模型？

> ContextBuilder 只做投影，不做存储和压缩。它从 SessionView 构造一次 Provider 可接受的消息序列：稳定的 system prompt、当前 TaskPlan 等运行指令、checkpoint 摘要（如果有）、checkpoint 后的真实 tail，以及最新用户输入。它还会在发送前校验 assistant 的 tool call 与 role=tool 的 result 是否严格配对。

它不会默认发：完整 JSONL、checkpoint 前的完整历史、已归档工具结果的原文、内部 `system_meta`、计数器或权限对象。

```text
append-only JSONL
  -> replay
SessionView（系统当前事实）
  -> ContextBuilder
Provider messages（本次模型上下文）
```

### 问：resume 时究竟发生什么？

> `/resume` 会读取并回放完整 JSONL，恢复有效消息、TaskPlan、checkpoint、归档、已消费工具结果、运行时任务边界状态和已知消息 ID，并重新装配当前可用工具、权限策略和 session 环境。它本身不立刻调用模型。用户继续输入后，ContextBuilder 才把 checkpoint 摘要加最近真实 tail 投影为本次模型上下文。

> checkpoint 不是恢复的存储边界。resume 用完整日志恢复系统事实；checkpoint 只是下一次模型请求的轻量上下文入口。

### 问：上次正等用户确认写文件，程序退出怎么办？

> 恢复时会识别会话尾部尚未闭合的权限工具调用，重建为待确认状态；不会因为程序重启而自动执行写操作。如果结果未知，会明确记录为中断/未知，而不是伪造成功。

## 3. 工具调用、权限与写前审查

### 问：完整讲一下一次工具请求到执行的链路。

> 模型先返回 tool call。系统校验工具是否存在、参数是否符合 schema、路径是否符合工具规则且处于沙箱范围内。对于声明了权限需求的工具，系统构造 PermissionRequest，其中包含操作类型、目标路径或命令、工作目录、工具名和原始参数等元信息。
>
> PermissionManager 先匹配用户已有 grant，再调用 DefaultPermissionPolicy 得出 allow、ask 或 deny。ask 会暂停 Agent Loop 并由 TUI 展示确认；对于 write、edit、apply_patch、delete，系统会基于当前真实文件内容预演出可信 diff 和文件快照。用户确认后会再次比较快照，若文件在确认期间被其他进程改过，diff 作废，必须重新生成。通过后才执行原始 tool call。
>
> 最后，无论成功、失败、拒绝或 diff 过期，都会生成唯一的 ToolResult，和原始 tool call 持久化配对；下一轮模型看到结果后再决定如何继续。

口诀：**校验 → 权限 → 审查 → 二次校验 → 执行 → 结果回传模型。**

### 问：PermissionRequest 长什么样？

```python
PermissionRequest(
    id="perm_xxx",
    action=PermissionAction.WRITE_PATH,
    target="firstcoder/app/tui.py",
    reason="编辑文件需要用户确认。",
    cwd=Path("/project"),
    metadata={
        "tool_name": "edit",
        "arguments": {"path": "...", "old": "...", "new": "..."},
        "allow_always": True,
        "allow_auto": True,
    },
)
```

`action` 可为 `read_path`、`write_path`、`delete_path`、`execute_shell`、`network_request`、`read_env`、`mcp_tool` 等。

### 问：allow / ask / deny 在哪里生效？为什么不能只靠 prompt？

> 这是模型外的硬门禁。模型 tool call 进入 PermissionAwareToolRegistry 后，PermissionManager 先查已有 grant，再由 DefaultPermissionPolicy 根据动作、目标、cwd、敏感性和模式判断 allow / ask / deny。prompt 只是软约束，模型可能误判或被 prompt injection 诱导；安全决策必须在工具真正执行前由程序做。

当前默认策略可这样举例：

- 项目内普通读取通常允许；
- 标准模式的文件写入、删除、shell、网络和 MCP 通常要求确认；
- 读取含 `KEY / TOKEN / SECRET / PASSWORD / COOKIE` 的环境变量直接拒绝；
- 删除项目根目录外路径直接拒绝；
- `.env`、`.pem`、`.key`、`.git` 等敏感路径会提升为确认；
- shell 控制符、管道、重定向、命令替换会被识别并提高风险等级。

不要说“正则拦住所有危险命令”。正则是辅助风险识别；真正的边界是工具前的权限策略、沙箱和用户确认。

### 问：diff 审查能防什么？不能防什么？

> 它能让用户审查基于真实文件计算出来的变更，也能通过执行前快照复核降低“用户审查 A 版本、执行时覆盖 B 版本”的并发覆盖风险。它不能替代操作系统级原子事务，也不能可靠预演 shell、网络、数据库或子进程的所有副作用。

### 问：Shell 为什么不能像 edit 一样提前生成 diff？

> shell 的实际副作用依赖运行时环境、网络、脚本分支、子进程和外部服务，无法可靠计算唯一 diff。因此 FirstCoder 对 shell 使用权限策略、工作目录、超时和输出上限控制，并默认要求用户确认。

### 问：bypass 有什么意义？

> bypass 用于可信本地自动化、基准评测或隔离环境中减少高频确认。代价是保护明显降低，产品必须显著展示当前模式，并说明写入、shell、网络等可能直接执行；不应把它作为普通用户默认模式。

## 4. 工具结果的“已执行”与“已被模型消费”

### 问：为什么要区分工具成功和模型消费结果？

> 工具执行成功，不等于模型已经读到结果。例如 read 返回了很长的文件内容，结果已写进 JSONL，但下一次模型请求前程序崩溃。若系统把它当作可压缩的旧历史，恢复后模型可能永远没见过这个结果。
>
> 所以只有工具结果进入一次成功完成的后续 Provider 请求后，系统才追加 `provider_projection_consumed`，把它标记为已消费。请求失败、超时、取消或流式消息未完成，都不会写这个标记。未消费的 tool result 不允许被 L2、L3 或 L4 有损覆盖。

> 这避免了“工具确实执行过，但模型丢失自己从未看过的输出”。

## 5. 上下文压缩机制

### 设计原则

> 完整事实与模型上下文分离。JSONL 和归档原文持续保留；压缩只减少模型默认看到的内容，不破坏审计和恢复能力。

### 何时触发

主模型请求前会计算动态预算：模型窗口减去输出预留、system prompt、工具 schema 和有效历史。默认在接近输入容量高水位时触发，目标是降到更低水位；也支持手动压缩、确认任务切换时主动清理旧任务派生上下文，以及 Provider 返回 `prompt too long` 时的阻塞恢复。

### 四层压缩

```text
L1：裁旧任务普通文本
L2：按类型压缩派生工具输出
L3：归档并用 placeholder 替换旧工具结果
L4：仍不够才由 LLM 生成 checkpoint handoff
```

#### L1：旧任务普通文本

只裁确认属于旧任务的普通对话文本。不裁最近用户意图、带 tool call 的 assistant 消息、tool transaction 或当前任务需要的内容。投影时最多加一个 `[Earlier dialogue trimmed]` 标记。

#### L2：确定性、按类型的有损压缩

不是让 LLM 自由总结。程序优先根据工具名，再结合 JSON 解析和正则/格式特征进行内容路由：

| 类型 | 识别 | 保留内容 |
| --- | --- | --- |
| 搜索结果 | `grep` / `rg`、`path:line:text` | 按文件分组、路径行号、有限高价值命中 |
| Git diff | 工具名或 `diff --git` / `@@` | 文件头、hunk、全部增删行、少量关键上下文 |
| 构建/测试日志 | `FAILED`、`ERROR`、Traceback、pytest 等 | 错误块、栈帧、失败汇总、有限 warning |
| JSON | `json.loads()` 成功 | status、error、message、reason、traceback 等重要字段；头尾和高价值数组项 |
| HTML | `<html>`、`<body>` 等 | 可见结构/文本，减少标签噪声 |
| 源码/普通文本 | 代码行首规则或兜底 | 保守处理；当前 fresh source read 默认不做有损压缩 |

安全阀：内容太短不压；压缩后必须严格更短才替换；替换前先 archive 原文；记录内容指纹、类型、压缩器、压缩前后 token 等 metadata。

#### L3：archive placeholder

把低价值旧工具结果换成短占位信息，但不移除整个工具事务。候选包括：已被后续修改变 stale 的源码读取、已被新读取覆盖的结果、重复派生输出和旧的大型派生输出。模型仍看到工具调用及其 placeholder，Provider 协议保持合法。

原文会保存到 session-scoped archive。模型通过 `retrieve_archive(archive_id, query=...)` 主动取回匹配行窗口；也可请求受字符上限限制的原文。

当前不是 embedding/RAG 自动召回。占位包含 archive id、工具信息和检索提示；语义线索不足是当前的改进点，后续可补受控语义摘要和来源文件/命令元数据。

#### L4：checkpoint handoff

规则压缩仍不足时才调用 LLM 生成结构化 handoff，包含：当前目标、硬约束、决定及理由、相关文件、已运行命令、有效结果、当前错误和下一步。

checkpoint 不会立即落盘。系统必须先验证：

- tail 不会从孤立 tool result 开始；
- tool call/result 没被切断；
- 未消费工具结果没有藏入 checkpoint 前；
- 用“摘要 + tail”重建 Provider messages 后确实低于目标预算。

全部通过才写 `checkpoint_created`。下一次模型看到的是：

```text
system prompt
+ checkpoint 摘要
+ checkpoint 后真实 tail
+ 最新用户输入
```

### 任务边界触发压缩

> 对新用户输入做 `same / switch / uncertain` 的轻量边界判断。`same` 继续当前任务；`switch` 表示明显切换；`uncertain` 默认保守地不立即切换。为降低意图抖动，候选任务 hash 连续稳定观察后才确认切换；确认切换时会强制清理旧任务的派生上下文，而不是直接删掉旧事实。

为什么不用每轮压缩：压缩有成本且有损，当前任务未变时，最近工具输出和决策过程往往正是模型完成任务需要的上下文。

局限：短输入、多意图输入、用户中途回头修改旧需求可能误判；稳定窗口会引入少量滞后。若任务很短或模型窗口足够大，可以退化为只在接近预算阈值时压缩，或让用户显式 `/new`、`/compact`、`/fork` 控制边界。

## 6. TUI、流式输出与中断

### 问：哪些内容可以立即渲染，哪些要等完成？

> 文本 delta 可以即时渲染，以保证交互实时性；但不能因为流中出现了部分 tool call 参数就执行工具。必须等 Provider 的 `message_completed`，确认 assistant 消息和 tool call 参数完整后，才持久化 assistant 消息并执行工具。

### 问：Ctrl+C 要保证什么？

> 未完成的流式 assistant 消息不能伪装成完成；尚未开始的工具不能执行；已写入 tool call 但没有最终结果时要补“中断且结果未知”的闭合结果，防止 resume 后留下孤立 tool call，导致 Provider 协议非法。

### 问：为什么 TaskPlan 面板按 revision 去重？

> 流式文本、工具状态等会频繁刷新 UI。若每次都重建任务面板，会造成闪烁、重复内容和旧状态覆盖。revision 只在任务计划真正变化时更新现有组件，兼顾实时性与稳定性。

## 7. Harbor 基准评测

### 问：213、221、225 和两个分数分别是什么？

> 225 是完整任务集。221 是拿到明确 reward 的任务数，213 是其中通过数，因此 reward-only pass@1 为 `213 / 221 = 96.38%`。端到端 Harbor mean 为 94.67%，它衡量完整运行链路，不应简单写成 `213 / 225`。

### 问：没有 reward 的任务算什么？

> 不应统一包装成通过或简单归因于网络。需要分别检查 `result.json`、verifier 输出和任务日志，区分 reward 文件缺失、网络/provider 异常、verifier 超时和 agent 产物问题。严格端到端口径下它们会拉低总体结果；分析 agent 能力时也要单独说明有效判分范围。

### 问：强模型跑 benchmark 和工程能力有什么关系？

> 模型决定上限，但工程决定结果是否可信、可复现、可解释。我的工作包括任务隔离、容器资源、网络异常分类、重试边界、Gradle 代理、verifier feedback 协议、结果归档和中断恢复。否则无法区分失败来自 agent、provider、网络还是 verifier，高分也可能只是偶然跑通。

## 8. 本地优先产品如何排障

FirstCoder 是 PyPI 分发的本地 CLI/TUI，不默认上传用户代码、对话或 telemetry。用户报告“改错文件”时，应请求最小化、可脱敏证据：

- FirstCoder / Python / OS 版本；
- Provider、model 与脱敏配置；
- 复现步骤、预期和实际结果；
- TUI 截图、错误堆栈；
- 脱敏 session export 或工具事件摘要；
- 最小复现仓库、最小脚本或文件结构。

定位时区分：模型初始选错路径、工具参数错误、上下文污染、用户审查没注意，还是审查和执行间的文件变化。没有足够证据时归为待确认问题，不能凭猜测改核心执行链路。

## 9. 高频易错表述

- 不说“resume 从 checkpoint 后恢复”——应说完整 JSONL 重建系统事实；checkpoint 决定后续模型投影。
- 不说“checkpoint 删除旧历史”——历史和 archive 原文仍保留。
- 不说“正则阻止所有危险 shell”——正则只是辅助风险识别。
- 不说“diff 审查保证绝对并发安全”——它降低并发覆盖风险，不是原子事务。
- 不说“工具成功就能压缩”——还要等模型成功消费该结果。
- 不说“没有 reward 一定是网络问题”——需分类检查。
- 不说“自动检索 archive”——当前是模型通过 `retrieve_archive` 显式取回。

## 10. 复习顺序

1. 先背“一分钟项目介绍”。
2. 重点理解 `JSONL → SessionView → ContextBuilder → Provider`。
3. 背工具链路：校验、权限、审查、二次校验、执行、结果配对。
4. 背压缩四层与“未消费工具结果不能有损压缩”。
5. 最后准备 Harbor 指标口径和本地产品排障边界。
