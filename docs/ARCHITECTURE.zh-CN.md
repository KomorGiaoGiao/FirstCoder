# FirstCoder 架构说明

[English version](ARCHITECTURE.md)

这是 FirstCoder 的架构教材：讲清包边界、依赖规则、主运行路径，以及改动应该落在哪一层。
建议和 [CODEBASE_READING_GUIDE.zh-CN.md](CODEBASE_READING_GUIDE.zh-CN.md) 一起读，边读边打开文中点名的源码文件，而不是死记文件名。

**读者：** 需要改运行时行为、又不想无意中制造第二套真相源的贡献者。

**不在本文范围：** 用户入门、Provider API Key、评测手册。那些在仓库根 README 与评测文档里。

---

## 1. 读完应能做到

1. 给定一个关注点（编排、事实、投影、工具、权限、UI），指出归属包。
2. 解释为什么 `tools` / `permissions` / `utils` 不能 import `agent`。
3. 从 CLI/TUI 把一轮用户输入追到 JSONL 事实，再追回 UI。
4. 判断改动属于 `AgentLoop`、`ContextWindowManager`、工具执行器、provider 适配，还是 slash command。
5. 说出 create / resume / fork 会话的统一装配入口。

---

## 2. 心智模型

FirstCoder 是**本地 coding agent**：模型提议动作；Python 在策略下执行工具；追加写日志保留审计轨迹。

用四层同心圆理解：

```text
┌────────────────────────────────────────────────────────────┐
│  表现层：app/（TUI、斜杠命令、选择器、流式展示）            │
├────────────────────────────────────────────────────────────┤
│  装配层：factory、SessionBootstrap、ports                   │
├────────────────────────────────────────────────────────────┤
│  编排层：agent/（AgentLoop、AgentSession）                  │
│    使用：providers、tools、permissions、skills、runtime     │
├────────────────────────────────────────────────────────────┤
│  事实与投影：context/（+ session 生命周期）                 │
│  副作用：tools/ 执行器，受 permissions/ 约束                 │
└────────────────────────────────────────────────────────────┘
```

比任何类名更重要的三个契约：

| 契约 | 含义 |
| --- | --- |
| **事实 vs 视图** | JSONL 事件是持久真相；发给模型的消息只是投影。 |
| **协调 vs 执行** | `AgentLoop` 决定*何时*；tools/providers 负责*做什么*。 |
| **策略 vs 提示词** | 安全靠代码路径强制；prompt 只引导模型，不代替策略。 |

设计如果违反其中一条，在本仓库里几乎一定是错的。

---

## 3. 包地图

规模会浮动，只作方位感。撰写时 `firstcoder/` 大约 25k 行 Python、174 个文件；最大的几块是 `context/`、`app/`、`tools/`、`agent/`。

| 包 | 职责 | 先读 | 不该拥有 |
| --- | --- | --- | --- |
| `runtime/` | 共享取消与结构化用户输入请求 | `cancellation.py`、`user_input.py` | 循环策略、工具、UI |
| `app/` | 组合根、TUI、斜杠命令、UI 侧 ports | `factory.py`、`runtime.py`、`ports.py`、`tui.py` | 厂商协议翻译 |
| `input/` | 附件发现、剪贴板读取与会话暂存 | `attachments.py`、`clipboard.py` | provider 协议编码或 widget 状态 |
| `agent/` | 单轮编排与会话运行时对象 | `loop.py`、`session.py`、`loop_limits.py` | 具体 shell/HTTP 工作 |
| `context/` | 追加写事实、投影、L1–L4 压缩 | `store.py`、`writer.py`、`context_builder.py`、`manager.py` | widget、厂商 SDK |
| `session/` | 目录/索引/new/resume/fork/share | `bootstrap.py`、`catalog.py`、`resume.py` | 模型补全 |
| `tools/` | schema、执行器、session registry、隐藏工具名单 | `builtin.py`、`registry.py`、`session_registry.py`、`hidden.py` | 最终权限裁决 |
| `permissions/` | allow/ask/deny 与授权持久化 | `manager.py`、`policy.py`、`grants.py` | 真正执行工具 |
| `providers/` | 内部格式 ↔ 厂商协议 | `types.py`、`factory.py`、各 adapter | 会话持久化 |
| `skills/` | 发现、路由、加载审计 | `discovery.py`、`router.py`、`loader.py` | 注册工具 |
| `mcp/` | 外部 MCP 服务器接入为工具 | client/manager | 核心循环控制 |
| `utils/` | 沙箱、子进程、文本工具 | `sandbox_access.py` | 业务编排 |
| `config/` | 配置解析 | loader / 配置模型 | 运行时状态 |
| `eval/` | 评测适配与指标 | 包入口 | 产品 UI |

