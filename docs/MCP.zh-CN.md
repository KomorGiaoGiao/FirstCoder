# MCP 客户端

FirstCoder 可以从配置的 MCP server 发现工具，并通过现有工具注册表提供给 agent。MCP
在这里仅扩展“工具”：发现到的工具命名为 `mcp__<server>__<tool>`，并且始终经过原有权限管理器。

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
```

`local` 以 stdio 启动 server，不经过 shell；配置的环境变量会叠加到宿主环境，
不会丢失 `PATH`。`remote` 使用 MCP SDK 的 Streamable HTTP client，并转发配置的
URL 与 headers。`allowed_tools` 可选，支持工具名 glob 过滤。

凭证请使用 `{env:NAME}`，不要直接写进配置。占位符只在真正连接时解析；变量缺失时，
对应 server 会安全失败，错误只会指出变量名，绝不会显示变量值。

## 权限与状态

每次 MCP 调用都使用 `mcp_tool` 权限动作，目标精确为 `<server>/<tool>`。标准模式和
激进模式默认都会暂停等待确认；只有 bypass 模式会自动放行。“始终允许”也仅适用于
这个精确的 server/tool 对。

在 TUI 或行式交互客户端中使用：

```text
/mcp list
/mcp doctor <server>
```

它们会显示连接状态、发现工具数和安全错误，不会输出配置 headers、已解析的环境变量
或其他秘密。server 失败、禁用或超时都不会阻止 FirstCoder 启动，只是不注入工具。

## 排障

- 先独立确认命令能作为 MCP stdio server 运行；普通日志必须写到 stderr，不能污染 stdout。
- 确认 `command` 是 argv 列表，remote URL 是 HTTP/HTTPS，server/tool 名只能包含字母、数字、`_`、`-`。
- 修改配置后用 `/mcp doctor <server>` 检查；重启 FirstCoder 才会重新连接，因为连接状态只存在进程内，不写入 session。
- 工具缺失时检查 `allowed_tools`，以及与内建或其他 MCP 工具的命名冲突。缺失秘密占位符时，在启动前 export 错误中点名的变量。

## 明确不支持

当前不实现 MCP resources、prompts、sampling、roots、elicitation、OAuth、插件市场或插件安装体系，也不会修改 FirstCoder 内建的 `web_search` 工具。

## 验证

```sh
.venv/bin/python -m pytest tests/test_mcp_integration.py -q
```
