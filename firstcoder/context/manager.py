"""上下文窗口压缩触发编排。

`CompactionPipeline` 只负责 L1-L3 怎么压缩，`LlmCompactService` 只负责 L4 checkpoint
摘要。manager 这一层负责判断什么时候触发、触发原因是什么、程序化压缩不够时是否进入
L4，以及把压缩事件写回 append-only session 日志。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol, Literal

from firstcoder.context.compaction import CompactionPipeline, CompactionRequest, CompactionEvent
from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id
from firstcoder.context.llm_compact import LlmCompactRequest, LlmCompactService, LlmCompactEvent
from firstcoder.context.models import SessionView
from firstcoder.context.runtime_state import SessionRuntimeState, auto_compact_circuit_is_open
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.token_budget import estimate_text_tokens


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


@dataclass(slots=True)
class ContextWindowManager:
    """统一上下文压缩触发入口。"""

    store: JsonlSessionStore
    pipeline: ProgrammaticCompactor | None = None
    l4_service: L4Compactor | None = None
    auto_compact_threshold: int = 32_000
    target_tokens: int = 24_000

    def __post_init__(self) -> None:
        if self.pipeline is None:
            self.pipeline = CompactionPipeline(root=self.store.root)

    def compact_if_needed(self, request: ContextCompactRequest) -> ContextCompactResult:
        trigger = ContextWindowTrigger(request.trigger)
        mode = ContextCompactMode(request.mode)
        before_tokens = _estimate_view_tokens(request.view)

        if not self._should_compact(trigger=trigger, before_tokens=before_tokens):
            return ContextCompactResult(
                status="skipped",
                reason="under_threshold",
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

        target_tokens = request.target_tokens or self.target_tokens
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

        if programmatic.event.after_tokens <= target_tokens:
            return ContextCompactResult(
                status="success",
                reason=trigger.value,
                view=programmatic.view,
                before_tokens=before_tokens,
                after_tokens=programmatic.event.after_tokens,
                programmatic_event=programmatic.event,
            )

        if self.l4_service is None:
            return ContextCompactResult(
                status="failed",
                reason="l4_service_missing",
                view=programmatic.view,
                before_tokens=before_tokens,
                after_tokens=programmatic.event.after_tokens,
                programmatic_event=programmatic.event,
            )

        l4_result = self.l4_service.compact(
            LlmCompactRequest(
                view=programmatic.view,
                runtime_state=request.runtime_state,
                mode=mode.value,
            )
        )
        self._record_l4_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=l4_result.event,
        )
        rebuilt_view = self.store.rebuild_session_view(request.view.session_id)

        return ContextCompactResult(
            status="success" if l4_result.event.status == "success" else l4_result.event.status,
            reason=trigger.value,
            view=rebuilt_view,
            before_tokens=before_tokens,
            after_tokens=programmatic.event.after_tokens,
            programmatic_event=programmatic.event,
            l4_event=l4_result.event,
        )

    def _should_compact(self, *, trigger: ContextWindowTrigger, before_tokens: int) -> bool:
        if trigger in {
            ContextWindowTrigger.MANUAL,
            ContextWindowTrigger.TASK_HASH_CHANGED,
            ContextWindowTrigger.PROMPT_TOO_LONG,
        }:
            return True
        return before_tokens >= self.auto_compact_threshold

    def _record_programmatic_event(
        self,
        *,
        session_id: str,
        trigger: ContextWindowTrigger,
        target_tokens: int,
        event: CompactionEvent,
    ) -> None:
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=session_id,
                type="compaction_completed",
                payload={
                    "trigger": trigger.value,
                    "target_tokens": target_tokens,
                    "event": asdict(event),
                },
            )
        )

    def _record_l4_event(
        self,
        *,
        session_id: str,
        trigger: ContextWindowTrigger,
        target_tokens: int,
        event: LlmCompactEvent,
    ) -> None:
        self.store.append_event(
            SessionEvent(
                id=new_event_id(),
                session_id=session_id,
                type="llm_compaction_completed",
                payload={
                    "trigger": trigger.value,
                    "target_tokens": target_tokens,
                    "event": asdict(event),
                },
            )
        )


def _estimate_view_tokens(view: SessionView) -> int:
    return sum(estimate_text_tokens(part.content) for message in view.messages for part in message.parts)