### 包如何协作

```text
cli / app
  ├── session.bootstrap  ──► AgentSession（tools + permissions + skills）
  ├── providers.factory  ──► ChatProvider
  ├── context.manager    ──► 压缩决策
  └── AgentChatRunner    ──► agent.loop.AgentLoop
                                ├── input 附件 → session store
                                ├── context.writer / builder
                                ├── providers.complete|astream
                                ├── tools（+ PermissionAwareToolRegistry）
                                └── runtime（取消 / 用户输入请求）
```

---

## 4. 依赖规则

### 允许的方向（高层）

```text
cli / app
  -> agent / session / context / providers / mcp / skills

agent
  -> context / tools / providers / permissions / skills / runtime

session
  -> agent（AgentSession 对象）/ context / skills / permissions

tools / permissions / utils
  -> runtime     # 禁止 -> agent

context
  -> providers.types / tools.types   # 只依赖数据形状，不依赖编排

providers / config
  -> 尽量不依赖上层
```

### 硬规则（以及为什么）

1. **`utils`、`permissions`、`tools` 不得 import `agent`。**
   - *为什么：* 它们更接近叶子层。一旦依赖编排层，小改动就容易环依赖，并和 UI/测试缠在一起。
   - *共享类型放哪：* `firstcoder.runtime`（`CancellationToken`、`UserInputRequest` 等）。
   - 运行时原语统一从 `firstcoder.runtime` 直接引用。

2. **UI/CLI 依赖 ports，不绑 loop 内部实现。**
   - `firstcoder.app.ports`：`CommandHandlerLike`、`ChatRunnerLike`、`CurrentSessionLike`、`ContextManagerLike`。
   - `firstcoder.agent.ports`：loop 用的最小 `ContextManagerLike`。
   - *为什么：* TUI 测试和替代前端可以 fake runner，而不必构造完整模型 provider。

3. **会话构造集中化。**
   - `firstcoder.session.bootstrap.SessionBootstrap` 是 create/resume/fork/factory 的统一装配入口。
   - *为什么：* grants 路径、skill catalog、AGENTS.md、tools 解析、沙箱接线曾在多处漂移。

4. **对用户隐藏的控制面工具只维护一份名单。**
   - `firstcoder.tools.hidden.HIDDEN_TOOL_STATUS_NAMES`
   - Session registry 保留内部 `task_boundary` 供隐藏分类器使用；主模型请求会过滤并拒绝它。

### 软边（已知，保持窄）

| 边 | 方向 | 说明 |
| --- | --- | --- |
| 会话对象在 agent | `session` → `agent` | 预期内：bootstrap 构造 `AgentSession`。 |
| 目录维护 | `context.store` → `session.index`（懒加载） | 保持懒加载；不要再长第二条写入路径。 |
| 仅类型导入 | 如 `runtime.user_input` 在 `TYPE_CHECKING` 下引用 `ToolResult` | 避免运行时环依赖。 |

### 反模式

| 不要 | 改为 |
| --- | --- |
| 工具执行器 import `AgentLoop` | 返回结构化 `ToolResult`，由 loop 决策 |
| 在新服务里复制 grant/skill 胶水 | 调用 `SessionBootstrap` |
| 把厂商专用字段塞进 `AgentSession` | 留在 provider adapter |
| 在 `tui.py` 特判隐藏某个工具 | 把名字写入 `tools.hidden` |
| 靠删除 JSONL 行“腾上下文” | 用压缩 / 投影 |

---

## 5. 一轮用户输入（详细路径）

这是系统的脊柱。记住*形状*，不必背每个 helper 名。

```text
用户在 TUI / CLI 提交文本和可选的已暂存附件
  │
  ▼
firstcoder/cli.py
  -> app.factory.create_firstcoder_app(...)
       组装：store、tools（+ MCP）、provider、SessionBootstrap.from_project、
             ContextWindowManager、AgentChatRunner、命令路由、FirstCoderApp
  │
  ▼
app.runtime.AgentChatRunner.run_user_turn / resume_with_user_input
  │
  ▼
agent.loop.AgentLoop
  1. 把附件复制到 session 存储，再经 session writer 追加用户消息/元数据（持久事实）
  2. 初始化首个任务，或运行隐藏的 task-boundary 分类
  3. 压缩触发：
       _auto_compact
       _compact_for_prompt_too_long
       _compact_after_task_hash_changed
     → ContextWindowManager.compact_if_needed
  4. ContextBuilder 构造 ChatRequest(messages, tools, system)
  5. provider.complete 或 provider.astream
  6. 对每个 tool_call：
       session 工具注册表（+ 权限 preflight）
       ASK：以 UserInputRequest(kind=permission_confirmation) 暂停
       直接改文件：先生成可信 prewrite diff；standard 走权限确认，已允许/aggressive 走仅 review 的 Apply
       allow：执行；追加 tool result 事实
  7. 按 AgentLoopLimits settle；验证结果作为证据返回模型
  8. 模型自然停止但当前 TaskPlan 仍有未完成任务时，最多发送一次不落盘的临时 system 对账指令
  │
  ▼
runtime 事件 → AgentChatRunner → TUI 转写 / 活动流 / 权限 UI
```

