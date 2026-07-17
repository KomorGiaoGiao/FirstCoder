# CLI 与 TUI 运行时设计

[English](CLI_TUI_DESIGN.md)

## 这篇解决什么

这篇解释 FirstCoder 怎样启动、怎样组装出可用 agent session、以及运行时事件怎样变成终端上的内容。它不定义工具安全和厂商协议；那两部分请看文末链接。

核心结论是：终端界面只是已经装配好的 runtime 的客户端，不应该在 UI 里临时发明 provider、权限规则或会话存储。

## 一条请求如何到屏幕上

```text
firstcoder [flags]
  -> cli.py 选择 TUI、REPL 或单轮模式
  -> factory.py 组装 store + provider + tools（+ MCP）+ SessionBootstrap session
  -> AgentChatRunner 为当前 session 启动 AgentLoop
  -> stream/tool/input event 变成 TUI transcript/activity 更新
  -> 事实持久化留在 <project>/.firstcoder
```

例如 `firstcoder --message "explain loop limits"` 走同步单轮路径；Textual 界面则通过 `AgentChatRunner` 走异步流式路径。两者共用 session、tool registry、provider type 和 agent loop，但展示、打断和确认交互不同。

## 启动与依赖装配

主组合根是 `firstcoder/app/factory.py:create_firstcoder_app`，按顺序创建：

1. 默认位于 `<project>/.firstcoder` 的 `JsonlSessionStore`（可由 `data_root` 覆盖）。
2. `SandboxAccess` 与 `create_builtin_registry` 创建的内置工具。
3. `McpManager`（后台连接）与 `McpToolProvider`：调用方未注入固定 tools 时，把 MCP 工具与 builtins 合并。
4. 已配置好的 `ChatProvider`。
5. `SessionBootstrap`——grants、skills、AGENTS.md、tools、sandbox 的统一装配入口——再经 `from_project` / resume 得到带 session 级 registry 的 `AgentSession`。
6. 带 provider L4 摘要能力的 `ContextWindowManager`。
7. session catalog/new/resume/fork/share 服务与 slash command handler（new/resume/fork 也走 `SessionBootstrap`，不要再抄一套胶水）。
8. `AgentChatRunner`、`RuntimeModelSwitcher`，最后才是 `FirstCoderApp`。

UI/CLI 应依赖 `firstcoder.app.ports`（`ChatRunnerLike`、`CommandHandlerLike` 等），不要绑死 loop 内部实现。这个顺序不是摆设：session 要先拿到工具和权限 manager，runner 又依赖 session 和 context manager。测试能给 factory 注入 fake provider 或小型 tools，不需要真打网络。

包边界与依赖规则见 [ARCHITECTURE.zh-CN.md](ARCHITECTURE.zh-CN.md)。

## 跨层关键对象

| 对象 | 谁产生 | 谁消费 | 意义 |
| --- | --- | --- | --- |
| `AppConfig` | `config/settings.py` | factory/provider switcher | 解析后的配置，避免各处直接读环境变量 |
| `AgentSession` | `SessionBootstrap` / session service | runner、command handler | 当前可持久化对话和 tool registry |
| `CurrentSessionState` | `app/runtime.py` | TUI、runner | `/new`、`/fork`、resume 时可替换的当前指针 |
| `ChatStreamEvent` | provider | runner/TUI | 规范化的文本、reasoning、tool-call 增量 |
| `ToolExecutionEvent` | agent loop | runner/TUI | 本地执行和模型流是两条事件线 |
| `UserInputRequest` | `firstcoder.runtime.user_input`（permission / `ask_user`） | 交互 UI | 跨包共享的暂停/恢复契约（tools 不得为此 import agent） |

## 用户可见模式

`firstcoder/cli.py` 当前会路由这些模式：

| 调用方式 | 适用场景 | 关键限制 |
| --- | --- | --- |
| `firstcoder` 或 `--tui` | 完整 Textual 交互 | 需要交互终端 |
| `--interactive` | 行式 REPL | 没有 TUI 那样的可视 transcript |
| `--message "..."` | 脚本/CI 的单请求 | 没有长时间交互式审批对话 |
| 不传 message 的 stdin | 管道输入一个请求 | 同样遵守单轮限制 |
| `config path/show/init` | 查看/初始化配置 | 不启动 agent turn |
| `--benchmark` | 基准入口 | 还需额外 benchmark 环境 |

常用覆盖项包括 `--project`、`--data-root`、`--session-id`、`--provider`、`--auto-approve`、`--max-tool-rounds`。新增 flag 前先读 `cli.py`：配置优先级是按字段实现的，并不是“所有 CLI 参数无脑最大”。

## TUI 实际渲染什么

`app/tui.py` 的 `FirstCoderApp` 渲染 `app/tui_state.py` 的 transcript 型状态：对话条目、工具活动、todo、provider/session 状态和 pending input。它会先缓冲 token，再批量刷新，避免一个 token 刷一次 widget。

列在 `firstcoder.tools.hidden.HIDDEN_TOOL_STATUS_NAMES` 的内部控制面工具（当前是 `task_boundary`）仍可被 agent 调用，但不应刷进人机活动流。

运行时有两条事件线：

- provider event：reasoning/text/tool-call 增量和最终 response；
- local event：工具 started、finished、skipped、denied、permission asked。

分开后，模型没有新文本时 UI 仍可准确显示“shell 正在执行”。不要把本地工具运行伪造成 assistant 的一句话。

## 命令要经服务改状态

Slash command 经 `CompositeCommandHandler` 拼装。主要类别：session（`/new`、`/fork`、`/resume`、`/share`、`/rename`）、model（`/model`）、context（`/context`、`/compact`）、permission mode（`/mode`）、skills（`/skills`、`/skill`）。handler 应调用拥有职责的 service，再更新 `CurrentSessionState`；不要图省事直接改 JSONL 或 TUI state，后面必有回旋镖。

## 动手验证

```sh
.venv/bin/python -m firstcoder --help
.venv/bin/python -m pytest tests/test_cli.py tests/test_app_factory.py tests/test_app_runtime.py -q
```

没有凭证时，直接读 `tests/test_app_factory.py` 的 fake-provider case：它展示真实对象图，并验证首个 provider 请求前 `task_boundary` 已被按 session 注入。

## 排障入口

| 现象 | 先看哪里 |
| --- | --- |
| 顶栏模型/provider 不对 | 解析后的 config，再看 `RuntimeModelSwitcher` |
| 新 session 还显示旧历史 | `CurrentSessionState` 和 session command 的回调 |
| 文字只在结束时出现 | provider streaming capability 与 runner 的 streaming 选择 |
| 确认后无法继续 | pending `UserInputRequest` 与 `aresume_with_user_input` |
| help 有命令但无行为 | 对应 handler 是否注册进 router |

## 扩展规则

- 展示行为加在 `app/`，不要加到 provider adapter。
- 会话 create/resume/fork 接线走 `SessionBootstrap`，不要在 factory 旁边再抄一套。
- 新的 UI 依赖先扩 `app.ports`，再绑具体实现。
- 不要混淆 stream event 与本地 tool event。
- 改可见流程前加聚焦 `test_app_*` 或 `test_cli.py`。

关联：[架构说明](ARCHITECTURE.zh-CN.md)、[Agent 主循环护栏](AGENT_LOOP_GUARDRAILS.zh-CN.md)、[权限设计](PERMISSIONS_DESIGN.zh-CN.md)、[Provider 设计](PROVIDERS_DESIGN.zh-CN.md)。
