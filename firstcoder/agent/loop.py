"""Agent 主循环最小闭环。"""

from __future__ import annotations

import asyncio
from typing import Protocol

from firstcoder.agent.session import AgentSession, PendingPermissionExecution
from firstcoder.agent.user_input import (
    AgentTurnResult,
    AgentTurnStatus,
    UserInputRequest,
    user_input_request_from_tool_result,
)
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.manager import ContextCompactRequest, ContextWindowTrigger
from firstcoder.permissions.types import PermissionDecisionKind, PermissionRequest
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatRequest, ChatResponse, ChatStreamEvent, ToolCall
from firstcoder.tools.permission_results import make_permission_denied_result
from firstcoder.tools.types import Tool, make_error_result


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
        result = self.run_user_turn_interactive(content)
        if result.response is not None:
            return result.response
        pending = result.pending_input
        content = pending.question if pending is not None else "等待用户输入。"
        return ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content=content,
            finish_reason=AgentTurnStatus.WAITING_FOR_USER_INPUT.value,
            raw={"pending_input": pending},
        )

    def run_user_turn_interactive(self, content: str) -> AgentTurnResult:
        """执行一轮会话，并在工具请求用户输入时暂停。

        旧的 `run_user_turn()` 保持返回 `ChatResponse`，方便现有测试和非交互入口
        继续工作。需要权限确认或 `ask_user` 暂停语义的上层应使用这个入口。
        """

        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self._permission_input_request_from_pending(pending),
            )

        self.session.append_user_message(content)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

        return self._run_tool_loop_interactive(self._complete_once_with_recovery)

    def resume_with_user_input(self, request_id: str, answer: str) -> AgentTurnResult:
        """用用户回答恢复一个暂停中的权限确认。

        普通 `ask_user` 第一版仍通过“下一条用户消息”继续；权限确认不能这样做，
        因为模型原始 tool_call 已经在历史里等待一个匹配的 tool_result。这里必须先
        用本地 pending 状态补齐最终 tool_result，再继续下一次 provider 调用。
        """

        pending = self.session.pending_permission_execution
        if pending is None or pending.request_id != request_id:
            return AgentTurnResult(
                status=AgentTurnStatus.COMPLETED,
                response=ChatResponse(
                    provider=self.provider.name,
                    model=self.provider.model,
                    content="没有找到可恢复的权限确认请求。",
                    finish_reason="error",
                ),
            )
        if self.session.permission_manager is None:
            return AgentTurnResult(
                status=AgentTurnStatus.COMPLETED,
                response=ChatResponse(
                    provider=self.provider.name,
                    model=self.provider.model,
                    content="当前会话没有权限管理器，无法恢复权限确认。",
                    finish_reason="error",
                ),
            )

        decision = self.session.permission_manager.resolve_confirmation(pending.permission_request, answer)
        if decision.kind == PermissionDecisionKind.DENY:
            result = make_permission_denied_result(
                tool_name=pending.tool_call.name,
                request=pending.permission_request,
                decision=decision,
            )
        else:
            result = self.session.execute_tool_call_after_permission_confirmation(pending.tool_call)

        self.session.pending_permission_execution = None
        self.session.append_tool_result(tool_call=pending.tool_call, result=result)
        self._append_skipped_tool_results(pending.skipped_tool_calls)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return self._run_tool_loop_interactive(self._complete_once_with_recovery)

    async def run_user_turn_streaming(self, content: str) -> ChatResponse:
        """使用 provider 内部 stream event 协议执行一轮会话。

        文本 delta 可以被上层即时展示，但工具调用仍保持原子语义：只有 stream 完成并
        返回完整 `ChatResponse.tool_calls` 后，才写入 assistant message 并执行工具。
        """

        self.last_stream_events = []
        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            pending_input = self._permission_input_request_from_pending(pending)
            return ChatResponse(
                provider=self.provider.name,
                model=self.provider.model,
                content=pending_input.question,
                finish_reason=AgentTurnStatus.WAITING_FOR_USER_INPUT.value,
                raw={"pending_input": pending_input},
            )

        self.session.append_user_message(content)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

        result = await self._run_tool_loop_interactive_async(self._stream_once_with_recovery)
        if result.response is not None:
            return result.response
        pending = result.pending_input
        content = pending.question if pending is not None else "等待用户输入。"
        return ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content=content,
            finish_reason=AgentTurnStatus.WAITING_FOR_USER_INPUT.value,
            raw={"pending_input": pending},
        )

    def run_user_turn_streaming_sync(self, content: str) -> ChatResponse:
        """同步入口，仅用于测试或没有运行中 event loop 的 CLI 场景。

        Textual 这类已经运行 asyncio event loop 的 UI 后续应该直接 await
        `run_user_turn_streaming()` 或放到 worker 中执行，不能调用这个包装方法。
        """

        return asyncio.run(self.run_user_turn_streaming(content))

    def _complete_once(self) -> ChatResponse:
        definitions = self._provider_tool_definitions()
        system_prefix = self.session.build_system_prefix(
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            provider_capabilities=getattr(self.provider, "capabilities", None),
            tools=definitions,
        )
        messages = self.context_builder.build_provider_messages(
            self.session.rebuild_view(),
            system_prefix=system_prefix,
        )
        return self.provider.complete(ChatRequest(messages=messages, tools=definitions))

    def _complete_once_with_recovery(self) -> ChatResponse:
        try:
            return self._complete_once()
        except ProviderError as exc:
            if not exc.requires_compaction:
                raise
            result = self._compact_if_needed(trigger=ContextWindowTrigger.PROMPT_TOO_LONG)
            if result is None or result.status != "success":
                raise
            return self._complete_once()

    async def _stream_once(self) -> ChatResponse:
        definitions = self._provider_tool_definitions()
        system_prefix = self.session.build_system_prefix(
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            provider_capabilities=getattr(self.provider, "capabilities", None),
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

    async def _stream_once_with_recovery(self) -> ChatResponse:
        try:
            return await self._stream_once_attempt()
        except ProviderError as exc:
            if not exc.requires_compaction:
                raise
            result = self._compact_if_needed(trigger=ContextWindowTrigger.PROMPT_TOO_LONG)
            if result is None or result.status != "success":
                raise
            return await self._stream_once_attempt()

    async def _stream_once_attempt(self) -> ChatResponse:
        start_event_count = len(self.last_stream_events)
        try:
            return await self._stream_once()
        except ProviderError:
            del self.last_stream_events[start_event_count:]
            raise

    def _run_tool_loop_interactive(self, complete_once) -> AgentTurnResult:
        response = self._drop_unsupported_tool_calls(complete_once())
        tool_rounds = 0
        while response.tool_calls:
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break

            self.session.append_assistant_response(response)
            execution = self._execute_tool_calls_interactive(response.tool_calls)
            if execution.pending_input is not None:
                return AgentTurnResult(
                    status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                    pending_input=execution.pending_input,
                )
            if execution.task_hash_changed:
                self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)
            self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break
            response = self._drop_unsupported_tool_calls(complete_once())

        self.session.append_assistant_response(response)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)

    async def _run_tool_loop_interactive_async(self, complete_once) -> AgentTurnResult:
        response = self._drop_unsupported_tool_calls(await complete_once())
        tool_rounds = 0
        while response.tool_calls:
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break

            self.session.append_assistant_response(response)
            execution = self._execute_tool_calls_interactive(response.tool_calls)
            if execution.pending_input is not None:
                return AgentTurnResult(
                    status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                    pending_input=execution.pending_input,
                )
            if execution.task_hash_changed:
                self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)
            self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

            tool_rounds += 1
            if tool_rounds >= self.max_tool_rounds:
                response = self._tool_round_limit_response(response)
                break
            response = self._drop_unsupported_tool_calls(await complete_once())

        self.session.append_assistant_response(response)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)

    def _execute_tool_calls(self, tool_calls: list[ToolCall]) -> bool:
        return self._execute_tool_calls_interactive(tool_calls).task_hash_changed

    def _execute_tool_calls_interactive(self, tool_calls: list[ToolCall]) -> "_ToolExecutionState":
        task_hash_changed = False
        for index, tool_call in enumerate(tool_calls):
            preflight = self.session.preflight_tool_call_permission(tool_call)
            if preflight is not None:
                if preflight.decision.kind == PermissionDecisionKind.DENY:
                    result = make_permission_denied_result(
                        tool_name=tool_call.name,
                        request=preflight.request,
                        decision=preflight.decision,
                    )
                    self.session.append_tool_result(tool_call=tool_call, result=result)
                    continue
                if preflight.decision.kind == PermissionDecisionKind.ASK:
                    pending_input = self._store_pending_permission_request(
                        tool_call=tool_call,
                        request=preflight.request,
                        skipped_tool_calls=tool_calls[index + 1 :],
                    )
                    return _ToolExecutionState(
                        task_hash_changed=task_hash_changed,
                        pending_input=pending_input,
                    )

            result = self.session.execute_tool_call(tool_call)
            self.session.append_tool_result(tool_call=tool_call, result=result)
            pending_input = user_input_request_from_tool_result(
                result,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
            )
            if pending_input is not None:
                self._append_skipped_tool_results(tool_calls[index + 1 :])
                return _ToolExecutionState(
                    task_hash_changed=task_hash_changed,
                    pending_input=pending_input,
                )
            if tool_call.name == "task_boundary" and result.ok and result.data.get("should_trigger_compaction"):
                task_hash_changed = True
        return _ToolExecutionState(task_hash_changed=task_hash_changed)

    def _store_pending_permission_request(
        self,
        *,
        tool_call: ToolCall,
        request: PermissionRequest,
        skipped_tool_calls: list[ToolCall],
    ) -> UserInputRequest:
        if self.session.permission_manager is None:
            raise RuntimeError("permission confirmation requires a permission manager")

        confirmation = self.session.permission_manager.build_confirmation(request)
        confirmation.payload["pending_tool_call"] = {
            "id": tool_call.id,
            "name": tool_call.name,
            "arguments": tool_call.arguments,
        }
        self.session.pending_permission_execution = PendingPermissionExecution(
            request_id=request.id,
            tool_call=tool_call,
            permission_request=request,
            skipped_tool_calls=list(skipped_tool_calls),
        )
        return confirmation

    def _permission_input_request_from_pending(self, pending: PendingPermissionExecution) -> UserInputRequest:
        if self.session.permission_manager is None:
            raise RuntimeError("permission confirmation requires a permission manager")

        confirmation = self.session.permission_manager.build_confirmation(pending.permission_request)
        confirmation.payload["pending_tool_call"] = {
            "id": pending.tool_call.id,
            "name": pending.tool_call.name,
            "arguments": pending.tool_call.arguments,
        }
        return confirmation

    def _append_skipped_tool_results(self, tool_calls: list[ToolCall]) -> None:
        """为等待用户输入后未执行的并行工具调用补齐结果。

        provider 要求 assistant 一次返回的每个 tool_call 都有对应 tool_result。
        当其中一个工具触发用户输入暂停时，后续工具不能继续执行；这里写入明确的
        skipped 结果，让会话历史保持可投影、可 resume。
        """

        for tool_call in tool_calls:
            result = make_error_result(
                tool_call.name,
                "已暂停等待用户输入，跳过同批次后续工具调用。",
                skipped_due_to_user_input=True,
            )
            self.session.append_tool_result(tool_call=tool_call, result=result)

    def _compact_if_needed(self, *, trigger: ContextWindowTrigger):
        if self.context_manager is None:
            return None
        return self.context_manager.compact_if_needed(
            ContextCompactRequest(
                view=self.session.rebuild_view(),
                runtime_state=self.session.runtime_state,
                trigger=trigger,
                current_turn=self.session.current_turn,
            )
        )

    def _provider_tool_definitions(self):
        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is not None and not capabilities.supports_tools:
            return []
        return self.session.tool_registry.definitions()

    def _drop_unsupported_tool_calls(self, response: ChatResponse) -> ChatResponse:
        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is None or capabilities.supports_tools or not response.tool_calls:
            return response
        self._drop_unsupported_tool_call_stream_events()
        response.diagnostics.warnings.append(
            "provider returned tool_calls even though supports_tools is false; tool calls were ignored"
        )
        return ChatResponse(
            provider=response.provider,
            model=response.model,
            content=response.content or "当前 provider 不支持 tool calling，已忽略模型返回的工具调用。",
            tool_calls=[],
            finish_reason="error",
            usage=response.usage,
            diagnostics=response.diagnostics,
            raw=response.raw,
        )

    def _drop_unsupported_tool_call_stream_events(self) -> None:
        if not self.last_stream_events:
            return
        self.last_stream_events = [
            event
            for event in self.last_stream_events
            if event.kind not in {"tool_call_started", "tool_call_delta", "tool_call_completed"}
        ]

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


class _ToolExecutionState:
    def __init__(
        self,
        *,
        task_hash_changed: bool,
        pending_input: UserInputRequest | None = None,
    ) -> None:
        self.task_hash_changed = task_hash_changed
        self.pending_input = pending_input
