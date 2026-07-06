# 工具系统设计

[English Version](TOOLS_DESIGN.md)

## 概述

工具层负责让 agent 以受控方式访问本地环境。工具在本地以具体 executor 的形式存在，对模型暴露为统一 schema，并在需要时通过权限预检进行包装。

当前运行时刻意把三件事拆开：

- 模型可见的工具定义
- 本地执行函数
- 程序侧权限要求

## 关键文件

- `firstcoder/tools/types.py`：`Tool`、`ToolResult`、`ToolPermissionSpec`
- `firstcoder/tools/registry.py`：基础 `ToolRegistry`
- `firstcoder/tools/permission_registry.py`：`PermissionAwareToolRegistry`
- `firstcoder/tools/builtin.py`：内置工具组装
- `firstcoder/tools/session_registry.py`：session 级包装和注入
- `firstcoder/tools/descriptions.py`：面向模型的描述重写
- `firstcoder/utils/introspection.py`：函数签名到工具 schema 的生成

代表性工具实现包括：

- 文件类：`view.py`、`write.py`、`edit.py`、`delete.py`、`apply_patch.py`
- 检索和查看类：`ls.py`、`tree.py`、`glob.py`、`grep.py`、`read_multi.py`
- 执行类：`shell.py`、`python_exec.py`、`diagnostics.py`
- git 类：`git_status.py`、`git_diff.py`、`git_log.py`
- 网络类：`fetch.py`、`web_search.py`
- 交互类：`ask_user.py`、`todo.py`、`think.py`、`task_boundary.py`

## 核心数据模型

当前实现中的 `Tool` 是具体 dataclass，不是 protocol。它包含：

- `definition`：模型可见的 `ToolDefinition`
- `executor`：返回 `ToolResult` 的本地 callable
- `permission`：可选的 `ToolPermissionSpec`

`ToolResult` 是所有工具统一返回的结果结构：

- `name`
- `ok`
- `content`
- `data`
- `error`

之后这个结果会被转换成 provider 可见的 tool message，写回会话历史。

## Registry 模型

`firstcoder/tools/registry.py` 中的基础 registry 设计得比较简单。

职责包括：

- 以唯一名称保存工具对象
- 暴露模型可见的 `ToolDefinition`
- 按名称分发执行
- 把运行时失败转成结构化 `ToolResult`，而不是直接抛异常打断 loop

这让 agent loop 更稳：未知工具、参数形状错误、executor 内部异常都会回到模型可见的工具错误，而不是直接让回合崩掉。

## 内置工具组装

内置工具通过 `firstcoder/tools/builtin.py` 里的 `create_builtin_registry(...)` 组装。

组装时按类别添加：

- 只读文件和检查类工具
- 可选变更类工具
- 可选执行类工具
- 可选网络类工具

工具创建后还会经过 `apply_agent_tool_description(...)`，让模型看到的描述不是原始 Python docstring，而是专门整理过的说明。

这点很关键：schema 来自函数签名，但最终给模型看的描述是二次加工过的。

## Session 级包装

agent 不会直接把原始 builtin registry 暴露给 loop。`firstcoder/tools/session_registry.py` 中的 `create_session_tool_registry(...)` 会做 session 级包装。

当前行为包括：

- 必要时注入 session-scoped 的 `task_boundary` 工具
- 当存在 `PermissionManager` 时，用 `PermissionAwareToolRegistry` 包住 registry

因此 loop 最终使用的 registry 是“带 session 语义”的，而不是纯静态全局对象。

## 与权限系统的耦合

权限不是通过一张全局静态映射表实现的。每个工具都可以带一个 `ToolPermissionSpec`，描述如何从运行时参数构造 `PermissionRequest`。

`ToolPermissionSpec` 支持：

- 固定的 `PermissionAction`
- 从某个参数提取 target
- 固定 target 值
- 自定义 target builder
- 可选的 cwd 提取
- `allow_always` / `allow_auto` 等策略提示

`PermissionAwareToolRegistry` 会据此：

1. 构造 `PermissionRequest`
2. 向 `PermissionManager` 发起预检
3. 然后选择：
   - 允许执行
   - 返回拒绝结果
   - 返回结构化确认结果并暂停当前回合

这个暂停 / 恢复流程本身就是工具执行模型的一部分。

## 工具执行流程

真实流程是：

1. provider 返回规范化后的 `ToolCall`
2. agent loop 先把 assistant tool-call 消息写入 session log
3. session tool registry 做权限预检
4. 如果需要权限确认，则暂停回合并保存 pending execution
5. 否则执行本地 executor，得到 `ToolResult`
6. loop 把最终 tool result 追加进 session log
7. 下一次 provider 调用基于更新后的历史继续

loop 还会保证每个 assistant tool call 最终都有一个匹配的 tool result，包括：

- 权限拒绝
- 跳过的 sibling tool call

这能保持 provider 可见消息序列始终合法。

## 特殊工具

有些工具不是简单的环境适配器：

- `todo` 会更新 session 可见的 todo 模型，供 TUI 使用
- `think` 记录结构化推理文本，但不改变外部环境
- `task_boundary` 是按 session 注入的，并参与任务感知上下文压缩
- `web_search` 是具体后端驱动的搜索工具，不是抽象搜索接口

这些工具会直接影响运行时行为，而不只是“访问文件”或“执行命令”。

## 设计说明

- 工具 schema 来自 Python 函数，但模型描述会在之后统一重写。
- 权限逻辑以每个工具为单位声明，通过 wrapper 执行，而不是散落在 executor 内。
- 工具失败会被转换成结构化结果，方便 loop 安全继续。
- session 级包装让运行时可以注入上下文相关工具，而不污染全局 registry。
