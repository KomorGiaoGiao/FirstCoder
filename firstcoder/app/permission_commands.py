"""权限相关 slash command。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from firstcoder.app.commands import CommandResult
from firstcoder.permissions.types import PermissionMode


class PermissionSessionLike(Protocol):
    mode: str

    def set_permission_mode(self, mode: PermissionMode | str) -> PermissionMode:
        ...


@dataclass(slots=True)
class PermissionCommandHandler:
    """处理 `/mode` 权限策略切换。"""

    session: PermissionSessionLike

    def handle(self, text: str) -> CommandResult:
        command = " ".join(text.strip().split())
        if not command.startswith("/"):
            return CommandResult(handled=False)

        if command == "/mode":
            return CommandResult(
                handled=True,
                output=(
                    f"Permission mode: {self.session.mode}\n"
                    "Available: conservative, standard, aggressive, bypass"
                ),
            )

        if command.startswith("/mode "):
            raw_mode = command.split(" ", 1)[1].strip().lower()
            try:
                mode = self.session.set_permission_mode(raw_mode)
            except ValueError:
                return CommandResult(
                    handled=True,
                    output="Unknown permission mode. Available: conservative, standard, aggressive, bypass",
                )
            return CommandResult(handled=True, output=f"Permission mode set to: {mode.value}")

        return CommandResult(handled=False)