### 持久状态 vs 进程内状态

| 持久（进程退出仍在） | 进程内（重建或丢失） |
| --- | --- |
| `.firstcoder/sessions/<id>.jsonl` 事实 | `SessionRuntimeState` |
| `.firstcoder/attachments/<session-id>/` 暂存的附件字节 | composer 里的待发送附件 chip |
| 权限 grants 文件（`permissions.json`） | pending permission 的原始 tool_call |
| 回放到 `SessionView.task_plan` 的 `task_plan_updated` 快照 | 当前 review 卡片的展开状态 |
| 磁盘上的 skill 文件 | prompt prefix cache |
| MCP 服务器配置 | 存活的 MCP 连接 |

Resume 通过回放 JSONL 尽量重建。跨重启不能丢的东西必须是事实或显式 grant——不能只活在 Python 对象里。

成功的 TaskPlan 变更会发出运行时工具事件，让 TUI 立即刷新；恢复时再从同一份 `task_plan_updated` 快照投影。`linear` 按稳定 order 推导顺序，`dag` 使用显式依赖；普通变更按稳定任务 ID 和当前 revision 定位。遇到 revision 冲突时先 `task_list` 再重试，不能靠替换整个计划解决。TUI 将其标记为模型上报状态，不会根据命令、文件修改或测试成功自动推断完成。

### 跨边界关键对象

| 对象 | 包 | 角色 |
| --- | --- | --- |
| `ChatRequest` / `ChatResponse` | `providers.types` | 内部模型 I/O |
| `UserAttachment` / `PreparedAttachment` | `input.attachments` | composer 输入和 session 安全的附件元数据 |
| `ContentPart` | `providers.types` | 厂商无关的文本/图片内容投影 |
| `Tool` / `ToolCall` / `ToolResult` | `tools.types` | schema + 执行结果 |
| `PermissionRequest` / decision | `permissions` | allow / ask / deny |
| `UserInputRequest` | `runtime.user_input` | 等人（权限或 ask_user） |
| `ContextCompactRequest` | `context.manager` | 是否/如何压缩 |
| `AgentTurnResult` / status | `agent.user_input` | 给 app 层的轮次结果 |
| `AgentLoopLimits` | `agent.loop_limits` | 轮次 / 调用 / 时间预算 |

---

## 6. 会话装配

`SessionBootstrap` 故意写得很无聊：唯一知道如何构造“绑定项目”的 `AgentSession` 的地方。

```text
SessionBootstrap
  解析 tools（静态列表或 tools_provider）
  permission_manager = 项目策略 + FilePermissionGrantStore(data_root)
  create / resume / from_project
      -> AgentSession.*
           writer、runtime_state、session 工具注册表、
           agents_md、skill_catalog、sandbox_access
```

调用方：

- `session.new.NewSessionService`
- `session.resume.ResumeService`
- `session.fork.ForkSessionService`
- `app.factory.create_firstcoder_app`

如果你正准备把「创建 PermissionManager + 发现 skills + 读 AGENTS.md」粘贴到第五个文件，停下来，扩展 `SessionBootstrap`。

### Catalog 公开 API

会话发现 helper 有意公开：

- `session.catalog.record_from_path`
- `session.catalog.build_record_from_events`
- `session.catalog.session_sort_key`

`session.index` 只依赖这些公开函数，不保留下划线兼容别名。

---

## 7. `AgentLoop` 内部编排

`AgentLoop` 是**单轮用户输入的事务管理器**，不是杂物抽屉。

它拥有：

- 轮次生命周期（开始 → 模型 ↔ 工具 → settle → 停止）
- 压缩*触发*（何时问 context manager）
- 权限与 `ask_user` 的暂停/恢复
- 来自 `AgentLoopLimits` 的停止原因（`tool_round_limit`、`provider_call_limit`、`turn_timeout`）

它不拥有：

