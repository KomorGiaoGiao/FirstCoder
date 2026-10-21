"""多个 slash command handler 的组合入口。"""

from __future__ import annotations

from dataclasses import dataclass

from firstcoder.app.commands import CommandResult
from firstcoder.app.ports import CommandHandlerLike


@dataclass(slots=True)
class CompositeCommandHandler:
    """按注册顺序委托各 handler, 第一个 handled 的胜出; 未匹配的 "/" 输入报 Unknown command。"""

    handlers: list[CommandHandlerLike]

    def handle(self, text: str) -> CommandResult:
        for handler in self.handlers:
            result = handler.handle(text)
            if result.handled:
                return result
        if text.strip().startswith("/"):
            return CommandResult(handled=True, output=f"Unknown command: {' '.join(text.strip().split())}")
        return CommandResult(handled=False)
