# MCP Client

FirstCoder can discover tools from configured MCP servers and expose them to the
agent through the existing tool registry. MCP is an extension boundary for
tools only: discovered tools are named `mcp__<server>__<tool>` and always pass
through the normal permission manager.

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
```

`local` launches a stdio server without a shell. Its configured environment is
added to the host environment, so the command can still find `PATH`. `remote`
uses the MCP SDK Streamable HTTP client and forwards the configured URL and
headers. `allowed_tools` is optional and accepts tool-name glob patterns.

Use `{env:NAME}` for credentials rather than putting them in configuration.
Placeholders are resolved only while connecting; if one is absent, that server
fails safely and the message identifies only the variable name, never its
value.

## Permissions and status

Every MCP call has the `mcp_tool` permission action and an exact target of
`<server>/<tool>`. In standard and aggressive modes it pauses for confirmation
by default; bypass mode is the sole automatic path. An explicit “allow always”
grant is limited to that exact server/tool pair.

Use these commands in the TUI or interactive client:

```text
/mcp list
/mcp doctor <server>
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
- Run `/mcp doctor <server>` after changing configuration. Restart FirstCoder
  to reconnect: connection state is process-local and is not stored in a
  session.
- If a tool is missing, inspect `allowed_tools` and name collisions with
  built-in or another MCP tool. If a secret placeholder is missing, export the
  named variable before launch.

## Deliberately unsupported

This client does not implement MCP resources, prompts, sampling, roots,
elicitation, OAuth, or a plugin marketplace/installation system. It also does
not alter FirstCoder's built-in `web_search` tool.

## Verification

```sh
.venv/bin/python -m pytest tests/test_mcp_integration.py -q
```
