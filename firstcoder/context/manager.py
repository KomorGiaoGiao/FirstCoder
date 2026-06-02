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
        trigger = ContextWindowTrigger(request.trigger)
        mode = ContextCompactMode(request.mode)
        trigger_decision = evaluate_context_triggers(request.view, self.config)
        before_tokens = trigger_decision.estimated_tokens
        auto_failure_count_before = request.runtime_state.auto_compact_failure_count

        if not self._should_compact(trigger=trigger, decision=trigger_decision):
            return ContextCompactResult(
                status="skipped",
                reason=trigger_decision.reason,
                view=request.view,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
            )

        if mode == ContextCompactMode.AUTO and auto_compact_circuit_is_open(request.runtime_state):
            return ContextCompactResult(
                status="skipped",
                reason="circuit_open",
                view=request.view,
                before_tokens=before_tokens,
                after_tokens=before_tokens,
            )

        target_tokens = request.target_tokens or self.config.target_for_trigger(trigger.value)
        programmatic = self.pipeline.compact(
            CompactionRequest(
                view=request.view,
                active_task_hash=request.runtime_state.active_task_hash,
                target_tokens=target_tokens,
                current_turn=request.current_turn,
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
