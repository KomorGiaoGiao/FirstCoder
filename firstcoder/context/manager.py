"""上下文窗口压缩触发编排。

`CompactionPipeline` 只负责 L1-L3 怎么压缩，`LlmCompactService` 只负责 L4 checkpoint
摘要。manager 这一层负责判断什么时候触发、触发原因是什么、程序化压缩不够时是否进入
L4，以及把压缩事件写回 append-only session 日志。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Protocol, Literal

from firstcoder.context.compaction import CompactionPipeline, CompactionRequest, CompactionEvent, CompactionResult
from firstcoder.context.fallback import CompactFallbackPolicy, FallbackStep
from firstcoder.context.llm_compact import LlmCompactRequest, LlmCompactService, LlmCompactEvent
from firstcoder.context.models import SessionView
from firstcoder.context.runtime_state import SessionRuntimeState, auto_compact_circuit_is_open
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.triggers import ContextCompactionConfig, evaluate_context_triggers
from firstcoder.context.writer import SessionEventWriter


class ContextWindowTrigger(StrEnum):
    AUTO = "auto"
    TASK_HASH_CHANGED = "task_hash_changed"
    PROMPT_TOO_LONG = "prompt_too_long"
    MANUAL = "manual"


class ContextCompactMode(StrEnum):
    AUTO = "auto"
    MANUAL = "manual"


ManagerStatus = Literal["success", "skipped", "failed"]


class ProgrammaticCompactor(Protocol):
    def compact(self, request: CompactionRequest):
        ...


class L4Compactor(Protocol):
    def compact(self, request: LlmCompactRequest):
        ...


@dataclass(slots=True)
class ContextCompactRequest:
    view: SessionView
    runtime_state: SessionRuntimeState
    trigger: ContextWindowTrigger | str = ContextWindowTrigger.AUTO
    mode: ContextCompactMode | str = ContextCompactMode.AUTO
    current_turn: int = 0
    target_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ContextCompactResult:
    status: ManagerStatus
    reason: str
    view: SessionView
    before_tokens: int
    after_tokens: int
    programmatic_event: CompactionEvent | None = None
    l4_event: LlmCompactEvent | None = None
    fallback_steps: list[dict[str, object]] | None = None
    final_failure_reason: str | None = None


@dataclass(slots=True)
class ContextWindowManager:
    """统一上下文压缩触发入口。"""

    store: JsonlSessionStore
    pipeline: ProgrammaticCompactor | None = None
    l4_service: L4Compactor | None = None
    config: ContextCompactionConfig | None = None
    fallback_policy: CompactFallbackPolicy = CompactFallbackPolicy()
    auto_compact_threshold: int = 32_000
    target_tokens: int = 24_000

    def __post_init__(self) -> None:
        if self.config is None:
            self.config = ContextCompactionConfig(
                auto_compact_threshold=self.auto_compact_threshold,
                target_tokens=self.target_tokens,
            )
        else:
            self.auto_compact_threshold = self.config.auto_compact_threshold
            self.target_tokens = self.config.target_tokens

        if self.pipeline is None:
            self.pipeline = CompactionPipeline(
                root=self.store.root,
                large_tool_result_tokens=self.config.large_tool_result_tokens,
                cold_turn_distance=self.config.cold_turn_distance,
                cold_preview_chars=self.config.cold_preview_chars,
            )

    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult:
        """根据触发源决定是否 compact，并把压缩事实写回事件日志。

        这个入口既服务自动触发，也服务强制触发：

        - AUTO：先看 token/工具输出/尾部消息数量是否超过阈值。
        - TASK_HASH_CHANGED：任务切换后强制整理旧任务上下文。
        - PROMPT_TOO_LONG：provider 已经拒绝请求，必须尝试 blocking compact。
        - MANUAL：用户主动要求观察压缩效果。
        """

        trigger = ContextWindowTrigger(request.trigger)
        mode = ContextCompactMode(request.mode)
        trigger_decision = evaluate_context_triggers(request.view, self.config)
        before_tokens = trigger_decision.estimated_tokens
        auto_failure_count_before = request.runtime_state.auto_compact_failure_count

        if not self._should_compact(trigger=trigger, decision=trigger_decision):
            # AUTO 场景下多数调用都会走到这里。返回 skipped 是有意义的状态，方便
            # `/compact status` 和测试知道“检查过，但还没到阈值”。
            return ContextCompactResult(
                status="skipped",
                reason=trigger_decision.reason,
                view=request.view,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
            )

        if (
            trigger == ContextWindowTrigger.AUTO
            and mode == ContextCompactMode.AUTO
            and auto_compact_circuit_is_open(request.runtime_state)
        ):
            # 连续失败后自动压缩会短暂熔断，避免每一轮对话都重复触发昂贵且失败的 L4。
            # 手动 compact 不受这个限制，方便用户主动排查。
            return ContextCompactResult(
                status="skipped",
                reason="circuit_open",
                view=request.view,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
            )

        target_tokens = request.target_tokens or self.config.target_for_trigger(trigger.value)
        force_route_current_text = _force_route_current_text_for_trigger(trigger)
        required_levels: tuple[Literal["l1", "l2", "l3"], ...] = (
            ("l2", "l3") if trigger == ContextWindowTrigger.TASK_HASH_CHANGED else ()
        )
        # 先跑确定性的 L1-L3。它们不依赖模型，成本低、结果可重放；只有仍然超预算时
        # 才进入 L4 LLM 摘要。
        programmatic = self.pipeline.compact(
            CompactionRequest(
                view=request.view,
                active_task_hash=request.runtime_state.active_task_hash,
                target_tokens=target_tokens,
                current_turn=request.current_turn,
                required_levels=required_levels,
                l2_result_target_tokens=self.config.l2_result_target_tokens,
                force_route_current_text=force_route_current_text,
                force_old_task_compaction=trigger == ContextWindowTrigger.TASK_HASH_CHANGED,
            )
        )
        self._record_programmatic_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=programmatic.event,
        )
        after_programmatic = evaluate_context_triggers(programmatic.view, self.config)
        after_tokens = after_programmatic.estimated_tokens

        if after_tokens <= target_tokens:
            # L1-L3 已经足够时，不需要调用 LLM。这里仍然写入 compaction_completed，
            # 这样 resume/debug 能看到本次压缩发生过什么。
            self._record_auto_success_if_needed(request=request, mode=mode)
            return ContextCompactResult(
                status="success",
                reason=_result_reason(trigger=trigger, auto_reason=trigger_decision.reason),
                view=programmatic.view,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                programmatic_event=programmatic.event,
            )

        if self.l4_service is None:
            # manager 可以在没有 L4 service 的测试/最小配置中运行。失败也写成
            # llm_compaction_completed，方便 runtime replay 恢复失败计数和状态。
            l4_event = LlmCompactEvent(
                status="failed",
                source_fingerprint=programmatic.event.input_fingerprint,
                failure_reason="l4_service_missing",
            )
            self._record_l4_event(
                session_id=request.view.session_id,
                trigger=trigger,
                target_tokens=target_tokens,
                event=l4_event,
            )
            self._record_auto_failure_if_needed(
                request=request,
                mode=mode,
                before_failure_count=auto_failure_count_before,
                failure_reason="l4_service_missing",
            )
            return ContextCompactResult(
                status="failed",
                reason="l4_service_missing",
                view=programmatic.view,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                programmatic_event=programmatic.event,
                l4_event=l4_event,
                final_failure_reason="l4_service_missing",
            )

        l4_result = self.l4_service.compact(
            LlmCompactRequest(
                view=programmatic.view,
                runtime_state=request.runtime_state,
                mode=mode.value,
            )
        )
        fallback_steps: list[dict[str, object]] = []
        if l4_result.event.status != "success":
            # L4 失败后不在 LlmCompactService 内部做复杂编排，而是在 manager 层统一应用
            # fallback policy。这样 L4 service 保持“生成 checkpoint”这个窄职责。
            fallback = self._run_fallback(
                request=request,
                trigger=trigger,
                mode=mode,
                target_tokens=target_tokens,
                programmatic=programmatic,
                l4_event=l4_result.event,
            )
            if fallback is not None:
                fallback_steps.extend(fallback.fallback_steps or [])
                failure_reason = fallback.final_failure_reason or l4_result.event.failure_reason or fallback.reason
                if fallback.status == "failed":
                    self._record_auto_failure_if_needed(
                        request=request,
                        mode=mode,
                        before_failure_count=auto_failure_count_before,
                        failure_reason=failure_reason,
                    )
                else:
                    self._record_auto_success_if_needed(request=request, mode=mode)
                return fallback

            failure_reason = l4_result.event.failure_reason or l4_result.event.status
            l4_event = _with_fallback(
                l4_result.event,
                fallback_steps=fallback_steps,
                final_failure_reason=failure_reason,
            )
            self._record_l4_event(
                session_id=request.view.session_id,
                trigger=trigger,
                target_tokens=target_tokens,
                event=l4_event,
            )
            self._record_auto_failure_if_needed(
                request=request,
                mode=mode,
                before_failure_count=auto_failure_count_before,
                failure_reason=failure_reason,
            )
            return ContextCompactResult(
                status="failed",
                reason=_result_reason(trigger=trigger, auto_reason=trigger_decision.reason),
                view=programmatic.view,
                before_tokens=before_tokens,
                after_tokens=after_tokens,
                programmatic_event=programmatic.event,
                l4_event=l4_event,
                fallback_steps=fallback_steps,
                final_failure_reason=failure_reason,
            )

        self._record_l4_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=l4_result.event,
        )
        rebuilt_view = self.store.rebuild_session_view(request.view.session_id)
        # checkpoint 写入后必须重新 rebuild，因为当前内存里的 programmatic.view 还不知道
        # 新 checkpoint 事件。后续 token 估算和 provider 投影都应基于重放后的视图。
        after_l4 = evaluate_context_triggers(rebuilt_view, self.config)
        self._record_auto_success_if_needed(request=request, mode=mode)

        return ContextCompactResult(
            status="success" if l4_result.event.status == "success" else l4_result.event.status,
            reason=_result_reason(trigger=trigger, auto_reason=trigger_decision.reason),
            view=rebuilt_view,
            before_tokens=before_tokens,
            after_tokens=after_l4.estimated_tokens,
            programmatic_event=programmatic.event,
            l4_event=l4_result.event,
            fallback_steps=fallback_steps,
        )

    def _run_fallback(
        self,
        *,
        request: ContextCompactRequest,
        trigger: ContextWindowTrigger,
        mode: ContextCompactMode,
        target_tokens: int,
        programmatic: CompactionResult,
        l4_event: LlmCompactEvent,
    ) -> ContextCompactResult | None:
        reason = l4_event.failure_reason or l4_event.status
        action = self.fallback_policy.action_for(reason)
        before_tokens = evaluate_context_triggers(programmatic.view, self.config).estimated_tokens

        if action == "stronger_programmatic":
            stronger = self.pipeline.compact(
                CompactionRequest(
                    view=programmatic.view,
                    active_task_hash=request.runtime_state.active_task_hash,
                    target_tokens=target_tokens,
                    current_turn=request.current_turn,
                    enabled_levels=("l1", "l2", "l3"),
                    required_levels=("l2", "l3") if trigger == ContextWindowTrigger.TASK_HASH_CHANGED else (),
                    l2_result_target_tokens=self.config.l2_result_target_tokens,
                    force_route_current_text=_force_route_current_text_for_trigger(trigger),
                    force_old_task_compaction=trigger == ContextWindowTrigger.TASK_HASH_CHANGED,
                )
            )
            self._record_programmatic_event(
                session_id=request.view.session_id,
                trigger=trigger,
                target_tokens=target_tokens,
                event=stronger.event,
            )
            after_decision = evaluate_context_triggers(stronger.view, self.config)
            status = "success" if after_decision.estimated_tokens <= target_tokens else "failed"
            step = FallbackStep(
                step=1,
                reason=reason,
                action=action,
                before_tokens=before_tokens,
                after_tokens=after_decision.estimated_tokens,
                status=status,
                error=None if status == "success" else "still_over_budget",
            ).to_dict()
            if status == "success":
                fallback_event = _with_fallback(
                    replace(l4_event, status="success", failure_reason="fallback_success"),
                    fallback_steps=[step],
                    final_failure_reason=None,
                )
                self._record_l4_event(
                    session_id=request.view.session_id,
                    trigger=trigger,
                    target_tokens=target_tokens,
                    event=fallback_event,
                )
                return ContextCompactResult(
                    status="success",
                    reason=_result_reason(trigger=trigger, auto_reason=reason),
                    view=stronger.view,
                    before_tokens=before_tokens,
                    after_tokens=after_decision.estimated_tokens,
                    programmatic_event=stronger.event,
                    l4_event=fallback_event,
                    fallback_steps=[step],
                )
            retry = self.l4_service.compact(
                LlmCompactRequest(
                    view=stronger.view,
                    runtime_state=request.runtime_state,
                    mode=mode.value,
                    summary_mode="stronger",
                )
            )
            rebuilt_view = self.store.rebuild_session_view(request.view.session_id)
            after_retry = evaluate_context_triggers(rebuilt_view, self.config)
            retry_status = "success" if retry.event.status == "success" else "failed"
            retry_step = FallbackStep(
                step=2,
                reason=retry.event.failure_reason or retry.event.status,
                action="retry_l4_stronger_summary",
                before_tokens=after_decision.estimated_tokens,
                after_tokens=after_retry.estimated_tokens,
                status=retry_status,
                error=retry.event.failure_reason,
            ).to_dict()
            retry_event = _with_fallback(
                retry.event,
                fallback_steps=[step, retry_step],
                final_failure_reason=None if retry_status == "success" else retry.event.failure_reason,
            )
            self._record_l4_event(
                session_id=request.view.session_id,
                trigger=trigger,
                target_tokens=target_tokens,
                event=retry_event,
            )
            return ContextCompactResult(
                status=retry_status,
                reason=_result_reason(trigger=trigger, auto_reason=reason),
                view=rebuilt_view,
                before_tokens=before_tokens,
                after_tokens=after_retry.estimated_tokens,
                programmatic_event=stronger.event,
                l4_event=retry_event,
                fallback_steps=[step, retry_step],
                final_failure_reason=None if retry_status == "success" else retry.event.failure_reason,
            )

        if action == "retry_l4_stronger_summary":
            retry = self.l4_service.compact(
                LlmCompactRequest(
                    view=programmatic.view,
                    runtime_state=request.runtime_state,
                    mode=mode.value,
                    summary_mode="stronger",
                )
            )
            rebuilt_view = self.store.rebuild_session_view(request.view.session_id)
            after_retry = evaluate_context_triggers(rebuilt_view, self.config)
            status = "success" if retry.event.status == "success" else "failed"
            step = FallbackStep(
                step=1,
                reason=reason,
                action=action,
                before_tokens=before_tokens,
                after_tokens=after_retry.estimated_tokens,
                status=status,
                error=retry.event.failure_reason,
            ).to_dict()
            retry_event = _with_fallback(
                retry.event,
                fallback_steps=[step],
                final_failure_reason=None if status == "success" else retry.event.failure_reason,
            )
            self._record_l4_event(
                session_id=request.view.session_id,
                trigger=trigger,
                target_tokens=target_tokens,
                event=retry_event,
            )
            return ContextCompactResult(
                status=status,
                reason=_result_reason(trigger=trigger, auto_reason=reason),
                view=rebuilt_view,
                before_tokens=before_tokens,
                after_tokens=after_retry.estimated_tokens,
                programmatic_event=programmatic.event,
                l4_event=retry_event,
                fallback_steps=[step],
                final_failure_reason=None if status == "success" else retry.event.failure_reason,
            )

        step = FallbackStep(
            step=1,
            reason=reason,
            action=action,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
            status="failed",
            error=reason,
        ).to_dict()
        l4_event = _with_fallback(
            l4_event,
            fallback_steps=[step],
            final_failure_reason=reason,
        )
        self._record_l4_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=l4_event,
        )
        return ContextCompactResult(
            status="failed",
            reason=_result_reason(trigger=trigger, auto_reason=reason),
            view=programmatic.view,
            before_tokens=before_tokens,
            after_tokens=before_tokens,
            programmatic_event=programmatic.event,
            l4_event=l4_event,
            fallback_steps=[step],
            final_failure_reason=reason,
        )

    def _record_auto_failure_if_needed(
        self,
        *,
        request: ContextCompactRequest,
        mode: ContextCompactMode,
        before_failure_count: int,
        failure_reason: str,
    ) -> None:
        if mode != ContextCompactMode.AUTO:
            return
        if request.runtime_state.auto_compact_failure_count > before_failure_count:
            return
        request.runtime_state.record_auto_compact_failure(failure_reason)

    def _record_auto_success_if_needed(
        self,
        *,
        request: ContextCompactRequest,
        mode: ContextCompactMode,
    ) -> None:
        if mode == ContextCompactMode.AUTO:
            request.runtime_state.record_auto_compact_success()

    def _should_compact(self, *, trigger: ContextWindowTrigger, decision) -> bool:
        """区分“阈值触发”和“语义/错误强制触发”。"""

        if trigger in {
            ContextWindowTrigger.MANUAL,
            ContextWindowTrigger.TASK_HASH_CHANGED,
            ContextWindowTrigger.PROMPT_TOO_LONG,
        }:
            return True
        return decision.should_compact

    def _record_programmatic_event(
        self,
        *,
        session_id: str,
        trigger: ContextWindowTrigger,
        target_tokens: int,
        event: CompactionEvent,
    ) -> None:
        SessionEventWriter(store=self.store, session_id=session_id).append_compaction_completed(
            trigger=trigger.value,
            target_tokens=target_tokens,
            event=event,
        )

    def _record_l4_event(
        self,
        *,
        session_id: str,
        trigger: ContextWindowTrigger,
        target_tokens: int,
        event: LlmCompactEvent,
    ) -> None:
        SessionEventWriter(store=self.store, session_id=session_id).append_llm_compaction_completed(
            trigger=trigger.value,
            target_tokens=target_tokens,
            event=event,
        )
def _result_reason(*, trigger: ContextWindowTrigger, auto_reason: str) -> str:
    if trigger == ContextWindowTrigger.AUTO:
        return auto_reason
    return trigger.value


def _force_route_current_text_for_trigger(trigger: ContextWindowTrigger) -> bool:
    return trigger in {ContextWindowTrigger.MANUAL, ContextWindowTrigger.PROMPT_TOO_LONG}


def _with_fallback(
    event: LlmCompactEvent,
    *,
    fallback_steps: list[dict[str, object]],
    final_failure_reason: str | None,
) -> LlmCompactEvent:
    return replace(
        event,
        fallback_steps=fallback_steps,
        final_failure_reason=final_failure_reason,
    )
