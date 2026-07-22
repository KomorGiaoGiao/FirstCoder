"""Stable protocol ports for the app/TUI boundary."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from firstcoder.context.manager import ContextCompactRequest, ContextCompactResult

if TYPE_CHECKING:
    from firstcoder.app.commands import CommandResult
    from firstcoder.input.attachments import UserAttachment


class CommandHandlerLike(Protocol):
    def handle(self, text: str) -> CommandResult: ...


class ChatRunnerLike(Protocol):
    last_pending_input: object | None

    def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> Any: ...

    def resume_with_user_input(self, request_id: str, answer: str) -> Any: ...


class CurrentSessionLike(Protocol):
    session_id: str


class ContextManagerLike(Protocol):
    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult: ...
