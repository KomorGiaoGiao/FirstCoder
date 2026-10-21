"""MCP slash 命令: list / doctor / reconnect, 只读状态快照, 不做配置变更。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from firstcoder.app.commands import CommandResult
from firstcoder.mcp.models import McpServerStatus


class McpStatusProvider(Protocol):
    """MCP 命令只需要安全的状态快照接口。"""

    def statuses(self) -> tuple[McpServerStatus, ...]: ...

    def doctor(self, name: str) -> McpServerStatus | None: ...

    def reconnect(self, name: str | None = None) -> bool: ...


@dataclass(slots=True)
class McpCommandHandler:
    """处理只读的 MCP 命令面。"""

    manager: McpStatusProvider

    def handle(self, text: str) -> CommandResult:
        """按 "子命令 + 参数个数" 逐个匹配, 匹配不到就落到用法提示。

        分发顺序: list -> doctor -> reconnect -> 兜底 usage。每个分支都返回
        handled=True, 只有开头不是 /mcp 时才放行给其他 handler。
        """

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
        # doctor: 探活单个 server, 名字不认识返回 None 时给出明确报错
        if len(parts) == 3 and parts[1] == "doctor":
            status = self.manager.doctor(parts[2])
            if status is None:
                return CommandResult(handled=True, output=f"Unknown MCP server: {parts[2]}")
            return CommandResult(handled=True, output=f"MCP {_render_status(status)}")
        if len(parts) == 2 and parts[1] == "doctor":
            return CommandResult(handled=True, output="Usage: /mcp doctor <server>")
        # reconnect: 支持 all 批量重连, 也支持按名单个重连; 返回值 False 表示
        # 没有可重连的目标 (未配置 / 已禁用)
        if len(parts) == 3 and parts[1] == "reconnect":
            if parts[2] == "all":
                if self.manager.reconnect():
                    return CommandResult(handled=True, output="Reconnecting all MCP servers")
                return CommandResult(handled=True, output="No enabled MCP servers to reconnect")
            if self.manager.reconnect(parts[2]):
                return CommandResult(handled=True, output=f"Reconnecting MCP server: {parts[2]}")
            return CommandResult(handled=True, output=f"Unknown or disabled MCP server: {parts[2]}")
        if len(parts) == 2 and parts[1] == "reconnect":
            return CommandResult(handled=True, output="Usage: /mcp reconnect <server|all>")
        # 参数个数不对等兜底情况: 不打回去让别的 handler 试, 直接给完整用法
        return CommandResult(handled=True, output="Usage: /mcp list | /mcp doctor <server> | /mcp reconnect <server|all>")


def _render_status(status: McpServerStatus) -> str:
    text = f"{status.name}: {status.state} ({status.tool_count} tools)"
    return f"{text} - error" if status.error else text
