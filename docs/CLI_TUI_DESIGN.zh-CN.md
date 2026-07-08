# CLI / TUI 设计

[English Version](CLI_TUI_DESIGN.md)

## 概述

FirstCoder 目前通过三种用户入口暴露同一套 agent 运行时：

- Textual TUI 交互式终端界面
- 行式交互 CLI（`--interactive`）
- 单轮 CLI（`--message` 或 stdin）

总入口在 `firstcoder/cli.py`。TUI 和 CLI 共享同一套核心运行时对象，但控制路径并不完全相同。TUI 通过 `AgentChatRunner` 走异步流式路径，单轮 CLI 走同步执行路径。

## 关键文件

- `firstcoder/cli.py`：顶层参数解析、模式路由、config 命令、REPL、单轮执行
- `firstcoder/app/factory.py`：组装主运行时对象图
- `firstcoder/app/runtime.py`：`CurrentSessionState` 和 `AgentChatRunner`
- `firstcoder/app/tui.py`：`FirstCoderApp` Textual 应用
- `firstcoder/app/tui_state.py`：以 transcript 为中心的 TUI 状态模型
- `firstcoder/app/commands.py`：slash command 协议
- `firstcoder/app/session_commands.py`：session 相关命令
- `firstcoder/app/permission_commands.py`：权限模式相关命令
- `firstcoder/app/router.py`：命令组合辅助逻辑
- `firstcoder/config/settings.py`：配置加载和优先级处理

## 运行时组装

当前实现中并没有 `AppFactory` 或 `RuntimeAssembly` 这类类式入口，运行时组装是函数式完成的。

`firstcoder/app/factory.py` 中的 `create_firstcoder_app(...)` 会按顺序组装：

1. JSONL session store
2. sandbox access 和 builtin tool registry
3. provider
4. permission grant store 和 project permission manager
5. `AgentSession`（新建或恢复）
6. `CurrentSessionState`
7. context compaction 相关服务
8. session catalog、resume、share 服务
9. slash command handlers
10. `AgentChatRunner`
11. `FirstCoderApp`

因此，TUI 更像是一个已经装配好运行时后的显示层，而不是在 UI 内部临时创建各个子系统。

## CLI 模式

`firstcoder/cli.py` 目前支持这些模式：

- `config` 命令：
  - `firstcoder config path`
  - `firstcoder config show`
  - `firstcoder config init`
- TUI 模式：
  - `firstcoder`
  - `firstcoder --tui`
- 行式 REPL：
  - `firstcoder --interactive`
- 单轮执行：
  - `firstcoder --message "..."`
  - 未显式传 message 时也可从 stdin 读取
- benchmark 模式：
  - `firstcoder --benchmark`

常见运行时覆盖参数包括：

- `--project`
- `--data-root`
- `--session-id`
- `--provider`
- `--auto-approve`
- `--max-tool-rounds`

## TUI 结构

Textual 应用主要由 `firstcoder/app/tui.py` 中的 `FirstCoderApp` 实现。

当前界面并不是由许多独立的“子系统 Widget 类”堆起来的，而是由少量具体控件构成：

- 顶部状态栏
- 可滚动 transcript 区域
- todo 面板
- activity 行
- 输入框

真实的 TUI 状态模型在 `firstcoder/app/tui_state.py`，核心对象包括：

- `TuiTranscript`
- `TuiTranscriptEntry`
- `TuiToolActivity`
- `TuiTodoItem`
- `TuiEntryKind`

也就是说，当前实现更接近“以 transcript 为中心的状态管理”，而不是旧文档里那种抽象的 `TUIState`。

## 流式输出与用户输入恢复

TUI 路径通过 `AgentChatRunner` 的异步接口工作：

- `arun_user_turn(...)`
- `aresume_with_user_input(...)`

`firstcoder/app/tui.py` 中会：

- 安装 stream event handler
- 缓冲文本增量
- 定时 flush 到 transcript
- 把 tool activity 和最终文本插入到同一条对话流里

当工具执行因为权限或用户输入而暂停时，loop 会返回一个 `pending_input` 请求。TUI 负责展示该请求，并在用户回复后通过 `aresume_with_user_input(...)` 恢复执行。

## Slash Commands

当前 TUI 中实际可用的命令包括：

- `/sessions`
- `/session <session_id>`
- `/new [title]`
- `/fork [title]`
- `/help`
- `/resume <session_id>`
- `/share [session_id] [--tool-results]`
- `/rename <title>`
- `/skills`
- `/skill <name>`
- `/context`
- `/compact status`
- `/compact`
- `/mode`
- `/mode <conservative|standard|aggressive|bypass>`

这些都是真实存在的 handler，不只是帮助文本。

## 配置优先级

配置加载实现在 `firstcoder/config/settings.py`。

当前优先级并不是一个统一的“所有 CLI 都压过所有配置”，而是按字段分别处理。

例如：

- provider 选择优先使用显式 CLI 覆盖，然后是 `FIRSTCODER_PROVIDER`，再是项目配置、全局配置和默认值
- provider 凭证和 base URL 在有映射时优先使用环境变量
- 顶层字段如 `model` 主要按项目配置、全局配置、默认值顺序解析

这点很重要，因为当前代码并没有一个通用的 CLI merge 层。

## 设计说明

- TUI 和 CLI 共享 session、provider、tools、permissions 和 context 这些核心子系统。
- TUI 路径比单轮 CLI 更强，因为它支持异步流式输出、中断和权限恢复。
- 运行时装配集中在 factory 函数中，这让测试和替代入口更容易维护。
