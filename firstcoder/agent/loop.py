"""Agent 主循环最小闭环。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from firstcoder.agent.session import AgentSession
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.manager import ContextCompactRequest, ContextWindowTrigger
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatRequest, ChatResponse, ChatStreamEvent, ToolCall
from firstcoder.tools.types import Tool


class ContextManagerLike(Protocol):
    def compact_if_needed(self, request: ContextCompactRequest):
        ...


class AgentLoop:
    """把用户输入、上下文投影、provider 调用和工具执行串成一轮会话。

    当前只实现上下文闭环需要的最小同步流程：用户消息落库、构造 system prefix、投影
    provider messages、处理一轮或多轮 tool calls。自动压缩和 provider 错误恢复留给后续
    `ContextWindowManager` 阶段接入。
    """

    def __init__(
        self,
        *,
        session: AgentSession,
        provider: ChatProvider,
        tools: list[Tool] | None = None,
        context_builder: ContextBuilder | None = None,
        context_manager: ContextManagerLike | None = None,
        max_tool_rounds: int = 4,
    ) -> None:
        self.session = session
        self.provider = provider
        self.context_builder = context_builder or ContextBuilder()
        self.context_manager = context_manager
        self.max_tool_rounds = max_tool_rounds
        self.last_stream_events: list[ChatStreamEvent] = []
        if tools:
            for tool in tools:
                if tool.name not in self.session.tool_registry.names():
                    self.session.tool_registry.register(tool)

    def run_user_turn(self, content: str) -> ChatResponse:
        self.session.append_user_message(content)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

        response = self._complete_once()
        tool_rounds = 0
        while response.tool_calls:
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break

            self.session.append_assistant_response(response)
            task_hash_changed = self._execute_tool_calls(response.tool_calls)
            if task_hash_changed:
                self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)
            self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break
            response = self._complete_once()

        self.session.append_assistant_response(response)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return response

    async def run_user_turn_streaming(self, content: str) -> ChatResponse:
        """使用 provider 内部 stream event 协议执行一轮会话。

        文本 delta 可以被上层即时展示，但工具调用仍保持原子语义：只有 stream 完成并
        返回完整 `ChatResponse.tool_calls` 后，才写入 assistant message 并执行工具。
        """

        self.last_stream_events = []
        self.session.append_user_message(content)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

        response = await self._stream_once()
        tool_rounds = 0
        while response.tool_calls:
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break

            self.session.append_assistant_response(response)
            task_hash_changed = self._execute_tool_calls(response.tool_calls)
            if task_hash_changed:
                self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)
            self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break
            response = await self._stream_once()

        self.session.append_assistant_response(response)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return response

    def run_user_turn_streaming_sync(self, content: str) -> ChatResponse:
        """同步入口，仅用于测试或没有运行中 event loop 的 CLI 场景。

        Textual 这类已经运行 asyncio event loop 的 UI 后续应该直接 await
        `run_user_turn_streaming()` 或放到 worker 中执行，不能调用这个包装方法。
        """

        return asyncio.run(self.run_user_turn_streaming(content))

    def _complete_once(self) -> ChatResponse:
        definitions = self.session.tool_registry.definitions()
        system_prefix = self.session.build_system_prefix(
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            tools=definitions,
        )
        messages = self.context_builder.build_provider_messages(
            self.session.rebuild_view(),
            system_prefix=system_prefix,
        )
        return self.provider.complete(ChatRequest(messages=messages, tools=definitions))

    async def _stream_once(self) -> ChatResponse:
        definitions = self.session.tool_registry.definitions()
        system_prefix = self.session.build_system_prefix(
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            tools=definitions,
        )
        messages = self.context_builder.build_provider_messages(
            self.session.rebuild_view(),
            system_prefix=system_prefix,
        )
        final_response: ChatResponse | None = None
        async for event in self.provider.astream(ChatRequest(messages=messages, tools=definitions)):
            self.last_stream_events.append(event)
            if event.kind == "message_completed":
                final_response = event.response
        if final_response is None:
            raise ProviderError(
                ProviderErrorKind.API_ERROR,
                "provider stream ended without message_completed event",
            )
        return final_response

    def _execute_tool_calls(self, tool_calls: list[ToolCall]) -> bool:
        task_hash_changed = False
        for tool_call in tool_calls:
            result = self.session.execute_tool_call(tool_call)
            self.session.append_tool_result(tool_call=tool_call, result=result)
            if tool_call.name == "task_boundary" and result.ok and result.data.get("should_trigger_compaction"):
                task_hash_changed = True
        return task_hash_changed

    def _compact_if_needed(self, *, trigger: ContextWindowTrigger) -> None:
        if self.context_manager is None:
            return
        self.context_manager.compact_if_needed(
            ContextCompactRequest(
                view=self.session.rebuild_view(),
                runtime_state=self.session.runtime_state,
                trigger=trigger,
                current_turn=self.session.current_turn,
            )
        )

    def _tool_round_limit_response(self, response: ChatResponse) -> ChatResponse:
        """工具轮次上限命中后，只保存纯文本说明，避免写入未执行的 tool_call。"""

        return ChatResponse(
            provider=response.provider,
            model=response.model,
            content=f"工具调用轮次达到上限（max_tool_rounds={self.max_tool_rounds}），已停止继续执行工具。",
            tool_calls=[],
            finish_reason="tool_round_limit",
            raw=response.raw,
        )
