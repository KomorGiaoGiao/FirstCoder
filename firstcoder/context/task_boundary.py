"""任务边界观察与程序生成 task hash。

这一层接收模型的极简判断：same/new/uncertain 和依据消息 ID。模型不直接提供 hash，
避免不同模型输出格式抖动；真实 hash 由这里用稳定输入生成。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id, stable_json_hash
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.versions import TASK_BOUNDARY_TOOL_VERSION


class TaskBoundaryDecision(StrEnum):
    SAME = "same"
    NEW = "new"
    UNCERTAIN = "uncertain"


@dataclass(frozen=True, slots=True)
class TaskBoundaryObservation:
    """一次任务边界观察的结构化结果。

    `should_trigger_compaction` 只表示“应该请求压缩 pipeline 执行”。这里不直接写
    checkpoint，也不决定四层压缩走到哪一层。
    """

    decision: TaskBoundaryDecision
    basis_message_id: str
    candidate_hash: str | None
    confirmed_change: bool
    should_trigger_compaction: bool


class TaskBoundaryService:
    """把模型边界判断转成稳定 task hash 和压缩触发信号。"""

    def __init__(self, *, required_stable_count: int = 2) -> None:
        self.required_stable_count = required_stable_count

    def candidate_hash(self, *, session_id: str, basis_message_id: str) -> str:
        digest = stable_json_hash(
            {
                "basis_message_id": basis_message_id,
                "session_id": session_id,
                "version": TASK_BOUNDARY_TOOL_VERSION,
            },
            length=16,
        )
        return f"task_{digest}"

    def observe(
        self,
        state: SessionRuntimeState,
        *,
        decision: TaskBoundaryDecision | str,
        basis_message_id: str,
    ) -> TaskBoundaryObservation:
        normalized_decision = TaskBoundaryDecision(decision)
        if normalized_decision in {TaskBoundaryDecision.SAME, TaskBoundaryDecision.UNCERTAIN}:
            state.candidate_task_hash = None
            state.task_hash_stable_count = 0
            return TaskBoundaryObservation(
                decision=normalized_decision,
                basis_message_id=basis_message_id,
                candidate_hash=None,
                confirmed_change=False,
                should_trigger_compaction=False,
            )

        candidate_hash = self.candidate_hash(
            session_id=state.session_id,
            basis_message_id=basis_message_id,
        )
        confirmed_change = state.observe_task_hash_candidate(
            candidate_hash,
            required_stable_count=self.required_stable_count,
        )
        return TaskBoundaryObservation(
            decision=normalized_decision,
            basis_message_id=basis_message_id,
            candidate_hash=candidate_hash,
            confirmed_change=confirmed_change,
            should_trigger_compaction=confirmed_change,
        )

    def to_event(self, *, session_id: str, observation: TaskBoundaryObservation) -> SessionEvent:
        return SessionEvent(
            id=new_event_id(),
            session_id=session_id,
            type="task_boundary_observed",
            payload={
                "decision": observation.decision.value,
                "basis_message_id": observation.basis_message_id,
                "candidate_hash": observation.candidate_hash,
                "confirmed_change": observation.confirmed_change,
                "should_trigger_compaction": observation.should_trigger_compaction,
            },
        )
