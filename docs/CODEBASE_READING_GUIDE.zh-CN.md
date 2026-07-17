# 代码阅读指南

[English version](CODEBASE_READING_GUIDE.md)

这是一条**沿着运行中系统走的路线**，不是文件夹清单。当你需要回答
“这个改动应该放哪？”、又不想随手抄旁边文件时，用它。

配合 [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md) 看包规则与耦合清理结果。
本指南侧重*如何学代码*：限时路径、端到端追踪、练习和排障配方。

---

## 1. 读完应能做到

| 用时 | 你应能… |
| --- | --- |
| 30 分钟 | 说出主路径，并打开五个关键文件 |
| 60 分钟 | 把一次 tool call 从模型输出追到 JSONL 再追到 UI |
| 2 小时 | 把小改动放到正确层，并点名该跑哪些测试 |

---

## 2. 限时学习路径

### 路径 A — 30 分钟：建立地图

1. 浏览 [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md) 第 2–5 节（心智模型 + 一轮路径）。
2. 按顺序打开这些文件（先别深读）：
   - `firstcoder/cli.py`
   - `firstcoder/app/factory.py`（`create_firstcoder_app`）
   - `firstcoder/app/runtime.py`（`AgentChatRunner`）
   - `firstcoder/agent/loop.py`（`AgentLoop`）
   - `firstcoder/session/bootstrap.py`（`SessionBootstrap`）
3. 运行：

```sh
rg -n "class AgentLoop|def create_firstcoder_app|class SessionBootstrap" firstcoder
```

### 路径 B — 60 分钟：吃透一个真实能力

三选一：

- 循环限额 → `agent/loop_limits.py` + `loop.py` 里的停止处理
- 权限询问 → `permissions/manager.py` + `AgentChatRunner` 的 resume
- 压缩触发 → `AgentLoop` helpers + `context/manager.py`

对选定主题：

1. `rg -n "关键词" firstcoder tests`
2. 读归属模块，再读**一个**调用方，再读**一个**测试。
3. 跑你找到的最窄 pytest 文件。

### 路径 C — 2 小时：具备改动能力

1. 完成路径 B。
2. 通读对应设计文档（tools / permissions / context / …）。
3. 先完成下面练习 1 和 2，**先不改产品代码**。
4. 然后做单文件改动，并重跑聚焦测试。

---

## 3. 依赖规则（速查）

- `utils` / `permissions` / `tools` 只 import `firstcoder.runtime`，**绝不** import `firstcoder.agent`。
- 构造会话统一走 `session.bootstrap.SessionBootstrap`。
- UI/CLI 边界使用 `app.ports` / `agent.ports`。
- 隐藏状态工具名单在 `tools.hidden.HIDDEN_TOOL_STATUS_NAMES`。

PR 若违反这些，先审架构再吵风格。

---

## 4. 从一轮真实请求开始

追踪类似 **“找到 loop limit 并解释它”** 的请求：

```text
firstcoder/cli.py
  -> firstcoder/app/factory.py:create_firstcoder_app
       SessionBootstrap.from_project
       ContextWindowManager
       AgentChatRunner
       命令路由 + FirstCoderApp
  -> firstcoder/app/runtime.py:AgentChatRunner.run_user_turn
  -> firstcoder/agent/loop.py:AgentLoop
  -> ContextBuilder 构造 ChatRequest(messages, tools, system)
  -> provider.complete / astream
  -> session 工具注册表 execute（+ permissions）
  -> 事实追加到 .firstcoder/sessions/<id>.jsonl
  -> runtime 事件回到 TUI / CLI
```

这条链教会中心契约：

| 层 | 做什么 | 不做什么 |
| --- | --- | --- |
| Loop | 协调一轮 | 为业务逻辑直接调厂商 SDK |
| Provider | 翻译协议 | 持久化会话 / 裁决权限 |
| Tools | 本地副作用 | 拥有最终 allow/deny 策略 |
| Context | 持久化 + 投影事实 | 渲染 widget |
| App/TUI | 展示 + 收集输入 | 重写工具执行 |

---

## 5. 地图（去哪找）

| 区域 | 先读 | 拥有 | 不拥有 |
| --- | --- | --- | --- |
| `runtime/` | `cancellation.py`、`user_input.py` | 共享取消与用户输入 DTO | 循环策略、UI |
| `app/` | `factory.py`、`runtime.py`、`tui.py`、`ports.py` | 接线、ports、终端 UX | 模型协议翻译 |
| `agent/` | `loop.py`、`session.py`、`loop_limits.py` | 单轮编排 | 具体 shell/HTTP |
| `context/` | `store.py`、`writer.py`、`context_builder.py`、`manager.py` | 持久事实 + 投影 | UI widget |
| `tools/` | `builtin.py`、`registry.py`、`session_registry.py` | schema、分发、session 包装 | 最终权限策略 |
| `permissions/` | `manager.py`、`policy.py`、`grants.py` | allow/ask/deny + grants | 执行工具 |
| `providers/` | `types.py`、`factory.py`、adapters | 内部 ↔ 厂商转换 | 会话持久化 |
| `skills/` | `discovery.py`、`router.py`、`loader.py` | skill 目录与加载审计 | 注册工具 |
| `session/` | `bootstrap.py`、`catalog.py`、`resume.py`、`fork.py` | 生命周期服务 | agent 轮次语义 |
| `mcp/` | manager + client | 外部工具一等公民化 | 核心循环控制 |

---

## 6. 四遍阅读法

别对着大文件随机滚动。

### 第一遍 — 给行为命名

用可见命令、工具名、错误文案或 UI 文案搜索：

```sh
rg -n "词" firstcoder tests
```

不要只靠文件名猜（光看 `manager.py` 不够）。

### 第二遍 — 找缝（seam）

先顺着 import 找到接口或 dataclass，再进实现：

- `ChatProvider`
- `Tool` / registry 协议
- `ContextManagerLike`
- `ChatRunnerLike`
- `PermissionRequest` / `UserInputRequest`

缝告诉你哪些部分可以独立变化。

### 第三遍 — 跟数据，不只跟控制流

一轮里始终盯住这些对象：

| 对象 | 为什么重要 |
| --- | --- |
| `ChatRequest` | 模型实际收到什么 |
| `ChatResponse` / 流事件 | 回来了什么 |
| `ToolCall` / `ToolResult` | 本地工作单元 |
| Session JSONL 事件 | 持久真相 |
| `UserInputRequest` | 为什么暂停 |
| `AgentTurnResult` | UI 被告知了什么 |

只跟 `if` 分支，很容易漏掉投影和 resume 类 bug。

### 第四遍 — 立刻读测试

测试对错误处理和不变量的描述，往往比 happy path 代码更精确。
改之前先跑窄测试：

```sh
.venv/bin/python -m pytest tests/test_whatever.py -q
```

请用 `pytest tests` 或明确路径，不要在仓库根裸跑 `pytest`：生成的 benchmark
树可能自带无关 `tests/` 目录。

---

## 7. 最小词汇表

| 术语 | 在 FirstCoder 里的含义 |
| --- | --- |
| **Turn（轮）** | 一次用户提交，由 `AgentLoop` 处理到停止或暂停 |
| **Fact（事实）** | 追加写的会话事件（或回放后的有效状态） |
| **Projection（投影）** | 从事实构造的、provider 可见消息（`ContextBuilder`） |
| **Compaction（压缩）** | 缩小投影；不擦除审计日志 |
| **Checkpoint** | L4 handoff 摘要，作为新的投影基点 |
| **Grant** | 对某 scope 记住的 allow 决策 |
| **Port** | `Protocol` 边界，避免 UI/测试绑死实现 |
| **Bootstrap** | 构造绑定项目的 `AgentSession` 的唯一装配路径 |
| **Hidden tool** | 仍可调用；只是不出现在嘈杂的人机状态流 |
| **Bypass mode** | 一种权限*策略模式*，不是“没有安全代码” |

---

## 8. 有用入口命令

```sh
# 编排与装配
rg -n "class AgentLoop|def create_firstcoder_app|class SessionBootstrap" firstcoder tests

# 请求 / 工具 / 权限缝
rg -n "ChatRequest\(|tool_registry\.execute|preflight\(" firstcoder tests

# 人工暂停 / 恢复
rg -n "UserInputRequest|resume_with_user_input|permission_confirmation" firstcoder tests

# 压缩触发
rg -n "_auto_compact|compact_if_needed|ContextWindowTrigger" firstcoder tests

# 完整单元测试（仓库根）
.venv/bin/python -m pytest tests -q
```

---

## 9. 第一次改动：选对层

| 你想… | 改… | 不要… |
| --- | --- | --- |
| 新的厂商选项 | `providers/` + config | 在 loop 里塞厂商字段 |
| 新的本地能力 | 新 `Tool` + 权限 spec | 在 `AgentLoop` 写特判 `if` |
| 不同审批规则 | `permissions/policy.py` 或 grants | 在工具里写死 allow |
| 不同历史可见性 | context 投影 / 压缩 | 破坏性编辑 JSONL |
| 新斜杠命令 | `app/` 命令 handler | 绕过 session 服务 |
| 共享取消/输入 DTO | `runtime/` | 从 tools import `agent` |

---

## 10. 高频误解

**“system prompt 就是全部上下文。”**  
不是。它是稳定前缀；`ContextBuilder` 单独投影会话历史；工具 schema 在 `ChatRequest.tools`。

**“bypass 就是没有安全代码。”**  
不是。它只是 policy mode；执行仍经 registry，并返回结构化结果。

**“压缩把历史删了。”**  
不是。JSONL 事实还在；checkpoint 改的是本次 provider 视图。

**“TUI 拥有会话。”**  
不是。TUI 负责展示和路由。会话事实在 `context` / `.firstcoder`。

**“出度高 = 模块设计差。”**  
未必。工厂和 agent loop *应该*依赖很多协作者。更臭的是叶子层高入度还向上 import。

