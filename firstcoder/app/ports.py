"""app/TUI 边界的稳定 Protocol 端口。

TUI 只依赖这些 Protocol, 不直接引用 agent/session 的具体实现, 方便测试替身注入。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from firstcoder.context.manager import ContextCompactRequest, ContextCompactResult

if TYPE_CHECKING:
    from firstcoder.agent.loop_limits import AgentLoopLimits
    from firstcoder.agent.session import AgentSession
    from firstcoder.app.commands import CommandResult
    from firstcoder.input.attachments import UserAttachment
    from firstcoder.permissions.types import PermissionMode
    from firstcoder.providers.types import ChatResponse, MainRequestOptions
    from firstcoder.runtime.user_input import UserInputRequest


class CommandHandlerLike(Protocol):
    def handle(self, text: str) -> CommandResult: ...


class ChatRunnerLike(Protocol):
    """注意这里Protocol, 说明这是一个'接口定义'
    """
    @property
    def last_pending_input(self) -> UserInputRequest | None: ...

    limits: AgentLoopLimits | None
    request_options: MainRequestOptions

    def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse: ...

    def resume_with_user_input(self, request_id: str, answer: str) -> ChatResponse: ...

    async def arun_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> Any: ...

    async def aresume_with_user_input(self, request_id: str, answer: str) -> Any: ...


class CurrentSessionLike(Protocol):
    @property
    def session_id(self) -> str: ...

    session: AgentSession

    def set_permission_mode(self, mode: PermissionMode | str) -> PermissionMode: ...


class ContextManagerLike(Protocol):
    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult: ...