- 厂商 HTTP 细节（providers）
- 文件/shell 副作用（tools）
- token 计算与 L1–L4 算法（context）
- widget 渲染（app）

相关拆分模块：

| 模块 | 关注点 |
| --- | --- |
| `agent/tool_execution.py` | 执行与记录 tool call |
| `agent/tool_flow.py` | 工具批次的流程控制 |
| `agent/tool_settlement.py` | 把工具结果 settle 进本轮 |
| `agent/task_plan_policy.py` | 当前 TaskPlan 读取与一次性结束对账 |
| `agent/task_boundary_classifier.py` | 任务边界分类 |
| `agent/loop_limits.py` | 预算与停止原因枚举 |

### 压缩触发 helper

优先用具名 helper，而不是每次拼 trigger 标志：

- `_auto_compact()`
- `_compact_for_prompt_too_long()`
- `_compact_after_task_hash_changed()`

它们包装 `_compact_if_needed`，让 loop 里的*意图*可读。

---

## 8. 上下文：事实、投影、压缩

职责切分：

| 层 | 归属 | 作用 |
| --- | --- | --- |
| 追加写日志 | `context.store.JsonlSessionStore` | 磁盘字节 |
| 写入 API | `context.writer.SessionEventWriter` | 会话侧类型化追加 |
| 有效事实 | replay → `SessionView` / runtime replay | “当前为真”的内容 |
| Provider 投影 | `context.context_builder.ContextBuilder` | 本次请求的 `ChatMessage[]` |
| 压缩路由 | `context.manager.ContextWindowManager` | 是否 / 哪一层 |
| L1–L3 | `context.compaction` / `context.content.*` | 确定性压缩 |
| L4 | `context.llm_compact` | 模型写的 coding handoff |

```text
JSONL 事件
  -> 回放 -> SessionView
  -> ContextBuilder -> ChatMessage[]（+ ChatRequest.tools 上的 schema）
  -> provider
```

值得钉在显示器上的不变量：

1. 压缩**不删除**审计日志；它改变的是投影。
2. 发给 provider 的历史绝不能以孤儿 `role=tool` 开头。
3. 每个 tool result 保留原来的 `tool_call_id` 配对。
4. 工具 schema 走 `ChatRequest.tools`，不要粘贴进 system prompt。

深读：[CONTEXT_MANAGEMENT_DESIGN.zh-CN.md](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)。

---

## 9. 工具、权限与人工暂停

```text
模型 tool_call
  -> PermissionAwareToolRegistry
       PermissionManager.preflight
         ALLOW  -> 执行工具
         DENY   -> 结构化拒绝结果（仍是一条 tool 消息）
         ASK    -> UserInputRequest(kind="permission_confirmation")
                   AgentLoop 在本地保存原始 tool_call
                   UI 用 request_id 回答
                   resolve_confirmation -> 恢复执行
```

`ask_user` 使用同一套 `UserInputRequest`，`kind="ask_user"`。

对 `write`、`edit`、`apply_patch`、`delete`，`ToolExecutor` 会在执行前构造可信 `PrewriteReview`。standard 模式下 diff 是普通权限暂停的一部分；已有 `ALLOW`（含 aggressive 或匹配 grant）仍会变成只用于 review 的 Apply 暂停。bypass 会发出非阻塞 diff 事件后立即继续；非交互 benchmark adapter 可显式关闭它。resume 会重验保存的快照，UI 也不能提供真正要执行的调用 payload。

关键安全规则：pending 的原始 `tool_call` 必须来自**本地会话状态**，不能信任模型回放、用户看不见的 payload。

隐藏工具（`tools.hidden`）被调用时仍会执行；只是不出现在嘈杂的人机活动流里。

深读：[TOOLS_DESIGN.zh-CN.md](TOOLS_DESIGN.zh-CN.md)、
[PERMISSIONS_DESIGN.zh-CN.md](PERMISSIONS_DESIGN.zh-CN.md)。

---

## 10. Provider 与扩展缝

Provider 把内部 `ChatRequest` / 流事件翻译成厂商协议（OpenAI 兼容、Anthropic 等）再翻译回来。它们不得写会话，也不得裁决权限。

Skill 被发现并路由进 prompt 面；它们不是第二套工具注册表。MCP 服务器*才是*额外工具，在 factory 的 tool provider 处合并，并且仍走权限。

深读：[PROVIDERS_DESIGN.zh-CN.md](PROVIDERS_DESIGN.zh-CN.md)、
[SKILL_SYSTEM_DESIGN.zh-CN.md](SKILL_SYSTEM_DESIGN.zh-CN.md)、
[MCP.zh-CN.md](MCP.zh-CN.md)。