**“批准后可以信任模型重发 pending tool_call。”**  
不行。恢复必须使用本地保存的原始 call。

---

## 11. 排障配方

### 权限 UI 出来了，但 resume 没反应

1. 确认 `UserInputRequest.id` 与 UI 提交一致。
2. 追踪 `AgentChatRunner.resume_with_user_input`。
3. 确认原始 `tool_call` 从 session/runtime 状态恢复。
4. 测试：`rg -n "resume_with_user_input|permission_confirmation" tests`。

### 模型说调了工具，工作区没变

1. 调用有没有变成持久 tool result 事件？
2. 决策是 `ASK` 还是 `DENY`？
3. 执行器是否抛错并被规范成 `ToolResult`？
4. 先跟 `PermissionAwareToolRegistry`，再进具体工具模块。

### Resume 后丢了工作上下文

1. 打开 `.firstcoder/sessions/<id>.jsonl`，确认事件确实追加。
2. 回放路径：store → view → `ContextBuilder`。
3. 查看是否有 checkpoint / compaction 改变了投影。
4. 别指望进程内 `SessionRuntimeState` 跨重启还在。

### “Tool not found” / MCP 工具缺失

1. factory 工具列表：builtins vs `McpToolProvider` 合并。
2. MCP 是否已连接？后台连接失败很容易被忽略。
3. session registry vs 全局 registry：session 作用域工具需要
   `create_session_tool_registry`。

### 轮次因限额停止

1. 读 `agent/loop_limits.py` 与 stop-reason 枚举。
2. 找 `AgentLoop` 记录 `tool_round_limit` / `provider_call_limit` /
   `turn_timeout` 的位置。
3. 调用方是否传入了非默认 `AgentLoopLimits`（`default`、`swe_lite`、`summary`…）。

---

## 12. 递进练习

打开仓库做。优先阅读和测试，少做臆测性编辑。

### 练习 1 — Loop limits（安全，不改产品代码）

1. 打开 `firstcoder/agent/loop_limits.py`。
2. 运行：

```sh
rg -n "max_tool_rounds|TOOL_ROUND_LIMIT|AgentLoopLimits" tests firstcoder
.venv/bin/python -m pytest tests -q -k "loop_limit or tool_round or AgentLoopLimits"
```

3. 自己写一句：*什么停了、记录了什么、谁会看见。*

### 练习 2 — Bootstrap 是唯一装配路径

1. 列出调用点：

```sh
rg -n "SessionBootstrap|AgentSession.create|AgentSession.resume" firstcoder
```

2. 确认 new/resume/fork/factory 在 grants/skills/tools 上走 bootstrap。
3. 若发现平行装配路径，把它当债务，不当模板。

### 练习 3 — 追踪一次权限询问

1. 从 `PermissionManager.preflight` / `build_confirmation` 出发。
2. 找出 `UserInputRequest` 如何到达 TUI。
3. 找出答案如何回到工具执行。
4. 记下原始 `tool_call` 存在哪里。

### 练习 4 — 投影 vs 事实

1. 读 `ContextBuilder.build_provider_messages`。
2. 在 `context/` 找一种 compaction 事件类型，以及保护 tool-call 顺序的测试。
3. 解释为什么删 JSONL 行不是合法的“修上下文溢出”。

---

## 13. 把测试当文档

| 关注点 | 好的起点（搜索） |
| --- | --- |
| App 装配 | `tests/test_app_factory.py`、`tests/test_cli.py` |
| TUI / runner | `tests/test_app_tui.py`、`tests/test_app_runtime.py` |
| Sessions | `tests/test_session_*.py` |
| Permissions | `tests/test_permissions_manager.py`、`tests/test_permission_results.py` |
| Context / compact | `tests/test_context_*.py` |
| Tools | `tests/test_tools_*.py` 或工具名相关文件 |
| Providers | `tests/test_providers_*.py` / adapter 测试 |

当设计文档和测试冲突时，**先信测试**，再在同一 PR 里修文档。

---

## 14. 相关文档

- [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md) — 边界与依赖规则
- [CLI_TUI_DESIGN.zh-CN.md](CLI_TUI_DESIGN.zh-CN.md) — 启动、命令、流式
- [AGENT_LOOP_GUARDRAILS.zh-CN.md](AGENT_LOOP_GUARDRAILS.zh-CN.md) — 停止/暂停/继续
- [CONTEXT_MANAGEMENT_DESIGN.zh-CN.md](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md) — 事实与压缩
- [TOOLS_DESIGN.zh-CN.md](TOOLS_DESIGN.zh-CN.md) / [PERMISSIONS_DESIGN.zh-CN.md](PERMISSIONS_DESIGN.zh-CN.md)
- [PROVIDERS_DESIGN.zh-CN.md](PROVIDERS_DESIGN.zh-CN.md) / [SKILL_SYSTEM_DESIGN.zh-CN.md](SKILL_SYSTEM_DESIGN.zh-CN.md)
- [docs/README.zh-CN.md](README.zh-CN.md) — 总索引
