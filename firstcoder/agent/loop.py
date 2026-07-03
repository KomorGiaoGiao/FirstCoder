"""Agent 主循环最小闭环。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from typing import Protocol

import anyio

from firstcoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason
from firstcoder.agent.session import AgentSession, PendingPermissionExecution
from firstcoder.agent.user_input import (
    AgentTurnResult,
    AgentTurnStatus,
    UserInputRequest,
    user_input_request_from_tool_result,
)
from firstcoder.agent.verification import is_successful_verification_result
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.manager import ContextCompactRequest, ContextWindowTrigger
from firstcoder.permissions.types import PermissionDecisionKind, PermissionRequest
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatRequest, ChatResponse, ChatStreamEvent, ToolCall
from firstcoder.tools.permission_results import make_permission_denied_result
from firstcoder.tools.types import Tool, ToolResult, make_error_result


_DEFAULT_MAX_TOOL_ROUNDS = object()


@dataclass(frozen=True, slots=True)
class ToolExecutionEvent:
    """Runtime-visible tool activity event.

    These events are intentionally separate from provider stream events: provider
    streams describe model output, while this describes local tool execution.
    """

    kind: Literal["started", "finished", "permission_requested", "denied", "skipped"]
    tool_call: ToolCall
    result: ToolResult | None = None
    permission_request: PermissionRequest | None = None


class ContextManagerLike(Protocol):
    def compact_if_needed(self, request: ContextCompactRequest):
        ...


class AgentLoop:
    """把用户输入、上下文投影、provider 调用和工具执行串成一轮会话。

    可以把这一层理解成 FirstCoder 的“单轮事务”：

    1. 先把用户输入写入 append-only session log。
    2. 从 session log 重建当前视图，投影成 provider messages。
    3. 调用模型。如果模型返回普通文本，就写入 assistant 消息并结束。
    4. 如果模型返回 tool_calls，就先写入 assistant tool_call，再执行工具。
    5. 工具结果写成 role=tool 消息后，再次调用模型，让模型基于工具结果继续回答。

    这里故意不把具体工具、OpenAI SDK chunk、Textual widget 混进来。AgentLoop 只协调
    “模型想做什么”和“会话事实应该怎样落库”，具体协议转换交给 provider/context 层。
    """

    def __init__(
        self,
        *,
        session: AgentSession,
        provider: ChatProvider,
        tools: list[Tool] | None = None,
        context_builder: ContextBuilder | None = None,
        context_manager: ContextManagerLike | None = None,
        max_tool_rounds: int | None | object = _DEFAULT_MAX_TOOL_ROUNDS,
        limits: AgentLoopLimits | None = None,
        clock=time.monotonic,
        stream_event_handler: Callable[[ChatStreamEvent], None] | None = None,
        tool_event_handler: Callable[[ToolExecutionEvent], None] | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.context_builder = context_builder or ContextBuilder()
        self.context_manager = context_manager
        resolved_limits = limits or AgentLoopLimits.default()
        if max_tool_rounds is not _DEFAULT_MAX_TOOL_ROUNDS:
            resolved_limits = resolved_limits.with_max_tool_rounds(max_tool_rounds)
        self.limits = resolved_limits
        self.max_tool_rounds = resolved_limits.max_tool_rounds
        self.clock = clock
        self.provider_call_count = 0
        self.turn_started_at: float | None = None
        self.last_stream_events: list[ChatStreamEvent] = []
        self.stream_event_handler = stream_event_handler
        self.tool_event_handler = tool_event_handler
        # session 创建时通常已经注册了 session-scoped 工具。这里允许调用方再传入一批
        # 测试或临时工具，但避免重复注册同名工具导致模型 schema 不稳定。
        if tools:
            for tool in tools:
                if tool.name not in self.session.tool_registry.names():
                    self.session.tool_registry.register(tool)

    def run_user_turn(self, content: str) -> ChatResponse:
        """非交互兼容入口。

        旧调用方只认识 `ChatResponse`。如果底层因为权限确认或 ask_user 暂停，这里会把
        “等待用户输入”包装成一条响应文本；真正需要恢复暂停的 UI 应使用
        `run_user_turn_interactive()` 和 `resume_with_user_input()`。
        """

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
            # 上一轮已经把 assistant tool_call 写进历史，但还缺一个匹配的 tool_result。
            # 这种情况下不能追加新的用户消息，否则 provider 会看到非法消息序列。
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self._permission_input_request_from_pending(pending),
            )

        self._begin_turn()
        self.session.append_user_message(content)
        # 用户消息写入后先给 context manager 一个机会。通常不会压缩；但当上下文已经接近
        # 阈值时，先整理历史可以避免下一次 provider 请求直接超窗。
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

        return self._run_tool_loop_interactive(self._complete_once_with_recovery)

    def resume_with_user_input(self, request_id: str, answer: str) -> AgentTurnResult:
        """用用户回答恢复一个暂停中的权限确认。

        普通 `ask_user` 第一版仍通过“下一条用户消息”继续；权限确认不能这样做，
        因为模型原始 tool_call 已经在历史里等待一个匹配的 tool_result。这里必须先
        用本地 pending 状态补齐最终 tool_result，再继续下一次 provider 调用。
        """

        result = self._append_permission_resume_result(request_id, answer)
        if result is not None:
            return result
        self._begin_turn()
        return self._run_tool_loop_interactive(self._complete_once_with_recovery)

    async def resume_with_user_input_streaming(self, request_id: str, answer: str) -> AgentTurnResult:
        """流式模式下恢复权限确认，并继续消费 provider stream。"""

        result = await self._append_permission_resume_result_async(request_id, answer)
        if result is not None:
            return result
        self._begin_turn()
        return await self._run_tool_loop_interactive_async(self._stream_once_with_recovery)

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

        self._begin_turn()
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

    def _append_permission_resume_result(self, request_id: str, answer: str) -> AgentTurnResult | None:
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
            # 拒绝也必须写成 tool_result。provider 协议要求每个 assistant tool_call 都有
            # 对应的 role=tool 消息，否则下一次请求会因为消息序列不合法而失败。
            result = make_permission_denied_result(
                tool_name=pending.tool_call.name,
                request=pending.permission_request,
                decision=decision,
            )
            self._emit_tool_event(
                "denied",
                pending.tool_call,
                result=result,
                permission_request=pending.permission_request,
            )
        else:
            # 用户同意后执行原始 pending tool_call。这里绕过再次权限检查，避免同一个
            # 确认请求被重复拦截；授权是否长期有效由 PermissionManager/GrantStore 决定。
            self._emit_tool_event(
                "started",
                pending.tool_call,
                permission_request=pending.permission_request,
            )
            result = self.session.execute_tool_call_after_permission_confirmation(pending.tool_call)
            self._emit_tool_event(
                "finished",
                pending.tool_call,
                result=result,
                permission_request=pending.permission_request,
            )

        self.session.pending_permission_execution = None
        self.session.append_tool_result(tool_call=pending.tool_call, result=result)
        self._append_skipped_tool_results(pending.skipped_tool_calls)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return None

    async def _append_permission_resume_result_async(self, request_id: str, answer: str) -> AgentTurnResult | None:
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
            self._emit_tool_event(
                "denied",
                pending.tool_call,
                result=result,
                permission_request=pending.permission_request,
            )
        else:
            self._emit_tool_event(
                "started",
                pending.tool_call,
                permission_request=pending.permission_request,
            )
            result = await anyio.to_thread.run_sync(
                self.session.execute_tool_call_after_permission_confirmation,
                pending.tool_call,
            )
            self._emit_tool_event(
                "finished",
                pending.tool_call,
                result=result,
                permission_request=pending.permission_request,
            )

        self.session.pending_permission_execution = None
        self.session.append_tool_result(tool_call=pending.tool_call, result=result)
        self._append_skipped_tool_results(pending.skipped_tool_calls)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return None

    def _complete_once(self, *, tool_choice="auto") -> ChatResponse:
        """构造一次 provider 请求并获得模型响应。

        这一步只负责“问模型一次”，不处理工具循环。拆开后，同步调用、streaming 调用、
        prompt-too-long 恢复都可以复用同一套上下文构造逻辑。
        """

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
        self._check_provider_call_limit()
        self._check_turn_timeout()
        self.provider_call_count += 1
        return self.provider.complete(ChatRequest(messages=messages, tools=definitions, tool_choice=tool_choice))

    def _complete_once_with_recovery(self, *, tool_choice="auto") -> ChatResponse:
        """同步模式下一次 provider 调用，并处理 prompt-too-long 的单次恢复。

        provider 如果拒绝请求，说明 assistant 回复还没有产生，也就没有新消息要落库。
        这时可以先触发 blocking compact，再重建 provider messages 重试一次。
        """

        try:
            return self._complete_once(tool_choice=tool_choice)
        except ProviderError as exc:
            if not exc.requires_compaction:
                raise
            result = self._compact_if_needed(trigger=ContextWindowTrigger.PROMPT_TOO_LONG)
            if result is None or result.status != "success":
                raise
            return self._complete_once(tool_choice=tool_choice)

    async def _stream_once(self, *, tool_choice="auto") -> ChatResponse:
        """消费一次 provider stream，最终仍返回完整 ChatResponse。

        UI 可以读取 `last_stream_events` 展示 text_delta；但工具调用必须等 stream 完成后
        才能执行，因为 OpenAI-compatible 的 tool arguments 可能分散在多个 chunk 中。
        """

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
        self._check_provider_call_limit()
        self._check_turn_timeout()
        self.provider_call_count += 1
        async for event in self.provider.astream(ChatRequest(messages=messages, tools=definitions, tool_choice=tool_choice)):
            self.last_stream_events.append(event)
            if self.stream_event_handler is not None:
                self.stream_event_handler(event)
            if event.kind == "message_completed":
                final_response = event.response
        if final_response is None:
            raise ProviderError(
                ProviderErrorKind.API_ERROR,
                "provider stream ended without message_completed event",
            )
        return final_response

    async def _stream_once_with_recovery(self, *, tool_choice="auto") -> ChatResponse:
        try:
            return await self._stream_once_attempt(tool_choice=tool_choice)
        except ProviderError as exc:
            if not exc.requires_compaction:
                raise
            result = self._compact_if_needed(trigger=ContextWindowTrigger.PROMPT_TOO_LONG)
            if result is None or result.status != "success":
                raise
            return await self._stream_once_attempt(tool_choice=tool_choice)

    async def _stream_once_attempt(self, *, tool_choice="auto") -> ChatResponse:
        start_event_count = len(self.last_stream_events)
        try:
            return await self._stream_once(tool_choice=tool_choice)
        except ProviderError:
            # streaming 尝试失败时，不能把已经收到的局部 delta 当成真实回答留给 UI。
            # 真正成功的重试会重新产生完整事件。
            del self.last_stream_events[start_event_count:]
            raise

    def _run_tool_loop_interactive(self, complete_once) -> AgentTurnResult:
        """核心工具循环：问模型，执行工具，再把工具结果回喂给模型。

        退出条件只有三类：
        - 模型返回的 response 没有 tool_calls：说明它已经给出最终回答。
        - 命中 max_tool_rounds：防止模型无限调用工具。
        - 某个工具需要用户输入或权限确认：暂停并把 pending_input 交给 UI。
        """

        try:
            response = self._drop_unsupported_tool_calls(complete_once())
            tool_rounds = 0
            while response.tool_calls:
                if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                    response = self._tool_round_limit_response(response)
                    break

                # 关键顺序：必须先写 assistant tool_call，再写对应 tool_result。provider 后续
                # 才能看到合法的 “assistant(tool_calls) -> tool(result)” 消息序列。
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

                if self.limits.successful_verification_stop and execution.successful_verification:
                    response = self._drop_unsupported_tool_calls(complete_once(tool_choice="none"))
                    break

                tool_rounds += 1
                if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                    response = self._tool_round_limit_response(response)
                    break
                # 工具结果已经写进 session log；下一次 complete_once() 会通过 ContextBuilder
                # 重新投影完整历史，让模型读取刚才的工具输出。
                response = self._drop_unsupported_tool_calls(complete_once())
        except _AgentLoopLimitReached as exc:
            response = self._limit_response(exc.reason)

        # 没有工具调用时，这条 response 就是最终 assistant 回复。命中轮次上限时也会写入
        # 一条纯文本说明，避免保存未执行的 tool_call。
        self.session.append_assistant_response(response)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)

    async def _run_tool_loop_interactive_async(self, complete_once) -> AgentTurnResult:
        """streaming 版本的工具循环，语义与同步版本一致。"""

        try:
            response = self._drop_unsupported_tool_calls(await complete_once())
            tool_rounds = 0
            while response.tool_calls:
                if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                    response = self._tool_round_limit_response(response)
                    break

                self.session.append_assistant_response(response)
                execution = await self._execute_tool_calls_interactive_async(response.tool_calls)
                if execution.pending_input is not None:
                    return AgentTurnResult(
                        status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                        pending_input=execution.pending_input,
                    )
                if execution.task_hash_changed:
                    self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)
                self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

                if self.limits.successful_verification_stop and execution.successful_verification:
                    response = self._drop_unsupported_tool_calls(await complete_once(tool_choice="none"))
                    break

                tool_rounds += 1
                if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                    response = self._tool_round_limit_response(response)
                    break
                response = self._drop_unsupported_tool_calls(await complete_once())
        except _AgentLoopLimitReached as exc:
            response = self._limit_response(exc.reason)

        self.session.append_assistant_response(response)
        self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)
        return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)

    def _execute_tool_calls(self, tool_calls: list[ToolCall]) -> bool:
        return self._execute_tool_calls_interactive(tool_calls).task_hash_changed

    def _execute_tool_calls_interactive(self, tool_calls: list[ToolCall]) -> "_ToolExecutionState":
        task_hash_changed = False
        successful_verification = False
        for index, tool_call in enumerate(tool_calls):
            # 权限检查放在工具执行前，但具体“这个路径能不能写 / 这个命令能不能跑”
            # 的判断由 permissions 和 permission-aware tool wrapper 完成。AgentLoop 只关心
            # allow / deny / ask 三种结果该如何写回会话。
            preflight = self.session.preflight_tool_call_permission(tool_call)
            if preflight is not None:
                if preflight.decision.kind == PermissionDecisionKind.DENY:
                    result = make_permission_denied_result(
                        tool_name=tool_call.name,
                        request=preflight.request,
                        decision=preflight.decision,
                    )
                    self._emit_tool_event(
                        "denied",
                        tool_call,
                        result=result,
                        permission_request=preflight.request,
                    )
                    self.session.append_tool_result(tool_call=tool_call, result=result)
                    continue
                if preflight.decision.kind == PermissionDecisionKind.ASK:
                    # 需要用户确认时不能继续执行同批次后续工具。否则用户还没批准第一个
                    # 高风险操作，后面的工具却已经产生副作用了。
                    pending_input = self._store_pending_permission_request(
                        tool_call=tool_call,
                        request=preflight.request,
                        skipped_tool_calls=tool_calls[index + 1 :],
                    )
                    self._emit_tool_event(
                        "permission_requested",
                        tool_call,
                        permission_request=preflight.request,
                    )
                    return _ToolExecutionState(
                        task_hash_changed=task_hash_changed,
                        pending_input=pending_input,
                    )

            self._emit_tool_event("started", tool_call)
            result = self.session.execute_tool_call(tool_call)
            self._emit_tool_event("finished", tool_call, result=result)
            self.session.append_tool_result(tool_call=tool_call, result=result)
            if is_successful_verification_result(tool_call.name, result):
                successful_verification = True
            # ask_user 这类工具本身不会继续执行副作用，而是把“需要问用户什么”包装在
            # ToolResult.data 中。这里把它转换成 AgentTurnResult 的 pending_input。
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
                # task_boundary 是一种“语义触发”：即使上下文还没超 token 阈值，确认任务切换
                # 后也应该整理旧任务上下文，降低旧任务信息污染新任务的概率。
                task_hash_changed = True
        return _ToolExecutionState(
            task_hash_changed=task_hash_changed,
            successful_verification=successful_verification,
        )

    async def _execute_tool_calls_interactive_async(self, tool_calls: list[ToolCall]) -> "_ToolExecutionState":
        task_hash_changed = False
        successful_verification = False
        for index, tool_call in enumerate(tool_calls):
            preflight = self.session.preflight_tool_call_permission(tool_call)
            if preflight is not None:
                if preflight.decision.kind == PermissionDecisionKind.DENY:
                    result = make_permission_denied_result(
                        tool_name=tool_call.name,
                        request=preflight.request,
                        decision=preflight.decision,
                    )
                    self._emit_tool_event(
                        "denied",
                        tool_call,
                        result=result,
                        permission_request=preflight.request,
                    )
                    self.session.append_tool_result(tool_call=tool_call, result=result)
                    continue
                if preflight.decision.kind == PermissionDecisionKind.ASK:
                    pending_input = self._store_pending_permission_request(
                        tool_call=tool_call,
                        request=preflight.request,
                        skipped_tool_calls=tool_calls[index + 1 :],
                    )
                    self._emit_tool_event(
                        "permission_requested",
                        tool_call,
                        permission_request=preflight.request,
                    )
                    return _ToolExecutionState(
                        task_hash_changed=task_hash_changed,
                        pending_input=pending_input,
                    )

            self._emit_tool_event("started", tool_call)
            result = await anyio.to_thread.run_sync(self.session.execute_tool_call, tool_call)
            self._emit_tool_event("finished", tool_call, result=result)
            self.session.append_tool_result(tool_call=tool_call, result=result)
            if is_successful_verification_result(tool_call.name, result):
                successful_verification = True
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
        return _ToolExecutionState(
            task_hash_changed=task_hash_changed,
            successful_verification=successful_verification,
        )

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
        # UI 会看到 confirmation.payload，但恢复时不信任 UI 回传的 tool_call。真实 tool_call
        # 保存在 session.pending_permission_execution 中，避免前端篡改参数后执行。
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
            self._emit_tool_event("skipped", tool_call, result=result)
            self.session.append_tool_result(tool_call=tool_call, result=result)

    def _emit_tool_event(
        self,
        kind: Literal["started", "finished", "permission_requested", "denied", "skipped"],
        tool_call: ToolCall,
        *,
        result: ToolResult | None = None,
        permission_request: PermissionRequest | None = None,
    ) -> None:
        if self.tool_event_handler is None:
            return
        self.tool_event_handler(
            ToolExecutionEvent(
                kind=kind,
                tool_call=tool_call,
                result=result,
                permission_request=permission_request,
            )
        )

    def _compact_if_needed(self, *, trigger: ContextWindowTrigger):
        """把压缩触发交给 context manager。

        AgentLoop 不判断 token 细节，也不决定 L1/L2/L3/L4 怎么做；它只在关键时机告诉
        context 层：“现在可能需要整理上下文了”。
        """

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
        """根据 provider 能力决定是否向模型暴露工具 schema。"""

        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is not None and not capabilities.supports_tools:
            return []
        return self.session.tool_registry.definitions()

    def _begin_turn(self) -> None:
        self.provider_call_count = 0
        self.turn_started_at = self.clock()

    def _check_provider_call_limit(self) -> None:
        limit = self.limits.max_provider_calls
        if limit is not None and self.provider_call_count >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.PROVIDER_CALL_LIMIT)

    def _check_turn_timeout(self) -> None:
        limit = self.limits.max_turn_seconds
        if limit is None or self.turn_started_at is None:
            return
        if self.clock() - self.turn_started_at >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.TURN_TIMEOUT)

    def _drop_unsupported_tool_calls(self, response: ChatResponse) -> ChatResponse:
        """兜底保护：不支持工具的 provider 理论上不该返回 tool_calls。

        如果兼容站行为异常仍返回了 tool_calls，这里把它们丢弃并记录 diagnostics，避免
        agent 执行一个 provider 能力声明之外的工具链。
        """

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

        return self._limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT, raw=response.raw)

    def _limit_response(self, reason: AgentLoopStopReason, *, raw: dict | None = None) -> ChatResponse:
        messages = {
            AgentLoopStopReason.PROVIDER_CALL_LIMIT: (
                f"provider 调用次数达到上限（max_provider_calls={self.limits.max_provider_calls}），已停止继续执行。"
            ),
            AgentLoopStopReason.TURN_TIMEOUT: (
                f"本轮任务耗时达到上限（max_turn_seconds={self.limits.max_turn_seconds}），已停止继续执行。"
            ),
            AgentLoopStopReason.TOOL_ROUND_LIMIT: (
                f"工具调用轮次达到上限（max_tool_rounds={self.limits.max_tool_rounds}），已停止继续执行工具。"
            ),
        }
        return ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content=messages[reason],
            tool_calls=[],
            finish_reason=reason.value,
            raw=raw,
        )


class _ToolExecutionState:
    def __init__(
        self,
        *,
        task_hash_changed: bool,
        pending_input: UserInputRequest | None = None,
        successful_verification: bool = False,
    ) -> None:
        self.task_hash_changed = task_hash_changed
        self.pending_input = pending_input
        self.successful_verification = successful_verification


class _AgentLoopLimitReached(Exception):
    def __init__(self, reason: AgentLoopStopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason
