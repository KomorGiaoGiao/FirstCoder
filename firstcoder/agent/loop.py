"""Agent 主循环最小闭环。"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Literal, cast

import anyio

from firstcoder.runtime.cancellation import AgentCancelledError, CancellationToken
from firstcoder.runtime.user_input import UserInputRequest
from firstcoder.agent.ports import ContextManagerLike
from firstcoder.agent.loop_limits import AgentLoopLimits, AgentLoopStopReason
from firstcoder.agent.provider_retry import DEFAULT_PROVIDER_RETRY_POLICY, ProviderRetryPolicy
from firstcoder.agent.execution_evidence import ExecutionEvidence, is_mutation_result
from firstcoder.agent.runtime_capabilities import AgentRuntimeCapabilities, PLANNING_TOOL_NAMES
from firstcoder.agent.session import AgentSession, PendingPermissionExecution
from firstcoder.agent.stagnation import StagnationGuard, append_guidance
from firstcoder.agent.telemetry import AgentTurnTelemetry
from firstcoder.agent.task_boundary_classifier import TaskBoundaryClassifier
from firstcoder.agent.task_plan_policy import TaskPlanPolicy, render_current_task_plan_snapshot
from firstcoder.agent.tool_execution import ToolExecutionEvent, ToolExecutor
from firstcoder.agent.tool_settlement import ToolCallSettlement
from firstcoder.agent.background import (
    DEFAULT_BACKGROUND_TOOL_NAMES,
    BackgroundJobManager,
    render_task_notification,
    with_background_controls,
)
from firstcoder.agent.user_input import (
    AgentTurnResult,
    AgentTurnStatus,
)
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.identity import new_request_id, stable_json_hash
from firstcoder.context.manager import ContextCompactRequest, ContextWindowTrigger
from firstcoder.context.token_budget import ContextBudget, build_context_budget
from firstcoder.context.task_boundary import TaskBoundaryService
from firstcoder.input.attachments import UserAttachment
from firstcoder.permissions.types import PermissionDecision, PermissionDecisionKind, PermissionRequest
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatMessage, ChatRequest, ChatResponse, ChatStreamEvent, FinishReason, MainRequestOptions, ToolCall
from firstcoder.tools.permission_results import (
    make_permission_denied_result,
    make_prewrite_review_failed_result,
    make_prewrite_review_stale_result,
)
from firstcoder.tools.background import create_background_cancel_tool, create_background_status_tool
from firstcoder.agent.subagent import SubagentRunner
from firstcoder.tools.delegate import create_delegate_tool
from firstcoder.tools.hidden import HIDDEN_TOOL_STATUS_NAMES
from firstcoder.tools.types import Tool, ToolResult, make_error_result


MAX_COMPLETION_GATE_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class PreparedMainRequest:
    request: ChatRequest
    request_id: str
    projection_fingerprint: str
    tool_result_part_ids: tuple[str, ...]


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
        limits: AgentLoopLimits | None = None,
        clock=time.monotonic,
        stream_event_handler: Callable[[ChatStreamEvent], None] | None = None,
        tool_event_handler: Callable[[ToolExecutionEvent], None] | None = None,
        guidance_provider: Callable[[], list[str]] | None = None,
        cancellation_token: CancellationToken | None = None,
        request_options: MainRequestOptions | None = None,
        classifier_provider: ChatProvider | None = None,
        classifier_request_options: MainRequestOptions | None = None,
        context_window: int | None = None,
        background_manager: BackgroundJobManager | None = None,
        background_tool_names: frozenset[str] | None = None,
        enable_delegate_tool: bool | None = None,
        runtime_capabilities: AgentRuntimeCapabilities | None = None,
        provider_retry_policy: ProviderRetryPolicy | None = None,
        provider_retry_sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.session = session
        self.tool_settlement = ToolCallSettlement(session)
        self.task_plan_policy = TaskPlanPolicy(session)
        self.provider = provider
        self.classifier_provider = classifier_provider or provider
        self.classifier_request_options = classifier_request_options or MainRequestOptions()
        self.request_options = request_options or MainRequestOptions()
        self.context_window = context_window
        self.context_builder = context_builder or ContextBuilder()
        self.context_manager = context_manager
        self.limits = limits or AgentLoopLimits.default()
        self.max_tool_rounds = self.limits.max_tool_rounds
        self.clock = clock
        self.telemetry_clock = time.monotonic
        self.provider_call_count = 0
        self.turn_started_at: float | None = None
        self.telemetry_started_at: float | None = None
        self.last_stream_events: list[ChatStreamEvent] = []
        self.stream_event_handler = stream_event_handler
        self.tool_event_handler = tool_event_handler
        self.guidance_provider = guidance_provider
        self.cancellation_token = cancellation_token
        self.provider_retry_policy = provider_retry_policy or DEFAULT_PROVIDER_RETRY_POLICY
        self.provider_retry_sleeper = provider_retry_sleeper or time.sleep
        self.background_manager = background_manager
        self.runtime_capabilities = runtime_capabilities or (
            AgentRuntimeCapabilities.benchmark(session.benchmark_task)
            if session.benchmark_task
            else AgentRuntimeCapabilities.interactive()
        )
        configured_background_names = self.runtime_capabilities.background_tool_names
        self.background_tool_names = (
            background_tool_names
            if background_tool_names is not None
            else configured_background_names or DEFAULT_BACKGROUND_TOOL_NAMES
        )
        self.enable_delegate_tool = (
            self.runtime_capabilities.enable_delegate_tool
            if enable_delegate_tool is None
            else enable_delegate_tool
        )
        self._task_plan_reconciliation_attempted = False
        self._completion_gate_attempted = False
        self._completion_gate_attempt_count = 0
        self._tool_rounds_completed = 0
        self.execution_evidence = ExecutionEvidence.for_task(session.benchmark_task)
        self.stagnation_guard = StagnationGuard()
        self.turn_telemetry = AgentTurnTelemetry()
        self.task_boundary_classifier = TaskBoundaryClassifier(
            session=session,
            provider=self.classifier_provider,
            request_options=self.classifier_request_options,
            context_builder=self.context_builder,
            compact_if_needed=self._compact_if_needed,
            check_cancelled=self._check_cancelled,
            reserve_provider_call=self._reserve_provider_call,
            check_turn_timeout=self._check_turn_timeout,
            tag_task_boundary_messages=self._tag_task_boundary_messages_with_active_hash,
        )
        # session 创建时通常已经注册了 session-scoped 工具。这里允许调用方再传入一批
        # 测试或临时工具，但避免重复注册同名工具导致模型 schema 不稳定。
        if tools:
            for tool in tools:
                if tool.name not in self.session.tool_registry.names():
                    self.session.tool_registry.register(tool)
        self._mcp_tool_names = {
            name
            for name in self.session.tool_registry.names()
            if name.startswith("mcp__")
        }
        self._active_mcp_tool_names: set[str] = set()
        # 初始化自动构造一个ToolExecutor
        self.tool_executor = ToolExecutor(
            session=session,
            settlement=self.tool_settlement,
            emit_event=self._emit_tool_event,
            check_cancelled=self._check_cancelled,
            cancellation_token=self.cancellation_token,
            tag_task_boundary_messages=self._tag_task_boundary_messages_with_active_hash,
            emit_settlements=self._emit_settlements,
            validate_tool_call=self._validate_tool_call,
            observe_tool_result=self._observe_tool_result,
            background_manager=self.background_manager,
            background_tool_names=self.background_tool_names,
        )
        self._ensure_background_control_tools()
        self._ensure_delegate_tool()

    async def run_user_turn(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
        streaming: bool = False,
    ) -> AgentTurnResult:
        """Execute one turn through the single asynchronous AgentTurnResult API."""

        if streaming:
            return await self._run_user_turn_streaming(content, attachments=attachments)
        return await anyio.to_thread.run_sync(
            lambda: self._run_user_turn_sync(content, attachments=attachments)
        )

    def replace_cancellation_token(self, token: CancellationToken | None) -> None:
        """Rebind cooperative cancellation when a paused turn resumes in the runner."""

        self.cancellation_token = token
        self.tool_executor.cancellation_token = token

    def clear_stream_events(self) -> None:
        self.last_stream_events = []

    def _validate_attachments(self, attachments: list[UserAttachment] | None) -> None:
        """在消息落库前确认 provider 能消费本轮附件。

        文件附件可以继续以内联文本或路径引用进入上下文；图片则必须由当前模型显式声明
        视觉能力，避免 provider 静默忽略图片后让 agent 在错误前提下执行任务。
        """

        if not attachments or not any(item.kind == "image" for item in attachments):
            return
        capabilities = getattr(self.provider, "capabilities", None)
        if bool(getattr(capabilities, "supports_vision", False)):
            return
        raise ValueError(
            f"模型 {self.provider.name}/{self.provider.model} 未声明视觉能力，"
            "无法发送图片附件；请在对应 [models] 配置中设置 vision = true。"
        )

    def _run_user_turn_sync(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:
        """Synchronous implementation kept private behind ``run_user_turn``."""

        try:
            return self._run_user_turn_sync_impl(content, attachments=attachments)
        except BaseException as exc:
            self._persist_errored_turn(exc)
            raise

    def _run_user_turn_sync_impl(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:

        if self.session.pending_permission_execution is not None:
            # 上一轮已经把 assistant tool_call 写进历史，但还缺一个匹配的 tool_result。
            # 这种情况下不能追加新的用户消息，否则 provider 会看到非法消息序列。
            pending = self.session.pending_permission_execution
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=self.tool_executor.permission_input_request_from_pending(pending),
            )

        self._validate_attachments(attachments)
        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        message_id = self.session.append_user_message(content, attachments=attachments)
        try:
            if self._initialize_active_task_if_missing(message_id) is None:
                # 已经有任务了, 用分类模型判断一下这次对话是否是新的任务
                self._classify_task_boundary(message_id)
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self._limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self._interrupted_response())

        return self._run_tool_loop_interactive(
            self._complete_once_with_recovery,
        )

    async def resume_with_user_input(
        self,
        request_id: str,
        answer: str,
        *,
        streaming: bool = False,
    ) -> AgentTurnResult:
        """Resume a paused turn through the single asynchronous result API."""

        if streaming:
            return await self._resume_with_user_input_streaming(request_id, answer)
        return await anyio.to_thread.run_sync(
            lambda: self._resume_with_user_input_sync(request_id, answer)
        )

    def _resume_with_user_input_sync(self, request_id: str, answer: str) -> AgentTurnResult:
        """用用户回答恢复一个暂停中的权限确认, 然后继续工具循环。

        为什么需要专门通道: 暂停时模型的 tool_call 已经落库, 但还没有配对的
        tool_result, 历史处于悬空状态; 而 provider API 要求 assistant(tool_calls)
        后面必须紧跟每个 tool_call_id 对应的 tool_result。如果像普通 ask_user 那样
        把用户的"同意/拒绝"当成下一条 user 消息直接追加, 序列就不合法, API 会拒绝。

        所以这里做的是"补洞": 按 request_id 从 session 找到 pending 的原始 tool_call
        (不信 UI 回传参数), 根据回答补齐最终 tool_result (拒绝则造拒绝结果, 同意则
        真正执行), 落库闭合历史后, 再进入工具循环继续问模型。
        """

        try:
            return self._resume_with_user_input_sync_impl(request_id, answer)
        except BaseException as exc:
            self._persist_errored_turn(exc)
            raise

    def _resume_with_user_input_sync_impl(self, request_id: str, answer: str) -> AgentTurnResult:
        # 恢复不算新一轮对话: 只有 pending 匹配时才重置计时器, 且不清空护栏计数,
        # 让这次恢复仍然占用原来那一轮的预算 (详见 _begin_turn)
        pending = self.session.pending_permission_execution
        if pending is not None and pending.request_id == request_id:
            self._begin_turn(new_user_turn=False)
        try:
            self._check_turn_timeout()
            self._check_cancelled()
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self._limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self._interrupted_response())
        # 核心一步: 按回答补齐悬空的 tool_result (拒绝则造拒绝结果, 同意则执行原始
        # tool_call), 落库闭合历史. 返回非 None 说明 pending 不存在等异常, 直接收尾
        result = self._append_permission_resume_result(request_id, answer)
        if result is not None:
            return result
        # 权限悬空已由上面补齐; 这里再对账一遍, 修复取消/崩溃留下的其他悬空 tool_call
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        # 历史合法后重新进入工具循环, 带着补全的 tool_result 继续问模型
        return self._run_tool_loop_interactive(self._complete_once_with_recovery)

    async def _resume_with_user_input_streaming(self, request_id: str, answer: str) -> AgentTurnResult:
        """流式模式下恢复权限确认，并继续消费 provider stream。"""

        try:
            return await self._resume_with_user_input_streaming_impl(request_id, answer)
        except BaseException as exc:
            self._persist_errored_turn(exc)
            raise

    async def _resume_with_user_input_streaming_impl(self, request_id: str, answer: str) -> AgentTurnResult:
        """同步路径 (_resume_with_user_input_sync_impl) 的流式镜像, 步骤一一对应:

        恢复计时 (不算新一轮) -> 超时/取消检查 -> 补洞落库 -> 进流式工具循环。
        """

        pending = self.session.pending_permission_execution
        if pending is not None and pending.request_id == request_id:
            self._begin_turn(new_user_turn=False)
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
        self._check_cancelled()
        return await self._run_tool_loop_interactive_async(self._stream_once_with_recovery)

    async def _run_user_turn_streaming(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:
        """使用 provider 内部 stream event 协议执行一轮会话。

        文本 delta 可以被上层即时展示，但工具调用仍保持原子语义：只有 stream 完成并
        返回完整 `ChatResponse.tool_calls` 后，才写入 assistant message 并执行工具。
        """

        try:
            return await self._run_user_turn_streaming_impl(content, attachments=attachments)
        except BaseException as exc:
            self._persist_errored_turn(exc)
            raise

    async def _run_user_turn_streaming_impl(
        self,
        content: str,
        *,
        attachments: list[UserAttachment] | None = None,
    ) -> AgentTurnResult:
        self.last_stream_events = []
        if self.session.pending_permission_execution is not None:
            pending = self.session.pending_permission_execution
            pending_input = self.tool_executor.permission_input_request_from_pending(pending)
            return AgentTurnResult(
                status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
                pending_input=pending_input,
            )

        self._validate_attachments(attachments)
        self._begin_turn()
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        message_id = self.session.append_user_message(content, attachments=attachments)
        try:
            if self._initialize_active_task_if_missing(message_id) is None:
                await self._classify_task_boundary_async(message_id)
        except _AgentLoopLimitReached as exc:
            return self._complete_turn(self._limit_response(exc.reason))
        except AgentCancelledError:
            return self._complete_turn(self._interrupted_response())

        result = await self._run_tool_loop_interactive_async(
            self._stream_once_with_recovery,
        )
        return result

    def _initialize_active_task_if_missing(self, basis_message_id: str):
        """若尚无进行中的任务, 则以该用户消息为起点自动建立首个任务。\n\n
        * 任务: 一段目标一致的连续对话
        """
        service = TaskBoundaryService(known_message_ids=self.session.known_message_ids)
        observation = service.initialize_active_task(self.session.runtime_state, basis_message_id=basis_message_id)
        if observation is not None:
            self.session.writer.append_task_boundary_observation(observation)
            self._tag_message_parts_with_task_hash(basis_message_id, observation.active_task_hash)
        return observation

    def _classify_task_boundary(self, basis_message_id: str) -> None:
        """发起隐藏的分类调用, 判断该消息是否开启新任务, 并将结果记入任务边界状态机。"""

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
                self._tag_message_parts_with_task_hash(
                    message_id,
                    active_hash if isinstance(active_hash, str) else None,
                )

    def _append_permission_resume_result(self, request_id: str, answer: str) -> AgentTurnResult | None:
        """补洞主流程: 校验 pending -> 按回答得到 tool_result -> 落库收尾。

        返回 None 表示悬空 tool_call 已闭合, 调用方应继续工具循环; 返回
        AgentTurnResult 表示前置校验失败 (无匹配 pending / 无权限管理器),
        直接作为本轮结果返回。
        """

        pending = self._pending_permission_for_resume(request_id)
        if isinstance(pending, AgentTurnResult):
            return pending
        # 被拦下的路径 (拒绝 / 预览失效) 在这里直接得到合成 tool_result, 不会执行工具
        result = self._prepare_permission_resume(pending, answer)
        if result is None:
            # 放行: 真正执行 session 保存的原始 tool_call, 拿到真实结果
            result = self._execute_resumed_permission_tool_call(pending)
            self._emit_finished_permission_resume(pending, result)
        self._finish_permission_resume(pending, result)
        return None

    async def _append_permission_resume_result_async(self, request_id: str, answer: str) -> AgentTurnResult | None:
        """同步版的异步镜像: 步骤完全一致, 仅把工具执行丢到线程里避免阻塞事件循环。"""

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
        """按 request_id 在 session 运行期状态里找待恢复的 pending。

        找不到 (或不匹配, 比如 UI 拿过期 id 重放) 就造一个错误收尾结果返回,
        用返回值而不是异常表达失败, 让调用方统一走"直接返回"分支。
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
        return pending

    def _prepare_permission_resume(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ) -> ToolResult | None:
        """先判定这个回答要不要拦住执行, 并发观测事件。

        返回 ToolResult: 被拦 (用户拒绝 / 写前预览失效), 调用方直接用该结果补洞;
        返回 None: 放行, 调用方接着执行原始 tool_call。
        """

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
        """补发执行完成的观测事件, 让 UI 能把 started -> finished 配成一对。"""

        self._emit_tool_event(
            "finished",
            pending.tool_call,
            result=result,
            permission_request=pending.permission_request,
        )

    def _finish_permission_resume(self, pending: PendingPermissionExecution, result: ToolResult) -> None:
        """收尾: 清 pending, 把最终 tool_result 落库, 悬空 tool_call 至此闭合。"""

        self.session.pending_permission_execution = None
        self._observe_tool_result(pending.tool_call, result)
        self.session.append_tool_result(tool_call=pending.tool_call, result=result)
        # 当初同批次里排在这个需审批调用之后的调用被跳过了, 这里补记为 skipped,
        # 保证同批每个 tool_call 都有配对的收尾记录
        self._emit_settlements("skipped", self.tool_settlement.append_skipped(pending.skipped_tool_calls))
        self._tool_rounds_completed += 1

    def _resolve_pending_confirmation(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ):
        """把用户的文本回答解析成权限决定 (ALLOW / DENY)。

        review_only (写前预览) 走单独分支: 同意前重新预检一遍, 防止预览期间文件
        或策略变化, 避免"批准了一个现在已经不允许的修改"。未知回答一律按 DENY
        处理 (fail-closed)。
        """

        permission_manager = self.session.permission_manager
        assert permission_manager is not None
        if not pending.review_only:
            return permission_manager.resolve_confirmation(pending.permission_request, answer)
        normalized = answer.strip().lower()
        if normalized in {"allow_once", "allow", "once", "2"}:
            current = self.session.preflight_tool_call_permission(pending.tool_call)
            if current is not None and current.decision.kind == PermissionDecisionKind.DENY:
                return current.decision
            return PermissionDecision(kind=PermissionDecisionKind.ALLOW, reason="用户批准应用已预览的修改。")
        if normalized in {"deny", "no", "1"} or normalized.startswith(("reject:", "reject_with_feedback:")):
            return permission_manager.resolve_confirmation(pending.permission_request, answer)
        return PermissionDecision(
            kind=PermissionDecisionKind.DENY,
            reason=f"未知写前预览选择：{answer}",
        )

    def _blocked_permission_resume_result(
        self,
        pending: PendingPermissionExecution,
        answer: str,
    ) -> ToolResult | None:
        """算出"不执行原调用"时的合成 tool_result; 返回 None 表示放行执行。

        三种拦截: ① 用户拒绝; ② 写前预览本身就失败; ③ 预览已过期 (确认期间
        目标文件被改动, 旧 diff 不可信)。
        """

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
        permission_manager = self.session.permission_manager
        assert permission_manager is not None
        if pending.prewrite_review.is_current(
            permission_manager.policy.project_root,
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

        这一步只负责“问模型一次”, 不处理工具循环。拆开后, 同步调用、streaming 调用、
        prompt-too-long 恢复都可以复用同一套上下文构造逻辑。
        """

        prepared = self._prepare_main_provider_request(
            tool_choice=tool_choice,
            runtime_instruction=runtime_instruction,
        )
        self._reserve_provider_call()
        self._check_turn_timeout()
        self._check_cancelled()
        response = self.provider.complete(prepared.request)
        self._record_projection_consumed(prepared)
        return response

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
        """同步模式下一次 provider 调用, 并处理 prompt-too-long 的单次恢复

        provider 如果拒绝请求, 说明 assistant 回复还没有产生, 也就没有新消息要落库。
        这时可以先触发 blocking compact, 再重建 provider messages 重试一次。
        """

        transient_retries = 0
        compaction_attempted = False
        while True:
            try:
                return self._complete_once(
                    tool_choice=tool_choice,
                    runtime_instruction=runtime_instruction,
                )
            except ProviderError as exc:
                if exc.retryable and transient_retries < self.provider_retry_policy.max_retries:
                    transient_retries += 1
                    self.turn_telemetry.observe_provider_retry(exc.kind)
                    self._wait_before_provider_retry(transient_retries)
                    continue
                if exc.requires_compaction and not compaction_attempted:
                    compaction_attempted = True
                    result = self._compact_for_prompt_too_long(
                        runtime_instruction=runtime_instruction
                    )
                    if result is not None and result.status == "success":
                        self.turn_telemetry.observe_provider_retry(exc.kind)
                        continue
                raise

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

        prepared = self._prepare_main_provider_request(
            tool_choice=tool_choice,
            runtime_instruction=runtime_instruction,
        )
        final_response: ChatResponse | None = None
        self._reserve_provider_call()
        self._check_turn_timeout()
        self._check_cancelled()
        async for event in self.provider.astream(prepared.request):
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
        self._record_projection_consumed(prepared)
        return final_response

    async def _stream_once_with_recovery(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
    ) -> ChatResponse:
        transient_retries = 0
        compaction_attempted = False
        while True:
            try:
                return await self._stream_once_attempt(
                    tool_choice=tool_choice,
                    runtime_instruction=runtime_instruction,
                )
            except ProviderError as exc:
                if exc.retryable:
                    transient_retries += 1
                    if transient_retries <= self.provider_retry_policy.max_retries:
                        self.turn_telemetry.observe_provider_retry(exc.kind)
                        await self._wait_before_provider_retry_async(transient_retries)
                    if transient_retries < self.provider_retry_policy.max_retries:
                        continue
                    return self._complete_once_with_recovery(
                        tool_choice=tool_choice,
                        runtime_instruction=runtime_instruction,
                    )
                if exc.requires_compaction and not compaction_attempted:
                    compaction_attempted = True
                    result = self._compact_for_prompt_too_long(
                        runtime_instruction=runtime_instruction
                    )
                    if result is not None and result.status == "success":
                        self.turn_telemetry.observe_provider_retry(exc.kind)
                        continue
                raise

    def _wait_before_provider_retry(self, retry_number: int) -> None:
        self._check_turn_timeout()
        self._check_cancelled()
        delay = self.provider_retry_policy.delay_for_retry(retry_number)
        self.provider_retry_sleeper(delay)
        self._check_turn_timeout()
        self._check_cancelled()

    async def _wait_before_provider_retry_async(self, retry_number: int) -> None:
        self._check_turn_timeout()
        self._check_cancelled()
        delay = self.provider_retry_policy.delay_for_retry(retry_number)
        await anyio.to_thread.run_sync(self.provider_retry_sleeper, delay)
        self._check_turn_timeout()
        self._check_cancelled()

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
        """核心工具循环: 问模型, 执行工具, 再把工具结果回喂给模型。

        退出条件只有三类:
        - 模型返回的 response 没有 tool_calls: 说明它已经给出最终回答。
        - 命中 max_tool_rounds: 防止模型无限调用工具。
        - 某个工具需要用户输入或权限确认: 暂停并把 pending_input 交给 UI。
        """

        guardrail_stop = False
        try:
            # 工具调用上限校验
            if self.max_tool_rounds is not None and self._tool_rounds_completed >= self.max_tool_rounds:
                return self._complete_turn(self._limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT))

            # 第一次调用模型, 获取模型回复, 判断是否需要进入工具循环
            response = self._drop_unsupported_tool_calls(complete_once(tool_choice=initial_tool_choice))
            tool_rounds = self._tool_rounds_completed

            # 主循环
            response, pending_input, tool_rounds = self._continue_tool_loop_from_response(
                response,
                complete_once,
                tool_rounds,
            )

            # 需要用户拍板
            if pending_input is not None:
                return self._pending_turn_result(pending_input)
            
            if response.finish_reason != AgentLoopStopReason.TOOL_ROUND_LIMIT.value:
                # 任务清单对账: 模型建的 todo 清单和实际执行是否一致
                response, pending_input, tool_rounds = self._run_task_plan_reconciliation_if_needed(
                    response,
                    complete_once,
                    tool_rounds,
                )
                if pending_input is not None:
                    return self._pending_turn_result(pending_input)

                # 完成闸门: 防止模型"嘴上说做完其实没做完"
                response, pending_input, _ = self._run_completion_gate_if_needed(
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

        if self._is_cancelled():    # 取消: 中断回复
            self._append_interrupted_tool_results()
            response = self._interrupted_response()
            return self._complete_turn(response)
        if guardrail_stop:          # 限额: 限流说明
            return self._complete_turn(response)

        # 没有工具调用时, 这条 response 就是最终 assistant 回复. 命中轮次上限时也会写入
        # 一条纯文本说明, 避免保存未执行的 tool_call
        return self._complete_turn(response)    # 正常：最终回复

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
                response, pending_input, tool_rounds = await self._run_task_plan_reconciliation_if_needed_async(
                    response,
                    complete_once,
                    tool_rounds,
                )
                if pending_input is not None:
                    return self._pending_turn_result(pending_input)
                response, pending_input, _ = await self._run_completion_gate_if_needed_async(
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

    def _pending_turn_result(self, pending_input: UserInputRequest) -> AgentTurnResult:
        self._persist_turn_telemetry(
            status="paused",
            stop_reason=pending_input.kind,
            finalize=False,
        )
        return AgentTurnResult(status=AgentTurnStatus.WAITING_FOR_USER_INPUT, pending_input=pending_input)

    def _complete_turn(self, response: ChatResponse) -> AgentTurnResult:
        """联合`ChatResponse`, 补充`status`构成`AgentTurnResult`
        """
        self.session.append_assistant_response(response)
        status = "completed"
        if response.finish_reason == "interrupted":
            status = "interrupted"
        elif response.finish_reason == "length" or response.finish_reason in {
            reason.value for reason in AgentLoopStopReason
        }:
            status = "limited"
        elif response.finish_reason in {"error", "content_filter"}:
            status = "errored"
        self._persist_turn_telemetry(
            status=status,
            stop_reason=response.finish_reason or "completed",
            finalize=True,
        )
        return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)

    def _continue_tool_loop_from_response(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        """从已有模型回复出发, 循环执行工具调用并把结果回喂模型, 直到模型给出最终答复。

        入参:
        - `response`: 当前模型回复, 其中的 tool_calls 决定是否进入循环;
        - `complete_once`: 调用大模型的函数(策略参数, 由调用方注入), 返回新的 ChatResponse;
        - `tool_rounds`: 已完成的工具轮次数, 用于校验上限。

        返回三元组: (最终模型回复, 需用户输入的请求(权限确认等, 无则为 None), 工具轮次数)。
        """

        while response.tool_calls:
            self._check_cancelled()
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                return self._tool_round_limit_response(response), None, tool_rounds

            # 关键顺序：必须先写 assistant tool_call，再写对应 tool_result。provider 后续
            # 才能看到合法的 “assistant(tool_calls) -> tool(result)” 消息序列。
            self.session.append_assistant_response(response)
            # 执行工具调用
            execution = self.tool_executor.execute_interactive(response.tool_calls)
            if execution.pending_input is not None:
                # 工具执行期间需要挂起(权限确认 ASK 或工具反问用户): 立即返回 pending_input,
                # 本轮暂停; 用户回复后走 resume 从断点恢复, 已执行的工具不会重跑。
                return response, execution.pending_input, tool_rounds
            if execution.task_hash_changed:
                # task_boundary 判定发生任务切换: 趁新旧任务的分界点先做上下文压缩
                self._compact_after_task_hash_changed()

            tool_rounds += 1
            self._tool_rounds_completed = tool_rounds
            if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
                return self._tool_round_limit_response(response), None, tool_rounds
            self._check_cancelled()
            response = self._drop_unsupported_tool_calls(complete_once())
        return response, None, tool_rounds

    def _run_task_plan_reconciliation_if_needed(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        instruction = self._final_reconciliation_instruction()
        if instruction is None:
            return response, None, tool_rounds
        response = self._drop_unsupported_tool_calls(complete_once(runtime_instruction=instruction))
        return self._continue_tool_loop_from_response(response, complete_once, tool_rounds)

    async def _run_task_plan_reconciliation_if_needed_async(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        instruction = self._final_reconciliation_instruction()
        if instruction is None:
            return response, None, tool_rounds
        response = self._drop_unsupported_tool_calls(await complete_once(runtime_instruction=instruction))
        return await self._continue_tool_loop_from_response_async(response, complete_once, tool_rounds)

    def _final_reconciliation_instruction(self) -> str | None:
        if self._task_plan_reconciliation_attempted:
            return None
        instruction = self.task_plan_policy.final_reconciliation_instruction()
        if instruction is None:
            return None
        self._task_plan_reconciliation_attempted = True
        return instruction

    def _run_completion_gate_if_needed(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        while True:
            instruction = self._completion_gate_instruction()
            if instruction is None:
                return response, None, tool_rounds
            response = self._drop_unsupported_tool_calls(
                complete_once(runtime_instruction=instruction)
            )
            response, pending_input, tool_rounds = self._continue_tool_loop_from_response(
                response,
                complete_once,
                tool_rounds,
            )
            if pending_input is not None:
                return response, pending_input, tool_rounds

    async def _run_completion_gate_if_needed_async(
        self,
        response: ChatResponse,
        complete_once,
        tool_rounds: int,
    ) -> tuple[ChatResponse, UserInputRequest | None, int]:
        while True:
            instruction = self._completion_gate_instruction()
            if instruction is None:
                return response, None, tool_rounds
            response = self._drop_unsupported_tool_calls(
                await complete_once(runtime_instruction=instruction)
            )
            response, pending_input, tool_rounds = await self._continue_tool_loop_from_response_async(
                response,
                complete_once,
                tool_rounds,
            )
            if pending_input is not None:
                return response, pending_input, tool_rounds

    def _completion_gate_instruction(self) -> str | None:
        if (
            not self.runtime_capabilities.enable_completion_gate
            or self._completion_gate_attempted
            or self._completion_gate_attempt_count >= MAX_COMPLETION_GATE_ATTEMPTS
        ):
            return None
        jobs = ()
        if self.background_manager is not None:
            jobs = self.background_manager.list(session_id=self.session.session_id)
        decision = self.execution_evidence.completion_decision(
            background_jobs=jobs,
        )
        instruction = decision.render_instruction()
        if instruction is not None:
            self._completion_gate_attempted = True
            self._completion_gate_attempt_count += 1
            self.turn_telemetry.observe_completion_gate(reason_count=len(decision.reasons))
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
        """调模型前的对账: 补齐上一轮被打断留下的悬空 tool_call

        Esc 取消 / 轮次超时 / 异常崩溃可能让日志尾部出现"有 tool_call、无
        tool_result"的残缺序列, 直接发给 provider 会消息非法. 这里为每个孤儿
        调用补写一条"结果未知"的中断记录, 恢复"每个 tool_call 必有配对结果"
        的不变量. 权限暂停造成的悬空不在此列——那是合法的在途交易, 由
        resume_with_user_input 补齐, 此处会跳过.
        """

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
            "background_started",
        ],
        tool_call: ToolCall,
        *,
        result: ToolResult | None = None,
        permission_request: PermissionRequest | None = None,
        prewrite_review: dict[str, object] | None = None,
    ) -> None:
        """把工具执行节点广播给订阅方(UI)。

        将 kind 及对应载荷打包成 ToolExecutionEvent, 调用注入的 tool_event_handler;
        无订阅方时静默跳过。kind 取值:
        - prewrite_review: 写入类工具执行前的预检(展示 diff 给用户);
        - started: 开始执行; finished: 执行完毕(带 result);
        - permission_requested: 卡在权限确认(带 permission_request);
        - denied: 被拒(校验失败/隐藏工具/权限拒绝);
        - skipped: 本轮被跳过(前面已有 pending);
        - interrupted: 被 Esc/超时打断;
        - background_started: 转入后台执行。
        """

        if self.tool_event_handler is None:
            return
        # tool_event_handler 是注入的回调函数, 由 firstcoder/app/tui_view.py 在每轮对话开始时安装
        # 带括号即调用该函数, 把事件对象作为入参传给它
        self.tool_event_handler(
            ToolExecutionEvent(
                kind=kind,
                tool_call=tool_call,
                result=result,
                permission_request=permission_request,
                prewrite_review=prewrite_review,
            )
        )

    def _prepare_main_provider_request(
        self,
        *,
        tool_choice="auto",
        runtime_instruction: str | None = None,
    ) -> PreparedMainRequest:
        """修补上下文, 必要时压缩, 最后拼出请求
        """
        self._repair_interrupted_tool_calls_before_provider_request()
        self._check_cancelled()
        self._append_pending_guidance()
        self._append_background_notifications()
        definitions = self._provider_tool_definitions()
        view = self.session.rebuild_view()
        budget = self._context_budget_for_view(
            view,
            runtime_instruction=runtime_instruction,
            definitions=definitions,
        )
        if self.context_manager is not None:
            # 必要时进行压缩
            result = self.context_manager.compact_if_needed(
                ContextCompactRequest(
                    view=view,
                    runtime_state=self.session.runtime_state,
                    budget=budget,
                    estimate_budget=lambda candidate: self._context_budget_for_view(
                        candidate,
                        runtime_instruction=runtime_instruction,
                        definitions=definitions,
                    ),
                    trigger=ContextWindowTrigger.AUTO,
                    current_turn=self.session.current_turn,
                )
            )
            if result.status == "success":
                view = self.session.rebuild_view()

        messages = self._request_messages(
            view=view,
            runtime_instruction=runtime_instruction,
        )
        # 组装
        request = self._main_chat_request(messages, definitions, tool_choice)
        return PreparedMainRequest(
            request=request,
            request_id=new_request_id(),
            projection_fingerprint=stable_json_hash(
                {
                    "messages": [asdict(message) for message in messages],
                    "tools": [asdict(definition) for definition in definitions],
                },
                length=24,
            ),
            tool_result_part_ids=self.context_builder.projected_tool_result_part_ids(view),
        )

    def _record_projection_consumed(self, prepared: PreparedMainRequest) -> None:
        self.session.record_provider_projection_consumed(
            request_id=prepared.request_id,
            projection_fingerprint=prepared.projection_fingerprint,
            part_ids=prepared.tool_result_part_ids,
            provider=self.provider.name,
            model=self.provider.model,
        )

    def _context_budget_for_view(
        self,
        view,
        *,
        runtime_instruction: str | None,
        definitions,
    ) -> ContextBudget:
        messages = self._request_messages(
            view=view,
            runtime_instruction=runtime_instruction,
        )
        return build_context_budget(
            messages=messages,
            tools=definitions,
            context_window=self.context_window,
            max_output_tokens=self.request_options.max_tokens,
        )

    def context_budget_for_view(self, view) -> ContextBudget:
        return self._context_budget_for_view(
            view,
            runtime_instruction=None,
            definitions=self._provider_tool_definitions(),
        )

    def _compact_if_needed(
        self,
        *,
        trigger: ContextWindowTrigger,
        runtime_instruction: str | None = None,
    ):
        """把压缩触发交给 context manager。

        AgentLoop 不判断 token 细节，也不决定 L1/L2/L3/L4 怎么做；它只在关键时机告诉
        context 层：“现在可能需要整理上下文了”。
        """

        if self.context_manager is None:
            return None
        definitions = self._provider_tool_definitions()
        view = self.session.rebuild_view()
        budget = self._context_budget_for_view(
            view,
            runtime_instruction=runtime_instruction,
            definitions=definitions,
        )
        return self.context_manager.compact_if_needed(
            ContextCompactRequest(
                view=view,
                runtime_state=self.session.runtime_state,
                budget=budget,
                estimate_budget=lambda candidate: self._context_budget_for_view(
                    candidate,
                    runtime_instruction=runtime_instruction,
                    definitions=definitions,
                ),
                trigger=trigger,
                current_turn=self.session.current_turn,
            )
        )

    def _compact_for_prompt_too_long(self, *, runtime_instruction: str | None = None):
        return self._compact_if_needed(
            trigger=ContextWindowTrigger.PROMPT_TOO_LONG,
            runtime_instruction=runtime_instruction,
        )

    def _compact_after_task_hash_changed(self):
        return self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)

    def _build_provider_messages(self, view, *, system_prefix):
        return self.context_builder.build_provider_messages(
            view,
            system_prefix=system_prefix,
            store_root=self.session.store.root,
        )

    def _request_messages(self, *, view=None, runtime_instruction: str | None = None):
        """拼出本次请求的完整消息: 静态系统前缀 + 动态叠加(临时指令/验收契约/计划快照),
        再与消息历史合并成 provider 格式的 messages。"""

        resolved_view = view or self.session.rebuild_view()
        system_prefix = self.session.build_system_prefix(
            provider_name=self.provider.name,
            provider_model=self.provider.model,
            provider_capabilities=getattr(self.provider, "capabilities", None),
        )
        # 以下是动态叠加内容
        # 本次请求的临时指令
        if runtime_instruction:
            system_prefix = [
                *system_prefix,
                ChatMessage(role="system", content=runtime_instruction),
            ]
        
        # 验收契约
        acceptance_contract = self.execution_evidence.render_acceptance_contract()
        if acceptance_contract:
            system_prefix = [
                *system_prefix,
                ChatMessage(role="system", content=acceptance_contract),
            ]

        # task_plan 快照
        if resolved_view.task_plan is not None:
            system_prefix = [
                *system_prefix,
                ChatMessage(
                    role="system",
                    content=render_current_task_plan_snapshot(resolved_view.task_plan),
                ),
            ]
        return self._build_provider_messages(
            resolved_view,
            system_prefix=system_prefix,
        )

    def _provider_tool_definitions(self):
        """从注册表全集中投影出"此刻告诉模型"的工具`schema`子集。

        `registry`是"定义+执行"的事实来源, 本函数只决定`schema`层面给模型看哪些。
        原则是`advertised`的每个工具这一时刻必然可调, 否则就是非法调用:
        - `provider`不支持工具调用 → 整体不暴露;
        - `task_boundary`是 loop 自用的内部记账工具, 模型看得到结果但调不了;
        - 未连上的 MCP server 的工具暂不暴露(模型调了也没有执行器);
        - think/web_search/ask_user/planning 按运行时开关过滤,
          不能兑现的能力不宣传(如 benchmark 下无人回答 ask_user);
        - 已有任务计划时放行`planning`工具, 供模型维护计划。
        执行永远不经过这里, 走 session.execute_tool_call → registry。
        """

        # 获取模型能力
        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is not None and not capabilities.supports_tools:
            return []
        definitions = []
        # 遍历registry中的工具, 将符合条件的工具收集并返回
        for definition in self.session.tool_registry.definitions():
            # task_boundary不暴露给模型(只提供给分类模型)
            if definition.name in HIDDEN_TOOL_STATUS_NAMES:
                continue
            if (
                definition.name in self._mcp_tool_names
                and definition.name not in self._active_mcp_tool_names
            ):
                continue
            if definition.name == "think" and not self.runtime_capabilities.expose_think_tool:
                continue
            if (
                definition.name == "web_search"
                and not self.runtime_capabilities.expose_web_search_tool
            ):
                continue
            if (
                definition.name == "ask_user"
                and not self.runtime_capabilities.allow_user_input
            ):
                continue
            if (
                definition.name in PLANNING_TOOL_NAMES
                and not self.runtime_capabilities.expose_planning_tools
                and self.session.rebuild_view().task_plan is None
            ):
                continue
            definitions.append(self._augment_tool_definition(definition))
        return definitions

    def _augment_tool_definition(self, definition):
        """给确实支持后台的工具, 在发出去的 schema 副本上标注后台控制能力。

        谁能后台由 background_manager(运行时存在) 和 background_tool_names(白名单)
        共同决定; 只有两者都满足才附加 run_in_background 等控制字段, 模型传了
        这些参数才有人兑现。只改副本不改 registry 里的原件, executor 真正
        调用前会把控制字段剥掉, 不传给底层工具函数。
        """

        if self.background_manager is None:
            return definition
        if definition.name not in self.background_tool_names:
            return definition
        return with_background_controls(definition)

    def _ensure_background_control_tools(self) -> None:
        """Register background_status/background_cancel whenever background runtime exists."""

        if self.background_manager is None:
            return
        names = set(self.session.tool_registry.names())
        if "background_status" not in names:
            self.session.tool_registry.register(create_background_status_tool(self.background_manager, session_id=self.session.session_id))
        if "background_cancel" not in names:
            self.session.tool_registry.register(create_background_cancel_tool(self.background_manager, session_id=self.session.session_id))

    def _ensure_delegate_tool(self) -> None:
        """Register the parent-facing delegate tool with a non-recursive child runner."""

        if not self.enable_delegate_tool:
            return
        if "delegate" in self.session.tool_registry.names():
            return
        project_root = None
        if self.session.permission_manager is not None:
            project_root = self.session.permission_manager.policy.project_root
        runner = SubagentRunner(
            store=self.session.store,
            provider=self.provider,
            tools=[tool for tool in self.session.tool_registry.tools() if tool.name != "delegate"],
            project_root=project_root,
            agents_md=self.session.agents_md,
            skill_catalog=self.session.skill_catalog,
            permission_manager=self.session.permission_manager,
            sandbox_access=self.session.sandbox_access,
            request_options=self.request_options,
        )
        self.session.tool_registry.register(
            create_delegate_tool(
                runner,
                parent_session_id=self.session.session_id,
                parent_task_hash=self.session.runtime_state.active_task_hash,
            )
        )

    def _begin_turn(self, *, new_user_turn: bool = True) -> None:
        """轮次状态处理\n\n
        * 新对话: 清空
        * 权限确认暂停后恢复: 不清空
        """
        if new_user_turn:
            # 新对话
            self._active_mcp_tool_names.clear()
            self.provider_call_count = 0
            self.turn_started_at = self.clock()
            self.telemetry_started_at = self.telemetry_clock()
            self._task_plan_reconciliation_attempted = False
            self._completion_gate_attempted = False
            self._completion_gate_attempt_count = 0
            self._tool_rounds_completed = 0
            self.execution_evidence.reset()
            self.stagnation_guard.reset()
            self.turn_telemetry.begin(
                turn_number=self.session.current_turn + 1,
                started_at=self.telemetry_started_at,
            )
        elif not self.turn_telemetry.active:
            # 当遇到权限确认而暂停时, 用户的回复(选择权限)并不意味着新一轮对话
            # 因此权限确认后的恢复并不能清空当前AgentLoop
            self.turn_started_at = self.clock()
            self.telemetry_started_at = self.telemetry_clock()
            self.turn_telemetry.begin(
                turn_number=self.session.current_turn,
                started_at=self.telemetry_started_at,
            )

    def _validate_tool_call(self, tool_call: ToolCall) -> ToolResult | None:
        validation_error = self._validate_mcp_tool_call(tool_call)
        if validation_error is not None:
            return validation_error
        route_error = self._runtime_tool_visibility_error(tool_call)
        if route_error is not None:
            return route_error
        if not self.runtime_capabilities.enable_stagnation_guard:
            return None
        return self.stagnation_guard.validate(tool_call)

    def _observe_tool_result(self, tool_call: ToolCall, result: ToolResult) -> None:
        self._observe_mcp_search_result(tool_call, result)
        self.turn_telemetry.observe_tool_result(
            tool_call,
            result,
            elapsed_seconds=self._turn_elapsed_seconds(),
        )
        if not (
            self.runtime_capabilities.enable_completion_gate
            or self.runtime_capabilities.enable_stagnation_guard
        ):
            return
        if (
            tool_call.name == "task_boundary"
            and result.ok
            and result.data.get("should_trigger_compaction")
        ):
            self.execution_evidence.reset()
            self.stagnation_guard.reset()
            self._completion_gate_attempted = False
            self._completion_gate_attempt_count = 0
            return
        if self.runtime_capabilities.enable_completion_gate:
            self.execution_evidence.observe(tool_call, result)
            if self._completion_gate_attempted and is_mutation_result(tool_call, result):
                self._completion_gate_attempted = False
        if self.runtime_capabilities.enable_stagnation_guard:
            append_guidance(result, self.stagnation_guard.observe(tool_call, result))

    def _runtime_tool_visibility_error(self, tool_call: ToolCall) -> ToolResult | None:
        if tool_call.name == "think" and not self.runtime_capabilities.expose_think_tool:
            return make_error_result(
                tool_call.name,
                "当前运行模式未暴露 think 工具。",
                tool_not_routed=True,
            )
        if tool_call.name == "ask_user" and not self.runtime_capabilities.allow_user_input:
            return make_error_result(
                tool_call.name,
                "当前运行模式为非交互 benchmark，未暴露 ask_user；请基于题面自主决策并验证目标。",
                tool_not_routed=True,
            )
        if (
            tool_call.name == "web_search"
            and not self.runtime_capabilities.expose_web_search_tool
        ):
            return make_error_result(
                tool_call.name,
                "当前 benchmark 协议未暴露 web_search；只能 fetch 题面已知的明确 URL。",
                tool_not_routed=True,
            )
        if (
            tool_call.name in PLANNING_TOOL_NAMES
            and not self.runtime_capabilities.expose_planning_tools
            and self.session.rebuild_view().task_plan is None
        ):
            return make_error_result(
                tool_call.name,
                "当前任务被路由为简单单任务，未暴露 TaskPlan 工具；请直接完成并验证目标。",
                tool_not_routed=True,
            )
        if tool_call.name == "delegate" and not self.enable_delegate_tool:
            return make_error_result(
                tool_call.name,
                "当前任务未启用子代理；请使用主代理工具直接完成。",
                tool_not_routed=True,
            )
        return None

    def _validate_mcp_tool_call(self, tool_call: ToolCall) -> ToolResult | None:
        if tool_call.name not in self._mcp_tool_names:
            return None
        if tool_call.name in self._active_mcp_tool_names:
            return None
        return make_error_result(
            tool_call.name,
            "MCP tool is not active for this user turn. Call mcp_tool_search first.",
            mcp_activation_required=True,
        )

    def _observe_mcp_search_result(
        self,
        tool_call: ToolCall,
        result: ToolResult,
    ) -> None:
        if tool_call.name != "mcp_tool_search" or not result.ok:
            return
        payload = result.data.get("mcp_tool_search")
        if not isinstance(payload, dict):
            return
        activated = payload.get("activated_tools")
        if not isinstance(activated, list):
            return
        self._active_mcp_tool_names.update(
            name
            for name in activated
            if isinstance(name, str) and name in self._mcp_tool_names
        )

    def _append_pending_guidance(self) -> None:
        """把用户在轮次运行期间插的话, 逐条转为`user_message`补进上下文(落盘),\n\n
        让模型在下一次请求时能看到并即时调整行动
        """
        if self.guidance_provider is None:
            return
        guidance_items = self.guidance_provider()
        for content in guidance_items:
            text = content.strip()
            if text:
                self.session.append_user_message(text)

    def _append_background_notifications(self) -> None:
        """调模型前, 把这段时间里已完成的后台任务逐条转成 `background_notification`
        消息补进历史; 通知不复用原始 tool_call_id(那个调用早已有"转后台"的
        tool_result), 保证每个 tool_call 恰好配一条 result, 消息对 provider 合法。"""

        if self.background_manager is None:
            return
        for notification in self.background_manager.collect_completed(session_id=self.session.session_id):
            self.session.append_background_notification(
                content=render_task_notification(notification),
                job_id=notification.job_id,
                tool_name=notification.tool_name,
                status=notification.status,
                task_id=notification.task_id,
                observed_revision=notification.observed_revision,
            )

    def _check_provider_call_limit(self) -> None:
        limit = self.limits.max_provider_calls
        if limit is not None and self.provider_call_count >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.PROVIDER_CALL_LIMIT)

    def _reserve_provider_call(self) -> None:
        """provider 调用前的预算检票口: 超限抛异常, 通过则预扣额度并记录遥测。"""

        self._check_provider_call_limit()
        self.provider_call_count += 1
        self.turn_telemetry.observe_provider_call()

    def _turn_elapsed_seconds(self) -> float:
        if self.telemetry_started_at is None:
            return 0.0
        return max(0.0, self.telemetry_clock() - self.telemetry_started_at)

    def _persist_turn_telemetry(
        self,
        *,
        status: str,
        stop_reason: str,
        finalize: bool,
        provider_failure_category: str | None = None,
    ) -> None:
        payload = self.turn_telemetry.snapshot(
            status=status,
            stop_reason=stop_reason,
            elapsed_seconds=self._turn_elapsed_seconds(),
            provider_failure_category=provider_failure_category,
            finalize=finalize,
        )
        if payload is not None:
            self.session.writer.append_agent_turn_telemetry(payload)

    def _persist_errored_turn(self, exc: BaseException) -> None:
        if not self.turn_telemetry.active:
            return
        if (
            isinstance(exc, AgentCancelledError | KeyboardInterrupt)
            or type(exc).__name__ == "CancelledError"
        ):
            self._persist_turn_telemetry(
                status="interrupted",
                stop_reason="interrupted",
                finalize=True,
            )
            return
        provider_category = exc.kind.value if isinstance(exc, ProviderError) else None
        stop_reason = provider_category or type(exc).__name__
        self._persist_turn_telemetry(
            status="errored",
            stop_reason=stop_reason,
            provider_failure_category=provider_category,
            finalize=True,
        )

    def _check_turn_timeout(self) -> None:
        limit = self.limits.max_turn_seconds
        if limit is None or self.turn_started_at is None:
            return
        if self.clock() - self.turn_started_at >= limit:
            raise _AgentLoopLimitReached(AgentLoopStopReason.TURN_TIMEOUT)

    def _is_cancelled(self) -> bool:
        return self.cancellation_token is not None and self.cancellation_token.is_cancelled

    def _check_cancelled(self) -> None:
        """判断当前Token是否被取消
        """
        if self.cancellation_token is not None:
            self.cancellation_token.raise_if_cancelled()

    def _drop_unsupported_tool_calls(self, response: ChatResponse) -> ChatResponse:
        """兜底保护：不支持工具的 provider 理论上不该返回 tool_calls。

        如果兼容站行为异常仍返回了 tool_calls, 这里把它们丢弃并记录 diagnostics, 避免
        agent 执行一个 provider 能力声明之外的工具链。
        """

        capabilities = getattr(self.provider, "capabilities", None)
        if capabilities is None or capabilities.supports_tools or not response.tool_calls:
            return response
        self._drop_unsupported_tool_call_stream_events()
        response.diagnostics.warnings.append("provider returned tool_calls even though supports_tools is false; tool calls were ignored")
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
        self.last_stream_events = [event for event in self.last_stream_events if event.kind not in {"tool_call_started", "tool_call_delta", "tool_call_completed"}]

    def _tool_round_limit_response(self, response: ChatResponse) -> ChatResponse:
        """工具轮次上限命中后，只保存纯文本说明，避免写入未执行的 tool_call。"""

        return self._limit_response(AgentLoopStopReason.TOOL_ROUND_LIMIT, raw=response.raw)

    def _limit_response(self, reason: AgentLoopStopReason, *, raw: dict | None = None) -> ChatResponse:
        messages = {
            AgentLoopStopReason.PROVIDER_CALL_LIMIT: (f"provider 调用次数达到上限（max_provider_calls={self.limits.max_provider_calls}），已停止继续执行。"),
            AgentLoopStopReason.TURN_TIMEOUT: (f"本轮任务耗时达到上限（max_turn_seconds={self.limits.max_turn_seconds}），已停止继续执行。"),
            AgentLoopStopReason.TOOL_ROUND_LIMIT: (f"工具调用轮次达到上限（max_tool_rounds={self.limits.max_tool_rounds}），已停止继续执行工具。"),
        }
        return ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content=messages[reason],
            tool_calls=[],
            finish_reason=cast(FinishReason, reason.value),
            raw=raw,
        )

    def _interrupted_response(self) -> ChatResponse:
        return ChatResponse(
            provider=self.provider.name,
            model=self.provider.model,
            content="当前任务已中断。",
            tool_calls=[],
            finish_reason=cast(FinishReason, "interrupted"),
            raw={"interrupted": True},
        )


class _AgentLoopLimitReached(Exception):
    def __init__(self, reason: AgentLoopStopReason) -> None:
        super().__init__(reason.value)
        self.reason = reason
