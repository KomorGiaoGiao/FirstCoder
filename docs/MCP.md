# MCP Client

FirstCoder's MCP client is a configuration-driven extension boundary for
external tools. It deliberately does not create a second agent loop, tool
registry, or permission system: a connected MCP tool becomes a normal
FirstCoder tool named `mcp__<server>__<tool>`.

## Mental model

```text
firstcoder mcp add / TOML configuration
  -> AppConfig loads desired servers
  -> McpManager connects and calls tools/list
  -> adapter creates regular Tool objects
  -> app.factory.McpToolProvider merges them with builtins at composition time
  -> SessionBootstrap / session registry + PermissionAwareToolRegistry
  -> AgentLoop calls the tool and records the normal session events
```

MCP is merged at the **composition root**, not by inventing a second tool
registry or agent loop. Failed or disabled servers simply omit tools.

Configuration is persistent intent. Connection state (`connected`, `failed`,
or `disabled`) is process-local and is rebuilt when FirstCoder starts.

## Install and manage servers from the CLI

Use the CLI to write global MCP configuration without editing TOML:

```sh
# Local stdio server. Everything after the name is its command argv.
firstcoder mcp add everything npx -y @modelcontextprotocol/server-everything

# Remote Streamable HTTP server.
firstcoder mcp add parallel --url https://search.parallel.ai/mcp

# Local variables and remote headers are kept in TOML but never printed by list.
firstcoder mcp add local-db --env DATABASE_URL='{env:DATABASE_URL}' uvx my-db-mcp
firstcoder mcp add company --url https://mcp.example.com/mcp \
  --header 'Authorization=Bearer {env:COMPANY_MCP_TOKEN}'

firstcoder mcp list
firstcoder mcp remove everything
```

`firstcoder mcp list` shows saved configuration, not a live connection. Restart
FirstCoder after changing it, then use the TUI commands below to inspect the
running process.

## Configuration

Put server definitions in either global `~/.config/firstcoder/config.toml` or
project `./firstcoder.toml`. A project definition with the same server name
completely replaces the global definition.

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

`local` launches a stdio server without a shell. Its configured environment is
added to the host environment, so the command can still find `PATH`. `remote`
uses the MCP SDK Streamable HTTP client and forwards the configured URL and
headers. `allowed_tools` is optional and accepts tool-name glob patterns.

For the common Bearer-token scheme, use `bearer_token_env_var`; FirstCoder
adds the `Authorization: Bearer ...` header at connection time. Use `headers`
for other schemes such as `X-API-Key`.

Use `{env:NAME}` for credentials rather than putting them in configuration.
Placeholders are resolved only while connecting; if one is absent, that server
fails safely and the message identifies only the variable name, never its
value.

## Permissions and status

Every MCP call has the `mcp_tool` permission action and an exact target of
`<server>/<tool>`. In standard and aggressive modes it pauses for confirmation
by default; bypass mode is the sole automatic path. An explicit “allow always”
grant is limited to that exact server/tool pair.

Use these commands in the TUI:

```text
/mcp list
/mcp doctor <server>
/mcp reconnect <server|all>
```

They show connection state, discovered tool count, and safe errors. They do
not print configured headers, resolved environment values, or other secrets.
A failed, disabled, or timed-out server does not block startup and contributes
no tools.

## Troubleshooting

- Confirm the command works as an MCP stdio server when run independently;
  ordinary logs must go to stderr, not stdout.
- Check that the configured `command` is an argv list, the remote URL is HTTP
  or HTTPS, and server/tool names contain only letters, numbers, `_`, or `-`.
- Run `/mcp doctor <server>` after changing configuration. Use
  `/mcp reconnect <server|all>` to reconnect in the background without
  restarting the TUI. Connection state is process-local and is not stored in a
  session.
- If a tool is missing, inspect `allowed_tools` and name collisions with
  built-in or another MCP tool. If a secret placeholder is missing, export the
  named variable before launch.

## Deliberately unsupported

This client does not implement MCP resources, prompts, sampling, roots,
elicitation, OAuth, or a plugin marketplace/installation system. `mcp add`
only writes safe local/remote configuration; it never downloads an arbitrary
plugin or authenticates to a third-party account. It also does not alter
FirstCoder's built-in `web_search` tool.

## Verification

```sh
.venv/bin/python -m pytest tests/test_mcp_integration.py -q
```

See also: [Architecture](ARCHITECTURE.md) and [Tools](TOOLS_DESIGN.md).
