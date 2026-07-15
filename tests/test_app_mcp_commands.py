from firstcoder.app.mcp_commands import McpCommandHandler
from firstcoder.mcp.models import McpServerStatus


class FakeMcpManager:
    def __init__(self) -> None:
        self.statuses_value = (
            McpServerStatus("lark", "connected", tool_count=2),
            McpServerStatus("broken", "failed", error="safe failure"),
        )

    def statuses(self):
        return self.statuses_value

    def doctor(self, name: str):
        return next((status for status in self.statuses_value if status.name == name), None)

    def reconnect(self, name: str | None = None) -> bool:
        self.reconnected = name
        return name is None or name == "lark"


def test_mcp_list_renders_safe_statuses_and_tool_counts() -> None:
    result = McpCommandHandler(FakeMcpManager()).handle("/mcp list")

    assert result.handled is True
    assert "MCP servers:" in result.output
    assert "lark: connected (2 tools)" in result.output
    assert "broken: failed (0 tools) - error" in result.output


def test_mcp_doctor_renders_one_server() -> None:
    result = McpCommandHandler(FakeMcpManager()).handle("/mcp doctor lark")

    assert result.handled is True
    assert result.output == "MCP lark: connected (2 tools)"


def test_mcp_command_reports_usage_and_unknown_server() -> None:
    handler = McpCommandHandler(FakeMcpManager())

    assert handler.handle("/mcp").output == "Usage: /mcp list | /mcp doctor <server> | /mcp reconnect <server|all>"
    assert handler.handle("/mcp doctor").output == "Usage: /mcp doctor <server>"
    assert handler.handle("/mcp doctor missing").output == "Unknown MCP server: missing"
    assert handler.handle("/mcp nonsense").output == "Usage: /mcp list | /mcp doctor <server> | /mcp reconnect <server|all>"


def test_mcp_reconnect_starts_a_named_server_or_all_servers() -> None:
    manager = FakeMcpManager()
    handler = McpCommandHandler(manager)

    named = handler.handle("/mcp reconnect lark")
    assert named.output == "Reconnecting MCP server: lark"
    assert manager.reconnected == "lark"

    all_servers = handler.handle("/mcp reconnect all")
    assert all_servers.output == "Reconnecting all MCP servers"
    assert manager.reconnected is None
