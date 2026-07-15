"""真实 MCP stdio 与远程配置边界的集成测试。"""

from __future__ import annotations

import sys
from pathlib import Path

from firstcoder.mcp.adapter import adapt_mcp_tool
from firstcoder.mcp.manager import McpManager
from firstcoder.mcp.models import McpLocalServerConfig, McpRemoteServerConfig, McpToolDescription
from firstcoder.permissions.manager import PermissionManager
from firstcoder.permissions.policy import DefaultPermissionPolicy
from firstcoder.permissions.types import PermissionConfirmationChoice
from firstcoder.permissions.types import PermissionDecisionKind
from firstcoder.tools.permission_registry import PermissionAwareToolRegistry
from firstcoder.tools.registry import ToolRegistry


def test_stdio_echo_tool_requires_confirmation_then_executes_after_explicit_allow(tmp_path) -> None:
    fixture = Path(__file__).parent / "fixtures" / "mcp_echo_server.py"
    manager = McpManager(
        (
            McpLocalServerConfig(
                name="echo",
                command=(sys.executable, str(fixture)),
                timeout_ms=5_000,
            ),
        )
    )
    try:
        manager.connect_all()
        assert manager.doctor("echo").state == "connected"
        discovered = dict(manager.tools())["echo"]
        assert discovered.name == "echo"

        tool = adapt_mcp_tool(manager, "echo", discovered)
        permissions = PermissionManager(policy=DefaultPermissionPolicy(tmp_path))
        registry = PermissionAwareToolRegistry(ToolRegistry([tool]), permissions)

        paused = registry.execute("mcp__echo__echo", {"message": "hello MCP"})

        assert paused.data["requires_user_input"] is True
        assert paused.data["permission_request"]["action"] == "mcp_tool"
        assert paused.data["permission_request"]["target"] == "echo/echo"

        pending = registry.preflight("mcp__echo__echo", {"message": "hello MCP"})
        assert pending is not None
        _, arguments, request, _ = pending
        allowed = permissions.resolve_confirmation(request, PermissionConfirmationChoice.ALLOW_ONCE.value)
        assert allowed.kind == PermissionDecisionKind.ALLOW

        result = registry.execute_without_permission_check("mcp__echo__echo", arguments)

        assert result.ok is True
        assert result.content == "hello MCP"
    finally:
        manager.close()


class _RemoteTransport:
    async def connect(self) -> None:
        return None

    async def list_tools(self) -> tuple[McpToolDescription, ...]:
        return (McpToolDescription("echo", "Echo text."),)

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        return {"content": [{"type": "text", "text": str(arguments["message"])}]}

    async def close(self) -> None:
        return None


class _CapturingRemoteFactory:
    def __init__(self) -> None:
        self.config: McpRemoteServerConfig | None = None

    def create(self, config: McpLocalServerConfig | McpRemoteServerConfig) -> _RemoteTransport:
        assert isinstance(config, McpRemoteServerConfig)
        self.config = config
        return _RemoteTransport()


def test_remote_config_forwards_url_and_headers_without_leaking_header_value() -> None:
    secret = "Bearer secret-value-that-must-not-appear"
    factory = _CapturingRemoteFactory()
    manager = McpManager(
        (
            McpRemoteServerConfig(
                name="remote",
                url="https://example.test/mcp",
                headers={"Authorization": secret},
            ),
        ),
        transport_factory=factory,
    )
    try:
        manager.connect_all()

        assert factory.config is not None
        assert factory.config.url == "https://example.test/mcp"
        assert factory.config.headers == {"Authorization": secret}
        status = manager.doctor("remote")
        assert status is not None
        assert status.state == "connected"
        assert secret not in repr(status)
        assert secret not in str(manager.statuses())
    finally:
        manager.close()
