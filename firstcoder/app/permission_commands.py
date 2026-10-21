"""权限相关 slash command。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from firstcoder.app.commands import CommandResult
from firstcoder.permissions.types import PermissionMode


class PermissionSessionLike(Protocol):
    @property
    def mode(self) -> str: ...

    def set_permission_mode(self, mode: PermissionMode | str) -> PermissionMode: ...


@dataclass(slots=True)
class PermissionCommandHandler:
    """处理 `/mode` 权限策略切换。"""

    session: PermissionSessionLike

    def handle(self, text: str) -> CommandResult:
        """/mode 查看当前权限模式; /mode <mode> 切换, 未知模式给出可用列表。

        切换立即作用于当前 session(经 PermissionSessionLike 代理); 非 /mode
        前缀返回 handled=False 让位给后续 handler。
        """
        command = " ".join(text.strip().split())
        if not command.startswith("/"):
            return CommandResult(handled=False)

        if command == "/mode":
            return CommandResult(
                handled=True,
                output=(f"Permission mode: {self.session.mode}\n" "Available: standard, aggressive, bypass"),
            )

        if command.startswith("/mode "):
            raw_mode = command.split(" ", 1)[1].strip().lower()
            try:
                mode = self.session.set_permission_mode(raw_mode)
            except ValueError:
                # set_permission_mode 对未知模式抛 ValueError, 转成用户可见提示
                return CommandResult(
                    handled=True,
                    output="Unknown permission mode. Available: standard, aggressive, bypass",
                )
            return CommandResult(handled=True, output=f"Permission mode set to: {mode.value}")

        return CommandResult(handled=False)
