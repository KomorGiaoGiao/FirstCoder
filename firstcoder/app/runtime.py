"""TUI 运行期 session 状态和聊天入口。

Textual widget 只负责显示和输入；这里把“当前 session 可被 resume 替换”和“普通输入
调用 AgentLoop”封成很薄的一层，避免 UI 直接持有 agent 编排细节。
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio

from firstcoder.agent.cancellation import CancellationToken
from firstcoder.agent.loop import AgentLoop, ToolExecutionEvent
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession
from firstcoder.agent.user_input import AgentTurnStatus, UserInputRequest
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.manager import ContextCompactRequest
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.permissions.types import PermissionMode
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatResponse, ChatStreamEvent
from firstcoder.tools.types import Tool


_DEFAULT_MAX_TOOL_ROUNDS = object()
_HIDDEN_DISPLAY_TOOLS = {"task_boundary"}


@dataclass(slots=True)
class CurrentSessionState:
    """可替换的当前 session 代理。

    `ContextCommandHandler` 只需要 `session_id`、`runtime_state`、`current_turn` 和
    `rebuild_view()`；把这些属性代理出来后，`/resume` 只要替换内部 session，context
    命令自然会看见新会话。
    """

    session: AgentSession

    def set_session(self, session: AgentSession) -> None:
        self.session = session

    @property
    def session_id(self) -> str:
        return self.session.session_id

    @property
    def runtime_state(self) -> SessionRuntimeState:
        return self.session.runtime_state

    @property
    def current_turn(self) -> int:
        return self.session.current_turn

    def rebuild_view(self) -> SessionView:
        return self.session.rebuild_view()

    @property
    def mode(self) -> str:
        return self.session.mode

    def set_permission_mode(self, mode: PermissionMode | str) -> PermissionMode:
        return self.session.set_permission_mode(mode)


@dataclass(slots=True)
class AgentChatRunner:
    """普通聊天入口，把当前 session 交给 AgentLoop 执行一轮。"""

    current_session: CurrentSessionState
    provider: ChatProvider
    tools: list[Tool] | None = None
    tools_provider: Callable[[], list[Tool]] | None = None
    context_builder: ContextBuilder | None = None
    context_manager: Any | None = None
    limits: AgentLoopLimits | None = None
    max_tool_rounds: int | None | object = _DEFAULT_MAX_TOOL_ROUNDS
    use_streaming: bool = False
    loops: list[AgentLoop] = field(default_factory=list)
    last_display_lines: list[str] = field(default_factory=list)
    last_stream_events: list[ChatStreamEvent] = field(default_factory=list)
    last_pending_input: UserInputRequest | None = None
    stream_event_handler: Callable[[ChatStreamEvent], None] | None = None
    tool_event_handler: Callable[[ToolExecutionEvent], None] | None = None
    pending_guidance: list[str] = field(default_factory=list)
    _guidance_lock: threading.Lock = field(default_factory=threading.Lock)
    _cancellation_lock: threading.Lock = field(default_factory=threading.Lock)
    _active_cancellation_token: CancellationToken | None = None

    def set_provider(self, provider: ChatProvider, *, use_streaming: bool) -> None:
        self.provider = provider
        self.use_streaming = use_streaming
        self.last_stream_events = []

    def add_guidance(self, content: str) -> None:
        text = content.strip()
        if not text:
            return
        with self._guidance_lock:
            self.pending_guidance.append(text)

    def drain_guidance(self) -> list[str]:
        with self._guidance_lock:
            guidance = list(self.pending_guidance)
            self.pending_guidance.clear()
        return guidance

    def cancel_current_turn(self) -> None:
        with self._cancellation_lock:
            if self._active_cancellation_token is not None:
                self._active_cancellation_token.cancel()

    def _begin_cancellable_turn(self) -> CancellationToken:
        token = CancellationToken()
        with self._cancellation_lock:
            self._active_cancellation_token = token
        return token

    def _finish_cancellable_turn(self, token: CancellationToken) -> None:
        with self._cancellation_lock:
            if self._active_cancellation_token is token:
                self._active_cancellation_token = None

    def run_user_turn(self, content: str) -> ChatResponse:
        before_count = len(self.current_session.rebuild_view().messages)
        self.last_pending_input = None
        cancellation_token = self._begin_cancellable_turn()
        loop = AgentLoop(
            session=self.current_session.session,
            provider=self.provider,
            tools=self._current_tools(),
            context_builder=self.context_builder,
            context_manager=self.context_manager,
            limits=self.limits,
            tool_event_handler=self.tool_event_handler,
            guidance_provider=self.drain_guidance,
            cancellation_token=cancellation_token,
            **self._legacy_max_tool_rounds_kwargs(),
        )
        self.loops.append(loop)
        try:
            result = loop.run_user_turn_interactive(content)
        finally:
            self._finish_cancellable_turn(cancellation_token)
        self.last_stream_events = []
        self.last_pending_input = result.pending_input
        after_view = self.current_session.rebuild_view()
        self.last_display_lines = _display_lines_from_messages(after_view.messages[before_count:])
        if result.response is not None:
            return result.response
        response = ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content=result.pending_input.question if result.pending_input else "等待用户输入。",
            finish_reason=AgentTurnStatus.WAITING_FOR_USER_INPUT.value,
            raw={"pending_input": result.pending_input},
        )
        if response.content:
            self.last_display_lines.append(response.content)
        return response

    def resume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        """恢复等待中的权限确认。

        普通 `ask_user` 后续仍走新的用户消息；权限确认必须先补齐原 tool_call 的
        tool_result，所以 UI 通过这个入口把用户选择交回 agent loop。
        """

        before_count = len(self.current_session.rebuild_view().messages)
        self.last_pending_input = None
        cancellation_token = self._begin_cancellable_turn()
        loop = AgentLoop(
            session=self.current_session.session,
            provider=self.provider,
            tools=self._current_tools(),
            context_builder=self.context_builder,
            context_manager=self.context_manager,
            limits=self.limits,
            tool_event_handler=self.tool_event_handler,
            guidance_provider=self.drain_guidance,
            cancellation_token=cancellation_token,
            **self._legacy_max_tool_rounds_kwargs(),
        )
        self.loops.append(loop)
        try:
            result = loop.resume_with_user_input(request_id, answer)
        finally:
            self._finish_cancellable_turn(cancellation_token)
        self.last_stream_events = []
        self.last_pending_input = result.pending_input
        after_view = self.current_session.rebuild_view()
        self.last_display_lines = _display_lines_from_messages(after_view.messages[before_count:])
        if result.response is not None:
            if result.response.content and not self.last_display_lines:
                self.last_display_lines.append(result.response.content)
            return result.response
        response = ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content=result.pending_input.question if result.pending_input else "等待用户输入。",
            finish_reason=AgentTurnStatus.WAITING_FOR_USER_INPUT.value,
            raw={"pending_input": result.pending_input},
        )
        if response.content:
            self.last_display_lines.append(response.content)
        return response

    async def arun_user_turn(self, content: str) -> ChatResponse:
        """异步聊天入口。

        Textual 已经运行在 asyncio event loop 中，所以 UI 需要 await 这个入口；只有这里
        才会在 `use_streaming=True` 时消费 provider 的内部 stream event。
        """

        if self.use_streaming:
            before_count = len(self.current_session.rebuild_view().messages)
            self.last_pending_input = None
            cancellation_token = self._begin_cancellable_turn()
            loop = AgentLoop(
                session=self.current_session.session,
                provider=self.provider,
                tools=self._current_tools(),
                context_builder=self.context_builder,
                context_manager=self.context_manager,
                limits=self.limits,
                stream_event_handler=self.stream_event_handler,
                tool_event_handler=self.tool_event_handler,
                guidance_provider=self.drain_guidance,
                cancellation_token=cancellation_token,
                **self._legacy_max_tool_rounds_kwargs(),
            )
            self.loops.append(loop)
            self.last_display_lines = []
            self.last_stream_events = []
            try:
                response = await anyio.to_thread.run_sync(
                    _run_coroutine_in_thread,
                    loop.run_user_turn_streaming(content),
                )
            finally:
                self._finish_cancellable_turn(cancellation_token)
            self.last_stream_events = list(loop.last_stream_events)
            raw_pending = response.raw.get("pending_input") if isinstance(response.raw, dict) else None
            self.last_pending_input = raw_pending if isinstance(raw_pending, UserInputRequest) else None
            after_view = self.current_session.rebuild_view()
            self.last_display_lines = _display_lines_from_messages(after_view.messages[before_count:])
            if self.last_pending_input is not None and response.content:
                self.last_display_lines.append(response.content)
            return response

        return await asyncio.to_thread(self.run_user_turn, content)

    async def aresume_with_user_input(self, request_id: str, answer: str) -> ChatResponse:
        if self.use_streaming:
            before_count = len(self.current_session.rebuild_view().messages)
            self.last_pending_input = None
            cancellation_token = self._begin_cancellable_turn()
            loop = AgentLoop(
                session=self.current_session.session,
                provider=self.provider,
                tools=self._current_tools(),
                context_builder=self.context_builder,
                context_manager=self.context_manager,
                limits=self.limits,
                stream_event_handler=self.stream_event_handler,
                tool_event_handler=self.tool_event_handler,
                guidance_provider=self.drain_guidance,
                cancellation_token=cancellation_token,
                **self._legacy_max_tool_rounds_kwargs(),
            )
            self.loops.append(loop)
            self.last_display_lines = []
            self.last_stream_events = []
            try:
                result = await anyio.to_thread.run_sync(
                    _run_coroutine_in_thread,
                    loop.resume_with_user_input_streaming(request_id, answer),
                )
            finally:
                self._finish_cancellable_turn(cancellation_token)
            self.last_stream_events = list(loop.last_stream_events)
            self.last_pending_input = result.pending_input
            after_view = self.current_session.rebuild_view()
            self.last_display_lines = _display_lines_from_messages(after_view.messages[before_count:])
            if result.response is not None:
                return result.response
            response = ChatResponse(
                provider=self.provider.name,
                model=self.provider.model,
                content=result.pending_input.question if result.pending_input else "等待用户输入。",
                finish_reason=AgentTurnStatus.WAITING_FOR_USER_INPUT.value,
                raw={"pending_input": result.pending_input},
            )
            if response.content:
                self.last_display_lines.append(response.content)
            return response

        return await asyncio.to_thread(self.resume_with_user_input, request_id, answer)

    def _current_tools(self) -> list[Tool] | None:
        """Resolve tools once per loop so the session registry sees that same list."""

        return self.tools_provider() if self.tools_provider is not None else self.tools

    def _legacy_max_tool_rounds_kwargs(self) -> dict[str, int | None | object]:
        if self.max_tool_rounds is _DEFAULT_MAX_TOOL_ROUNDS:
            return {}
        return {"max_tool_rounds": self.max_tool_rounds}


def _display_lines_from_messages(messages: list[AgentMessage]) -> list[str]:
    """把一轮新增事实压成 TUI 可读的短行。

    这里不重新编排 agent，只读取本轮已经落到 event log 的消息。这样 TUI 可以看到
    tool call/result 摘要，又不会知道 provider/tool 协议细节。
    """

    lines: list[str] = []
    for message in messages:
        if message.role == "assistant":
            lines.extend(_assistant_lines(message.parts))
        elif message.role == "tool":
            lines.extend(_tool_lines(message.parts))
    return lines


def _run_coroutine_in_thread(coro):
    return asyncio.run(coro)


def _assistant_lines(parts: list[MessagePart]) -> list[str]:
    lines: list[str] = []
    for part in parts:
        if part.kind == "text" and part.content:
            lines.append(part.content)
        elif part.kind == "tool_call":
            metadata = part.metadata
            name = str(metadata.get("tool_name") or "tool")
            if name in _HIDDEN_DISPLAY_TOOLS:
                continue
            arguments = json.dumps(metadata.get("arguments") or {}, ensure_ascii=False, sort_keys=True)
            lines.append(f"Tool call: {name} {_truncate(arguments, 400)}")
    return lines


def _tool_lines(parts: list[MessagePart]) -> list[str]:
    lines: list[str] = []
    for part in parts:
        if part.kind != "tool_result":
            continue
        metadata = part.metadata
        name = str(metadata.get("tool_name") or "tool")
        if name in _HIDDEN_DISPLAY_TOOLS:
            continue
        status = "success" if metadata.get("ok", True) else "failed"
        content = _truncate(part.content, 400)
        lines.append(f"Tool result: {name} {status}: {content}")
    return lines


def _truncate(text: str, max_chars: int) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return "." * max_chars
    return normalized[: max_chars - 3] + "..."
