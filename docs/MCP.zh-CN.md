# MCP 客户端

FirstCoder 的 MCP 是“外部工具扩展层”，不是第二套 agent、工具注册表或权限系统。已连接的
MCP tool 会被转换为普通 FirstCoder tool，名称固定为 `mcp__<server>__<tool>`。

## 整体链路

```text
firstcoder mcp add / TOML 配置
  -> AppConfig 读取想要连接的 server
  -> McpManager 连接并执行 tools/list
  -> adapter 转成现有 Tool
  -> app.factory.McpToolProvider 在装配期与 builtins 合并
  -> SessionBootstrap / session registry + PermissionAwareToolRegistry
  -> AgentLoop 调用工具并照常写入 session
```

MCP 在**组合根**合并进工具面，不会另起一套 registry 或 agent loop。失败/禁用的 server 只是不注入工具。

配置是持久化的“想连接什么”；`connected`、`failed`、`disabled` 是进程内运行状态，重启后会重新建立。

## 用 CLI 安装和管理

不想手改 TOML 时，使用下面命令写入全局 MCP 配置：

```sh
# 本地 stdio MCP；名称后的所有内容都是 command argv。
firstcoder mcp add everything npx -y @modelcontextprotocol/server-everything

# 远程 Streamable HTTP MCP。
firstcoder mcp add parallel --url https://search.parallel.ai/mcp

# local 使用环境变量，remote 使用 header；list 永不打印它们的值。
firstcoder mcp add local-db --env DATABASE_URL='{env:DATABASE_URL}' uvx my-db-mcp
firstcoder mcp add company --url https://mcp.example.com/mcp \
  --header 'Authorization=Bearer {env:COMPANY_MCP_TOKEN}'

firstcoder mcp list
firstcoder mcp remove everything
```

`firstcoder mcp list` 看的是保存的配置，不代表正在运行的连接。改完后重启 FirstCoder，
再使用下方 TUI 命令查看此进程的真实状态。

## 配置

可以在全局 `~/.config/firstcoder/config.toml` 或项目 `./firstcoder.toml`
中定义 server。项目里同名 server 会完整覆盖全局定义。

```toml
[mcp.local_echo]
type = "local"
command = ["python", "-m", "my_mcp_server"]
enabled = true
timeout_ms = 5000
env = { SERVICE_TOKEN = "{env:SERVICE_TOKEN}" }
allowed_tools = ["echo", "files_*"]

[mcp.company]
type = "remote"
url = "https://mcp.example.com/mcp"
headers = { Authorization = "Bearer {env:COMPANY_MCP_TOKEN}" }
enabled = true
timeout_ms = 8000

[mcp.github]
type = "remote"
url = "https://api.githubcopilot.com/mcp/"
bearer_token_env_var = "GITHUB_PAT_TOKEN"
```

`local` 以 stdio 启动 server，不经过 shell；配置的环境变量会叠加到宿主环境，
不会丢失 `PATH`。`remote` 使用 MCP SDK 的 Streamable HTTP client，并转发配置的
URL 与 headers。`allowed_tools` 可选，支持工具名 glob 过滤。

常见的 Bearer Token 认证使用 `bearer_token_env_var`；FirstCoder 会在连接时
自动加入 `Authorization: Bearer ...`。其他认证方案（例如 `X-API-Key`）继续使用 `headers`。

凭证请使用 `{env:NAME}`，不要直接写进配置。占位符只在真正连接时解析；变量缺失时，
对应 server 会安全失败，错误只会指出变量名，绝不会显示变量值。

## 权限与状态

每次 MCP 调用都使用 `mcp_tool` 权限动作，目标精确为 `<server>/<tool>`。标准模式和
激进模式默认都会暂停等待确认；只有 bypass 模式会自动放行。“始终允许”也仅适用于
这个精确的 server/tool 对。

在 TUI 中使用：

```text
/mcp list
/mcp doctor <server>
/mcp reconnect <server|all>
```

它们会显示连接状态、发现工具数和安全错误，不会输出配置 headers、已解析的环境变量
或其他秘密。server 失败、禁用或超时都不会阻止 FirstCoder 启动，只是不注入工具。

## 排障

- 先独立确认命令能作为 MCP stdio server 运行；普通日志必须写到 stderr，不能污染 stdout。
- 确认 `command` 是 argv 列表，remote URL 是 HTTP/HTTPS，server/tool 名只能包含字母、数字、`_`、`-`。
- 修改配置后先用 `/mcp doctor <server>` 检查；可用 `/mcp reconnect <server|all>` 在后台重新建立连接，无须重启 TUI。连接状态只存在进程内，不写入 session。
- 工具缺失时检查 `allowed_tools`，以及与内建或其他 MCP 工具的命名冲突。缺失秘密占位符时，在启动前 export 错误中点名的变量。

## 明确不支持

当前不实现 MCP resources、prompts、sampling、roots、elicitation、OAuth、插件市场或插件安装体系。
`mcp add` 只会安全写入 local/remote 配置，不会下载任意插件，也不会替你登录第三方账户；它也不会修改 FirstCoder 内建的 `web_search` 工具。

## 验证

```sh
.venv/bin/python -m pytest tests/test_mcp_integration.py -q
```

另见：[架构说明](ARCHITECTURE.zh-CN.md)、[工具设计](TOOLS_DESIGN.zh-CN.md)。