`ContextBuilder` 还会在构造请求时把已持久化的图片附件投影为 `ContentPart`。它只读取能解析到 session store 内的路径；JSONL 只保存相对路径和元数据，不落图片 base64。

---

## 11. 决策树：改动落在哪？

```text
是在改厂商 HTTP body 形状？
  -> providers/（+ config）

是模型可调用的新本地能力？
  -> tools/（Tool + 权限 spec），注册到 builtin/session registry

是 allow/ask/deny 的时机或规则？
  -> permissions/policy.py 或 grants

是模型从历史里*能看见*什么？
  -> context 投影/压缩（不要为这事删 JSONL）

是一轮的停止/暂停/继续？
  -> agent/loop.py（+ loop_limits / tool_* helpers）

是斜杠命令或 TUI widget？
  -> app/ 的 commands / views；调用 session 服务，不要重写它们

是 create/resume/fork 时 grants、skills、tools 的接线？
  -> session/bootstrap.py

是 tools/permissions 也需要的共享取消或用户输入 DTO？
  -> runtime/
```

---

## 12. 新代码检查清单

1. 下层只是为了一个类型想依赖 `agent`？把类型下沉到 `runtime`（或其它中性包）。
2. 创建或恢复会话？走 `SessionBootstrap`。
3. 新命令或 chat runner 依赖？先扩展 `app.ports`。
4. 不想刷状态流的工具？写入 `tools.hidden`。
5. 改压缩策略？优先改 `context.manager` / pipeline，而不是 loop 或 TUI。
6. PR 描述里的运行时结论能指到真实文件吗？不能的话，设计还是雾。

---

## 13. 如何验证架构结论

```sh
# 依赖意图：tools/permissions/utils 不应 import agent
rg -n "from firstcoder\.agent|import firstcoder\.agent" firstcoder/tools firstcoder/permissions firstcoder/utils

# 统一装配入口
rg -n "SessionBootstrap" firstcoder tests

# ports 表面
rg -n "ChatRunnerLike|CommandHandlerLike|ContextManagerLike" firstcoder tests

# 隐藏工具单一名单
rg -n "HIDDEN_TOOL_STATUS_NAMES" firstcoder tests

# 架构改动后常用的聚焦套件
.venv/bin/python -m pytest \
  tests/test_app_tui.py tests/test_session_*.py \
  tests/test_app_factory.py tests/test_app_runtime.py \
  tests/test_cli.py tests/test_permissions_manager.py \
  tests/test_permission_results.py -q
```

请使用 `pytest tests`（或明确文件列表），不要在仓库根裸跑 `pytest`：生成的 benchmark 树里可能自带无关 `tests/` 目录。

---

## 14. 相关文档

| 主题 | 文档 |
| --- | --- |
| 第一条端到端阅读路线 | [CODEBASE_READING_GUIDE.zh-CN.md](CODEBASE_READING_GUIDE.zh-CN.md) |
| TUI 装配与流式输出 | [CLI_TUI_DESIGN.zh-CN.md](CLI_TUI_DESIGN.zh-CN.md) |
| 轮次停止/暂停/继续 | [AGENT_LOOP_GUARDRAILS.zh-CN.md](AGENT_LOOP_GUARDRAILS.zh-CN.md) |
| 事实与压缩 | [CONTEXT_MANAGEMENT_DESIGN.zh-CN.md](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md) |
| 工具 | [TOOLS_DESIGN.zh-CN.md](TOOLS_DESIGN.zh-CN.md) |
| 权限 | [PERMISSIONS_DESIGN.zh-CN.md](PERMISSIONS_DESIGN.zh-CN.md) |
| Provider | [PROVIDERS_DESIGN.zh-CN.md](PROVIDERS_DESIGN.zh-CN.md) |
| 多模态附件链路 | [MULTIMODAL_INPUT_DESIGN.zh-CN.md](MULTIMODAL_INPUT_DESIGN.zh-CN.md) |
| Skill | [SKILL_SYSTEM_DESIGN.zh-CN.md](SKILL_SYSTEM_DESIGN.zh-CN.md) |
| MCP | [MCP.zh-CN.md](MCP.zh-CN.md) |
| 技术文档总索引 | [README.zh-CN.md](README.zh-CN.md) |

---

## 15. 文档侧注

本树的架构改动优先**结构性**变化（抽出共享模块、ports、bootstrap），而不是借机重写功能。这可能暂时*增加*行数（shim、ports、额外文件），同时降低耦合。
**行数目标**和**解耦目标**不是同一种优化。
