"""MCP slash command handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from firstcoder.app.commands import CommandResult
from firstcoder.mcp.models import McpServerStatus


class McpStatusProvider(Protocol):
    """MCP commands need only safe status snapshots."""

    def statuses(self) -> tuple[McpServerStatus, ...]: ...

    def doctor(self, name: str) -> McpServerStatus | None: ...


@dataclass(slots=True)
class McpCommandHandler:
    """Handle the small, read-only MCP command surface."""

    manager: McpStatusProvider

    def handle(self, text: str) -> CommandResult:
        parts = text.strip().split()
        if not parts or parts[0] != "/mcp":
            return CommandResult(handled=False)
        if parts == ["/mcp", "list"]:
            statuses = self.manager.statuses()
            lines = ["MCP servers:"]
            lines.extend(_render_status(status) for status in statuses)
            if not statuses:
                lines.append("- none configured")
            return CommandResult(handled=True, output="\n".join(lines))
        if len(parts) == 3 and parts[1] == "doctor":
            status = self.manager.doctor(parts[2])
            if status is None:
                return CommandResult(handled=True, output=f"Unknown MCP server: {parts[2]}")
            return CommandResult(handled=True, output=f"MCP {_render_status(status)}")
        if len(parts) == 2 and parts[1] == "doctor":
            return CommandResult(handled=True, output="Usage: /mcp doctor <server>")
        return CommandResult(handled=True, output="Usage: /mcp list | /mcp doctor <server>")


def _render_status(status: McpServerStatus) -> str:
    text = f"{status.name}: {status.state} ({status.tool_count} tools)"
    return f"{text} - error" if status.error else text
