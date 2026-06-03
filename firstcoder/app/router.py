"""多个 slash command handler 的组合入口。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from firstcoder.app.commands import CommandResult


class CommandHandlerLike(Protocol):
    def handle(self, text: str) -> CommandResult:
        ...


@dataclass(slots=True)
class CompositeCommandHandler:
    handlers: list[CommandHandlerLike]

    def handle(self, text: str) -> CommandResult:
        handled_any = False
        for handler in self.handlers:
            result = handler.handle(text)
            if result.handled:
                return result
            handled_any = handled_any or result.handled
        if text.strip().startswith("/"):
            return CommandResult(handled=True, output=f"Unknown command: {' '.join(text.strip().split())}")
        return CommandResult(handled=handled_any)
