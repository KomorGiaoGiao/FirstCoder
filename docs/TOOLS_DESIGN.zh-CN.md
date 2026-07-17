# 工具设计

[English](TOOLS_DESIGN.md)

## 问题与边界

工具让模型可以请求本地操作，但不会让 provider 代码直接拥有文件系统或 shell 权限。本层负责三件事：模型可见定义、本地执行器、可选权限元数据；最终 allow/ask/deny 的策略不属于这里。

## 端到端例子：读取 `view`

```text
create_builtin_registry(project_root)
  -> Tool(definition, executor, permission spec)
  -> create_session_tool_registry 注入 task_boundary/retrieve_archive
  -> PermissionAwareToolRegistry 包装 dispatch
  -> AgentLoop 将 registry.definitions() 放进 ChatRequest.tools
  -> provider 返回 ToolCall(name="view", arguments=...)
  -> registry 预检/执行 -> ToolResult
  -> AgentLoop 写 role=tool 结果，再请求模型
```

JSON Schema 经 `ChatRequest.tools` 发送，由 provider adapter 转成各家原生 `tools` 格式。它不会追加进 system prompt，既不会重复占对话 token，也绝不是安全边界。

## 核心契约

`tools/types.py` 定义具体数据类：

| 类型 | 含义 |
| --- | --- |
| `ToolDefinition` | 模型看到的 name、description、JSON-Schema 风格 parameters |
| `Tool` | definition + 本地 executor + 可选 `ToolPermissionSpec` |
| `ToolResult` | 统一的 `name`、`ok`、`content`、`data`、`error` |
| `ToolPermissionSpec` | 如何从真实 arguments 推导权限请求 |

executor 返回 `ToolResult`，而不是让异常穿透 agent loop。因此未知工具、参数不合法和执行失败都能回给模型，并且 session 可回放。

## Registry 的创建与包装

`tools/builtin.py` 的 `create_builtin_registry` 组装 inspection、mutation、execution、network、git、interaction 等类别。`utils/introspection.py` 从函数签名得到基础 schema，随后覆盖为给模型阅读的 curated description，避免把 Python docstring 生硬扔过去。

原始 builtin 不是最终 registry。`create_session_tool_registry` 会注入 `task_boundary`、按需注入 `retrieve_archive`，有 manager 时再以 `PermissionAwareToolRegistry` 包一层。因为这些工具需要 session 状态，所以不能混进无状态全局 builtin。

装配期 `app.factory` 还可能通过 `McpToolProvider` 把 MCP 工具与 builtins 合并，再进入 session registry。对 loop 来说仍是同一张工具面，不是第二套 agent。

### 隐藏的控制面工具

`firstcoder.tools.hidden.HIDDEN_TOOL_STATUS_NAMES` 是唯一名单，列出不应刷进人机活动流的工具（当前是 `task_boundary`）。它们仍可调用，只是 UI/状态展示过滤。不要为此在 `tui.py` 里散落特判工具名。

## 执行规则

`ToolRegistry.execute(name, arguments)` 只解析一个名字并规范化失败；`PermissionAwareToolRegistry.execute` 先由 tool spec 生成 `PermissionRequest`，再拿 allow/ask/deny。`ASK` 只返回结构化信号、不执行；`AgentLoop` 存下原调用，用户回答后恢复。

loop 必须保持 provider 对话的配对顺序：

```text
assistant(tool_call id=call_1) -> tool(tool_call_id=call_1)
```

被拒绝、跳过、失败也要有第二条消息。不要在 UI 里伪造 tool result，也不要在 context 压缩时删掉它。

## 特殊工具

- `todo` 保存 UI 可见的计划协议；
- `think` 记录结构化 reasoning，不改工作区；
- `task_boundary` 判断新 task；hash 由程序生成，模型不能自报；
- `retrieve_archive` 仅能有界读取当前 session 的归档内容；
- `web_search` 优先 Parallel MCP，配置后可回退 Exa。

它们不是“顺手的小命令”，而是运行时参与者；改输出 schema 等于改变 context 和测试契约。

## 依赖规则

`tools`（以及 `permissions`、`utils`）可以为共享取消/用户输入类型 import `firstcoder.runtime`，但**不得** import `firstcoder.agent`。若 DTO 需要被 loop 上下层共用，放进 `runtime/`。

## 安全新增工具

1. 写小而真诚的 executor，返回 `ToolResult`。
2. 推导/校验 schema，并给出清晰 description。
3. 触碰本地或网络资源时注册 `ToolPermissionSpec`。
4. 放进正确 builtin 分类，别给 loop 加专属特判。
5. 覆盖成功、非法参数、denied/ask，必要时覆盖 provider 序列。

```sh
.venv/bin/python -m pytest tests/test_tools.py tests/test_schema.py \
  tests/test_introspection.py tests/test_permission_registry.py -q
```

## 排错地图

| 现象 | 先查 |
| --- | --- |
| 模型没看到工具 | builtin/session registry 或 provider capabilities |
| 参数 schema 不对 | introspection 或 curated definition |
| 没有预期确认就执行 | permission spec/wrapper/policy |
| 调用后 provider 拒绝历史 | tool result 缺失或 id 不匹配 |
| 单测能跑、真 session 不能 | session registry 装配 |

关联：[架构说明](ARCHITECTURE.zh-CN.md)、[权限设计](PERMISSIONS_DESIGN.zh-CN.md)、[Provider 设计](PROVIDERS_DESIGN.zh-CN.md)。
