"""Agent 主循环最小闭环。"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Callable
from typing import Literal

import anyio

from firstcoder.runtime.cancellation import AgentCancelledError, CancellationToken
from firstcoder.agent.ports import ContextManagerLike
from firstcoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason
from firstcoder.agent.session import AgentSession, PendingPermissionExecution
from firstcoder.agent.task_boundary_classifier import (
    CLASSIFICATION_PROMPT as _TASK_BOUNDARY_CLASSIFICATION_PROMPT,
    TaskBoundaryClassifier,
)
from firstcoder.agent.todo_policy import TodoPolicy
from firstcoder.agent.tool_execution import ToolExecutionEvent, ToolExecutor
from firstcoder.agent.tool_settlement import ToolCallSettlement
from firstcoder.agent.user_input import (
    AgentTurnResult,
    AgentTurnStatus,
    UserInputRequest,
)
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.manager import ContextCompactRequest, ContextWindowTrigger
from firstcoder.context.system_prompt import PromptPrefixCache
from firstcoder.context.token_budget import estimate_chat_request_tokens
from firstcoder.context.task_boundary import TaskBoundaryService
from firstcoder.input.attachments import UserAttachment
from firstcoder.permissions.types import PermissionDecision, PermissionDecisionKind, PermissionRequest
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatMessage, ChatRequest, ChatResponse, ChatStreamEvent, MainRequestOptions, ToolCall
from firstcoder.skills.loader import SkillLoadError, SkillLoader
from firstcoder.skills.router import SkillRouter
from firstcoder.skills.session import append_skill_loaded, append_skill_required_file_loaded, append_skill_selected
from firstcoder.tools.permission_results import (
    make_permission_denied_result,
    make_prewrite_review_failed_result,
    make_prewrite_review_stale_result,
)
from firstcoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES
from firstcoder.tools.types import Tool, ToolResult


_DEFAULT_MAX_TOOL_ROUNDS = object()


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
        guidance_provider: Callable[[], list[str]] | None = None,
        cancellation_token: CancellationToken | None = None,
        request_options: MainRequestOptions | None = None,
    ) -> None:
        self.session = session
        self.tool_settlement = ToolCallSettlement(session)
        self.todo_policy = TodoPolicy(session)
        self.provider = provider
        self.request_options = request_options or MainRequestOptions()
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
        self.guidance_provider = guidance_provider
        self.cancellation_token = cancellation_token
        self._skills_prepared_for_turn: int | None = None
        self._todo_reconciliation_attempted = False
        self._tool_rounds_completed = 0
        self.task_boundary_classifier = TaskBoundaryClassifier(
            session=session,
            provider=provider,
            context_builder=self.context_builder,
            compact_if_needed=self._compact_if_needed,
            check_cancelled=self._check_cancelled,
            reserve_provider_call=self._reserve_provider_call,
            check_turn_timeout=self._check_turn_timeout,
            tag_task_boundary_messages=self._tag_task_boundary_messages_with_active_hash,
        )
        self.tool_executor = ToolExecutor(
            session=session,
            settlement=self.tool_settlement,
            emit_event=self._emit_tool_event,
            check_cancelled=self._check_cancelled,
            cancellation_token=self.cancellation_token,
            tag_task_boundary_messages=self._tag_task_boundary_messages_with_active_hash,
            emit_settlements=self._emit_settlements,
        )
        # session 创建时通常已经注册了 session-scoped 工具。这里允许调用方再传入一批
        # 测试或临时工具，但避免重复注册同名工具导致模型 schema 不稳定。
        if tools:
            for tool in tools:
                if tool.name not in self.session.tool_registry.names():
                    self.session.tool_registry.register(tool)

    def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        """非交互兼容入口。

        旧调用方只认识 `ChatResponse`。如果底层因为权限确认或 ask_user 暂停，这里会把
        “等待用户输入”包装成一条响应文本；真正需要恢复暂停的 UI 应使用
        `run_user_turn_interactive()` 和 `resume_with_user_input()`。
        """

        result = self.run_user_turn_interactive(content, attachments=attachments)
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

    def replace_cancellation_token(self, token: CancellationToken | None) -> None:
        """Rebind cooperative cancellation when a paused turn resumes in the runner."""

        self.cancellation_token = token
        self.tool_executor.cancellation_token = token

    def clear_stream_events(self) -> None:
        self.last_stream_events = []

    def run_user_turn_interactive(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:
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
                pending_input=self.tool_executor.permission_input_request_from_pending(pending),
            )

        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        message_id = self.session.append_user_message(content, attachments=attachments)
        try:
            if self._initialize_active_task_if_missing(message_id) is None:
                self._classify_task_boundary(message_id)
            # 用户消息写入后先给 context manager 一个机会。通常不会压缩；但当上下文已经接近
            # 阈值时，先整理历史可以避免下一次 provider 请求直接超窗。
            self._auto_compact()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self._limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self._interrupted_response())

        return self._run_tool_loop_interactive(
            self._complete_once_with_recovery,
        )

    def resume_with_user_input(self, request_id: str, answer: str) -> AgentTurnResult:
        """用用户回答恢复一个暂停中的权限确认。

        普通 `ask_user` 第一版仍通过“下一条用户消息”继续；权限确认不能这样做，
        因为模型原始 tool_call 已经在历史里等待一个匹配的 tool_result。这里必须先
        用本地 pending 状态补齐最终 tool_result，再继续下一次 provider 调用。
        """

        try:
            self._check_turn_timeout()
            self._check_cancelled()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self._limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self._interrupted_response())
        result = self._append_permission_resume_result(request_id, answer)
        if result is not None:
            return result
        self._begin_turn(new_user_turn=False)
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        return self._run_tool_loop_interactive(self._complete_once_with_recovery)

    async def resume_with_user_input_streaming(self, request_id: str, answer: str) -> AgentTurnResult:
        """流式模式下恢复权限确认，并继续消费 provider stream。"""

        try:
            self._check_turn_timeout()
            self._check_cancelled()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self._limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self._interrupted_response())
        result = await self._append_permission_resume_result_async(request_id, answer)
        if result is not None:
            return result
        self._begin_turn(new_user_turn=False)
        self._check_cancelled()
        return await self._run_tool_loop_interactive_async(self._stream_once_with_recovery)

    async def run_user_turn_streaming(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        """使用 provider 内部 stream event 协议执行一轮会话。

        文本 delta 可以被上层即时展示，但工具调用仍保持原子语义：只有 stream 完成并
        返回完整 `ChatResponse.tool_calls` 后，才写入 assistant message 并执行工具。
        """

        self.last_stream_events = []
        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            pending_input = self.tool_executor.permission_input_request_from_pending(pending)
            return ChatResponse(
                provider=self.provider.name,
                model=self.provider.model,
                content=pending_input.question,
                finish_reason=AgentTurnStatus.WAITING_FOR_USER_INPUT.value,
                raw={"pending_input": pending_input},
            )

        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        message_id = self.session.append_user_message(content, attachments=attachments)
        try:
            if self._initialize_active_task_if_missing(message_id) is None:
                await self._classify_task_boundary_async(message_id)
            self._auto_compact()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self._limit_response(exc.reason)).response
        except AgentCancelledError:
            return self._complete_turn(self._interrupted_response()).response

        result = await self._run_tool_loop_interactive_async(
            self._stream_once_with_recovery,
        )
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

    def run_user_turn_streaming_sync(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> ChatResponse:
        """同步入口，仅用于测试或没有运行中 event loop 的 CLI 场景。

        Textual 这类已经运行 asyncio event loop 的 UI 后续应该直接 await
        `run_user_turn_streaming()` 或放到 worker 中执行，不能调用这个包装方法。
        """

        return asyncio.run(self.run_user_turn_streaming(content, attachments=attachments))

    def _initialize_active_task_if_missing(self, basis_message_id: str):
        service = TaskBoundaryService(known_message_ids=self.session.known_message_ids)
        observation = service.initialize_active_task(self.session.runtime_state, basis_message_id=basis_message_id)
        if observation is not None:
            self.session.writer.append_task_boundary_observation(observation)
            self._tag_message_parts_with_task_hash(basis_message_id, observation.active_task_hash)
        return observation

    def _classify_task_boundary(self, basis_message_id: str) -> None:
        self.task_boundary_classifier.classify(basis_message_id)

    async def _classify_task_boundary_async(self, basis_message_id: str) -> None:
        await self.task_boundary_classifier.classify_async(basis_message_id)

    def _tag_message_parts_with_task_hash(self, message_id: str, task_hash: str | None) -> None:
        if not task_hash:
            return
        view = self.session.rebuild_view()
        message = next((message for message in view.messages if message.id == message_id), None)
        if message is None:
            return
        for part in message.parts:
            self.session.writer.append_message_part_metadata_updated(
                message_id=message_id,
                part_id=part.id,
                metadata={"task_hash": task_hash},
            )

    def _tag_task_boundary_messages_with_active_hash(self, data: dict[str, object]) -> None:
        active_hash = data.get("active_task_hash")
        message_ids = {
            str(data.get("basis_message_id") or ""),
            str(data.get("candidate_basis_message_id") or ""),
        }
        for message_id in message_ids:
            if message_id:
                self._tag_message_parts_with_task_hash(message_id, active_hash)

    def _append_permission_resume_result(self, request_id: str, answer: str) -> AgentTurnResult | None:
        pending = self._pending_permission_for_resume(request_id)
        if isinstance(pending, AgentTurnResult):
            return pending
        result = self._prepare_permission_resume(pending, answer)
        if result is None:
            result = self._execute_resumed_permission_tool_call(pending)
            self._emit_finished_permission_resume(pending, result)
        self._finish_permission_resume(pending, result)
        return None

    async def _append_permission_resume_result_async(self, request_id: str, answer: str) -> AgentTurnResult | None:
        pending = self._pending_permission_for_resume(request_id)
        if isinstance(pending, AgentTurnResult):
            return pending
        result = self._prepare_permission_resume(pending, answer)
        if result is None:
            result = await anyio.to_thread.run_sync(self._execute_resumed_permission_tool_call, pending)
            self._emit_finished_permission_resume(pending, result)
        self._finish_permission_resume(pending, result)
        return None

    def _pending_permission_for_resume(
        self,
        request_id: str,
    ) -> PendingPermissionExecution | AgentTurnResult:
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
        return pending

    def _prepare_permission_resume(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ) -> ToolResult | None:
        result = self._blocked_permission_resume_result(pending, answer)
        if result is not None:
            self._emit_tool_event(
                "denied",
                pending.tool_call,
                result=result,
                permission_request=pending.permission_request,
            )
            return result
        self._emit_tool_event(
            "started",
            pending.tool_call,
            permission_request=pending.permission_request,
        )
        self._check_cancelled()
        return None

    def _execute_resumed_permission_tool_call(self, pending: PendingPermissionExecution) -> ToolResult:
        # 用户同意后使用 session 保存的原始 tool_call，不能相信 UI 回传的参数。
        return self.tool_executor.execute_after_permission_with_cancellation_context(pending.tool_call)

    def _emit_finished_permission_resume(
        self,
        pending: PendingPermissionExecution,
        result: ToolResult,
    ) -> None:
        self._emit_tool_event(
            "finished",
            pending.tool_call,
            result=result,
            permission_request=pending.permission_request,
        )

    def _finish_permission_resume(self, pending: PendingPermissionExecution, result: ToolResult) -> None:
        self.session.pending_permission_execution = None
        self.session.append_tool_result(tool_call=pending.tool_call, result=result)
        self._emit_settlements("skipped", self.tool_settlement.append_skipped(pending.skipped_tool_calls))
        self._tool_rounds_completed += 1
        self._auto_compact()

    def _resolve_pending_confirmation(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ):
        if not pending.review_only:
            return self.session.permission_manager.resolve_confirmation(pending.permission_request, answer)
        normalized = answer.strip().lower()
        if normalized in {"allow_once", "allow", "once", "2"}:
            current = self.session.preflight_tool_call_permission(pending.tool_call)
            if current is not None and current.decision.kind == PermissionDecisionKind.DENY:
                return current.decision
            return PermissionDecision(kind=PermissionDecisionKind.ALLOW, reason="用户批准应用已预览的修改。")
        if normalized in {"deny", "no", "1"} or normalized.startswith(("reject:", "reject_with_feedback:")):
            return self.session.permission_manager.resolve_confirmation(pending.permission_request, answer)
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"未知写前预览选择：{answer}",
        )

    def _blocked_permission_resume_result(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ) -> ToolResult | None:
        decision = self._resolve_pending_confirmation(pending, answer)
        if decision.kind == PermissionDecisionKind.DENY:
            return make_permission_denied_result(
                tool_name=pending.tool_call.name,
                request=pending.permission_request,
                decision=decision,
            )
        if pending.prewrite_review is None:
            return None
        if not pending.prewrite_review.ok:
            return make_prewrite_review_failed_result(
                tool_name=pending.tool_call.name,
                request=pending.permission_request,
                error=pending.prewrite_review.error or "未知错误",
            )
        if pending.prewrite_review.is_current(
            self.session.permission_manager.policy.project_root,
            access=self.session.sandbox_access,
        ):
            return None
        return make_prewrite_review_stale_result(
            tool_name=pending.tool_call.name,
            request=pending.permission_request,
        )

    def _complete_once(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
    ) -> ChatResponse:
        """构造一次 provider 请求并获得模型响应。

        这一步只负责“问模型一次”，不处理工具循环。拆开后，同步调用、streaming 调用、
        prompt-too-long 恢复都可以复用同一套上下文构造逻辑。
        """

        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        self._append_pending_guidance()
        self._prepare_skills_for_current_turn()
        definitions = self._provider_tool_definitions()
        messages = self._request_messages(runtime_instruction=runtime_instruction)
        self._reserve_provider_call()
        self._check_turn_timeout()
        self._check_cancelled()
        return self.provider.complete(self._main_chat_request(messages, definitions, tool_choice))

    def _main_chat_request(self, messages, definitions, tool_choice) -> ChatRequest:
        return ChatRequest(
            messages=messages,
            tools=definitions,
            tool_choice=tool_choice,
            **self.request_options.as_chat_request_kwargs(),
        )

    def _complete_once_with_recovery(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
    ) -> ChatResponse:
        """同步模式下一次 provider 调用，并处理 prompt-too-long 的单次恢复。

        provider 如果拒绝请求，说明 assistant 回复还没有产生，也就没有新消息要落库。
        这时可以先触发 blocking compact，再重建 provider messages 重试一次。
        """

        try:
            return self._complete_once(
                tool_choice=tool_choice,
                runtime_instruction=runtime_instruction,
            )
        except ProviderError as exc:
            if not exc.requires_compaction:
                raise
            result = self._compact_for_prompt_too_long()
            if result is None or result.status != "success":
                raise
            return self._complete_once(
                tool_choice=tool_choice,
                runtime_instruction=runtime_instruction,
            )

    def _prepare_skills_for_current_turn(self) -> None:
        current_turn = self.session.current_turn
        if self._skills_prepared_for_turn == current_turn:
            return
        self._skills_prepared_for_turn = current_turn
        if not self.session.skill_catalog.skills:
            return
        user_message = self._current_user_message_content()
        if not user_message:
            return
        decision = SkillRouter().route(
            user_message,
            agents_md=self.session.agents_md,
            catalog=self.session.skill_catalog,
        )
        if decision.selected is None or decision.confidence != "high":
            return
        append_skill_selected(self.session.writer, decision)
        loader = SkillLoader()
        try:
            loaded = loader.load(decision.selected)
        except SkillLoadError:
            return
        required_files = []
        for file_path in loaded.required_files:
            try:
                required = loader.load_required_file(loaded, file_path)
            except SkillLoadError:
                continue
            required_files.append(required)
        if required_files:
            loaded = type(loaded)(
                skill=loaded.skill,
                content=loaded.content,
                required_files=loaded.required_files,
                required_file_contents=required_files,
            )
        self.session.loaded_skills.append(loaded)
        append_skill_loaded(self.session.writer, loaded)
        for required in required_files:
            append_skill_required_file_loaded(self.session.writer, required)
        self.session.prompt_cache = PromptPrefixCache()

    def _current_user_message_content(self) -> str:
        for message in reversed(self.session.rebuild_view().messages):
            if message.role != "user":
                continue
            return "\n".join(part.content for part in message.parts if part.kind == "text")
        return ""

    async def _stream_once(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
    ) -> ChatResponse:
        """消费一次 provider stream，最终仍返回完整 ChatResponse。

        UI 可以读取 `last_stream_events` 展示 text_delta；但工具调用必须等 stream 完成后
        才能执行，因为 OpenAI-compatible 的 tool arguments 可能分散在多个 chunk 中。
        """

        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        self._append_pending_guidance()
        self._prepare_skills_for_current_turn()
        definitions = self._provider_tool_definitions()
        messages = self._request_messages(runtime_instruction=runtime_instruction)
        final_response: ChatResponse | None = None
        self._reserve_provider_call()
        self._check_turn_timeout()
        self._check_cancelled()
        async for event in self.provider.astream(self._main_chat_request(messages, definitions, tool_choice)):
            self._check_cancelled()
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

    async def _stream_once_with_recovery(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
    ) -> ChatResponse:
        retryable_failures = 0
        while True:
            try:
                return await self._stream_once_attempt(
                    tool_choice=tool_choice,
                    runtime_instruction=runtime_instruction,
                )
            except ProviderError as exc:
                if exc.retryable:
                    if retryable_failures == 0:
                        retryable_failures += 1
                        continue
                    return self._complete_once(
                        tool_choice=tool_choice,
                        runtime_instruction=runtime_instruction,
                    )
                if not exc.requires_compaction:
                    raise
                result = self._compact_for_prompt_too_long()
                if result is None or result.status != "success":
                    raise
                return await self._stream_once_attempt(
                    tool_choice=tool_choice,
                    runtime_instruction=runtime_instruction,
                )

    async def _stream_once_attempt(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
    ) -> ChatResponse:
        start_event_count = len(self.last_stream_events)
        try:
            return await self._stream_once(
                tool_choice=tool_choice,
                runtime_instruction=runtime_instruction,
            )
        except ProviderError:
            # streaming 尝试失败时，不能把已经收到的局部 delta 当成真实回答留给 UI。
            # 真正成功的重试会重新产生完整事件。
            del self.last_stream_events[start_event_count:]
            raise

    def _run_tool_loop_interactive(self, complete_once, *, initial_tool_choice="auto") -> AgentTurnResult:
        """核心工具循环：问模型，执行工具，再把工具结果回喂给模型。

        退出条件只有三类：
        - 模型返回的 response 没有 tool_calls：说明它已经给出最终回答。
        - 命中 max_tool_rounds：防止模型无限调用工具。
        - 某个工具需要用户输入或权限确认：暂停并把 pending_input 交给 UI。
        """

        guardrail_stop = False
        try:
            if self.max_tool_rounds is not None and self._tool_rounds_completed >= self.max_tool_rounds:
                return self._complete_turn(self._limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT))
            response = self._drop_unsupported_tool_calls(complete_once(tool_choice=initial_tool_choice))
            tool_rounds = self._tool_rounds_completed
            response, pending_input, tool_rounds = self._continue_tool_loop_from_response(
                response,
                complete_once,
                tool_rounds,
            )
            if pending_input is not None:
                return self._pending_turn_result(pending_input)
            if response.finish_reason != AgentLoopStopReason.TOOL_ROUND_LIMIT.value:
                response, pending_input, _ = self._run_todo_self_check_if_needed(
                    response,
                    complete_once,
                    tool_rounds,
                )
                if pending_input is not None:
                    return self._pending_turn_result(pending_input)
        except _AgentLoopLimitReached as exc:
            response = self._limit_response(exc.reason)
            guardrail_stop = True
        except AgentCancelledError:
            self._append_interrupted_tool_results()
            response = self._interrupted_response()

        if self._is_cancelled():
            self._append_interrupted_tool_results()
            response = self._interrupted_response()
            return self._complete_turn(response)
        if guardrail_stop:
            return self._complete_turn(response)

        # 没有工具调用时，这条 response 就是最终 assistant 回复。命中轮次上限时也会写入
        # 一条纯文本说明，避免保存未执行的 tool_call。
        return self._complete_turn(response)

    async def _run_tool_loop_interactive_async(self, complete_once, *, initial_tool_choice="auto") -> AgentTurnResult:
        """streaming 版本的工具循环，语义与同步版本一致。"""

        guardrail_stop = False
        try:
            if self.max_tool_rounds is not None and self._tool_rounds_completed >= self.max_tool_rounds:
                return self._complete_turn(self._limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT))
            response = self._drop_unsupported_tool_calls(await complete_once(tool_choice=initial_tool_choice))
            tool_rounds = self._tool_rounds_completed
            response, pending_input, tool_rounds = await self._continue_tool_loop_from_response_async(
                response,
                complete_once,
                tool_rounds,
            )
            if pending_input is not None:
                return self._pending_turn_result(pending_input)
            if response.finish_reason != AgentLoopStopReason.TOOL_ROUND_LIMIT.value:
                response, pending_input, _ = await self._run_todo_self_check_if_needed_async(
                    response,
                    complete_once,
                    tool_rounds,
                )
                if pending_input is not None:
                    return self._pending_turn_result(pending_input)
        except _AgentLoopLimitReached as exc:
            response = self._limit_response(exc.reason)
            guardrail_stop = True
        except AgentCancelledError:
            self._append_interrupted_tool_results()
            response = self._interrupted_response()

        if self._is_cancelled():
            self._append_interrupted_tool_results()
            response = self._interrupted_response()
            return self._complete_turn(response)
        if guardrail_stop:
            return self._complete_turn(response)

        return self._complete_turn(response)

    @staticmethod
    def _pending_turn_result(pending_input: UserInputRequest) -> AgentTurnResult:
        return AgentTurnResult(status=AgentTurnStatus.WAITING_FOR_USER_INPUT, pending_input=pending_input)

    def _complete_turn(self, response: ChatResponse) -> AgentTurnResult:
        self.session.append_assistant_response(response)
        self._auto_compact()
        return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)

    def _continue_tool_loop_from_response(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        while response.tool_calls:
            self._check_cancelled()
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                return self._tool_round_limit_response(response), None, tool_rounds

            # 关键顺序：必须先写 assistant tool_call，再写对应 tool_result。provider 后续
            # 才能看到合法的 “assistant(tool_calls) -> tool(result)” 消息序列。
            self.session.append_assistant_response(response)
            execution = self.tool_executor.execute_interactive(response.tool_calls)
            if execution.pending_input is not None:
                return response, execution.pending_input, tool_rounds
            self._auto_compact()
            if execution.task_hash_changed:
                self._compact_after_task_hash_changed()

            tool_rounds += 1
            self._tool_rounds_completed = tool_rounds
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                return self._tool_round_limit_response(response), None, tool_rounds
            self._check_cancelled()
            response = self._drop_unsupported_tool_calls(complete_once())
        return response, None, tool_rounds

    def _run_todo_self_check_if_needed(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        instruction = self._todo_reconciliation_instruction()
        if instruction is None:
            return response, None, tool_rounds
        response = self._drop_unsupported_tool_calls(
            complete_once(runtime_instruction=instruction)
        )
        return self._continue_tool_loop_from_response(response, complete_once, tool_rounds)

    async def _run_todo_self_check_if_needed_async(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        instruction = self._todo_reconciliation_instruction()
        if instruction is None:
            return response, None, tool_rounds
        response = self._drop_unsupported_tool_calls(
            await complete_once(runtime_instruction=instruction)
        )
        return await self._continue_tool_loop_from_response_async(response, complete_once, tool_rounds)

    def _todo_reconciliation_instruction(self) -> str | None:
        if self._todo_reconciliation_attempted:
            return None
        instruction = self.todo_policy.final_reconciliation_instruction()
        if instruction is None:
            return None
        self._todo_reconciliation_attempted = True
        return instruction

    async def _continue_tool_loop_from_response_async(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        while response.tool_calls:
            self._check_cancelled()
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                return self._tool_round_limit_response(response), None, tool_rounds

            self.session.append_assistant_response(response)
            execution = await self.tool_executor.execute_interactive_async(response.tool_calls)
            if execution.pending_input is not None:
                return response, execution.pending_input, tool_rounds
            self._auto_compact()
            if execution.task_hash_changed:
                self._compact_after_task_hash_changed()

            tool_rounds += 1
            self._tool_rounds_completed = tool_rounds
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                return self._tool_round_limit_response(response), None, tool_rounds
            self._check_cancelled()
            response = self._drop_unsupported_tool_calls(await complete_once())
        return response, None, tool_rounds

    def _append_interrupted_tool_results(self) -> None:
        self._emit_settlements("interrupted", self.tool_settlement.append_interrupted_tail())

    def _repair_interrupted_tool_calls_before_provider_request(self) -> None:
        self._emit_settlements("interrupted", self.tool_settlement.repair_before_provider_request())

    def _emit_settlements(self, kind, settlements) -> None:
        for settlement in settlements:
            self._emit_tool_event(kind, settlement.tool_call, result=settlement.result)

    def _emit_tool_event(
        self,
        kind: Literal[
            "prewrite_review",
            "started",
            "finished",
            "permission_requested",
            "denied",
            "skipped",
            "interrupted",
        ],
        tool_call: ToolCall,
        *,
        result: ToolResult | None = None,
        permission_request: PermissionRequest | None = None,
        prewrite_review: dict[str, object] | None = None,
    ) -> None:
        if self.tool_event_handler is None:
            return
        self.tool_event_handler(
            ToolExecutionEvent(
                kind=kind,
                tool_call=tool_call,
                result=result,
                permission_request=permission_request,
                prewrite_review=prewrite_review,
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
                estimate_tokens=self._estimate_provider_request_tokens,
            )
        )

    def _auto_compact(self):
        return self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)

    def _compact_for_prompt_too_long(self):
        return self._compact_if_needed(trigger=ContextWindowTrigger.PROMPT_TOO_LONG)

    def _compact_after_task_hash_changed(self):
        self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)
        return self._compact_if_needed(trigger=ContextWindowTrigger.AUTO)


    def _estimate_provider_request_tokens(self, view) -> int:
        definitions = self._provider_tool_definitions()
        system_prefix = self.session.build_system_prefix(
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            provider_capabilities=getattr(self.provider, "capabilities", None),
        )
        messages = self._build_provider_messages(view, system_prefix=system_prefix)
        config = getattr(self.context_manager, "config", None)
        reserved_output_tokens = getattr(config, "reserved_output_tokens", 4_096)
        return estimate_chat_request_tokens(
            messages=messages,
            tools=definitions,
            reserved_output_tokens=reserved_output_tokens,
        )

    def _build_provider_messages(self, view, *, system_prefix):
        """Project context while retaining compatibility with extension builders."""

        build = self.context_builder.build_provider_messages
        parameters = inspect.signature(build).parameters.values()
        accepts_store_root = any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD or parameter.name == "store_root"
            for parameter in parameters
        )
        kwargs = {"system_prefix": system_prefix}
        if accepts_store_root:
            kwargs["store_root"] = self.session.store.root
        return build(view, **kwargs)

    def _request_messages(self, *, runtime_instruction: str | None = None):
        system_prefix = self.session.build_system_prefix(
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            provider_capabilities=getattr(self.provider, "capabilities", None),
        )
        if runtime_instruction:
            system_prefix = [
                *system_prefix,
                ChatMessage(role="system", content=runtime_instruction),
            ]
        return self._build_provider_messages(
            self.session.rebuild_view(),
            system_prefix=system_prefix,
        )

    def _provider_tool_definitions(self):
        """根据 provider 能力决定是否向模型暴露工具 schema。"""

        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is not None and not capabilities.supports_tools:
            return []
        return [
            definition
            for definition in self.session.tool_registry.definitions()
            if definition.name not in HIDDEN_TOOL_STATUS_NAMES
        ]

    def _begin_turn(self, *, new_user_turn: bool = True) -> None:
        if new_user_turn:
            self.provider_call_count = 0
            self.turn_started_at = self.clock()
            self._todo_reconciliation_attempted = False
            self._tool_rounds_completed = 0

    def _append_pending_guidance(self) -> None:
        if self.guidance_provider is None:
            return
        guidance_items = self.guidance_provider()
        for content in guidance_items:
            text = content.strip()
            if text:
                self.session.append_user_message(text)

    def _check_provider_call_limit(self) -> None:
        limit = self.limits.max_provider_calls
        if limit is not None and self.provider_call_count >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.PROVIDER_CALL_LIMIT)

    def _reserve_provider_call(self) -> None:
        self._check_provider_call_limit()
        self.provider_call_count += 1

    def _check_turn_timeout(self) -> None:
        limit = self.limits.max_turn_seconds
        if limit is None or self.turn_started_at is None:
            return
        if self.clock() - self.turn_started_at >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.TURN_TIMEOUT)

    def _is_cancelled(self) -> bool:
        return self.cancellation_token is not None and self.cancellation_token.is_cancelled

    def _check_cancelled(self) -> None:
        if self.cancellation_token is not None:
            self.cancellation_token.raise_if_cancelled()

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

    def _interrupted_response(self) -> ChatResponse:
        return ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content="当前任务已中断。",
            tool_calls=[],
            finish_reason="interrupted",
            raw={"interrupted": True},
        )



class _AgentLoopLimitReached(Exception):
    def __init__(self, reason: AgentLoopStopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason
