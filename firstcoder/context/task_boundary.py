"""任务边界观察与程序生成 task hash。

这一层接收模型的极简判断：same/new/uncertain 和依据消息 ID。模型不直接提供 hash，
避免不同模型输出格式抖动；真实 hash 由这里用稳定输入生成。
"""

from __future__ import annotations

from firstcoder.utils.text import optional_str

from dataclasses import dataclass
from enum import StrEnum
from typing import Collection

from firstcoder.context.events import SessionEvent
from firstcoder.context.identity import new_event_id, stable_json_hash
from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.models import utc_now_iso
from firstcoder.context.versions import CONTEXT_EVENT_SCHEMA_VERSION, TASK_BOUNDARY_TOOL_VERSION


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
    stable_count: int = 0
    active_task_hash: str | None = None
    candidate_basis_message_id: str | None = None
    triggered_compaction: bool = False
    confirmation_reason: str = "not_confirmed"
    required_stable_count: int = 2
    event_version: str = CONTEXT_EVENT_SCHEMA_VERSION
    strategy_version: str = TASK_BOUNDARY_TOOL_VERSION
    created_at: str = ""


@dataclass(frozen=True, slots=True)
class TaskBoundaryPolicy:
    """程序侧任务边界策略。

    模型仍只提交 `decision` 和 `basis_message_id`。是否允许单次确认、哪些消息 ID
    合法，都由 agent/session 侧传入 policy 控制，避免把激进程度交给模型自由决定。
    """

    single_observation_basis_message_ids: Collection[str] = ()


class TaskBoundaryService:
    """把模型边界判断转成稳定 task hash 和压缩触发信号。"""

    def __init__(
        self,
        *,
        required_stable_count: int = 2,
        known_message_ids: Collection[str] | None = None,
        policy: TaskBoundaryPolicy | None = None,
    ) -> None:
        self.required_stable_count = required_stable_count
        self.known_message_ids = known_message_ids
        self.policy = policy or TaskBoundaryPolicy()

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
        self._validate_basis_message_id(basis_message_id)
        normalized_decision = TaskBoundaryDecision(decision)
        if normalized_decision == TaskBoundaryDecision.SAME and state.candidate_task_hash is not None:
            candidate_hash = state.candidate_task_hash
            candidate_basis_message_id = state.candidate_task_basis_message_id
            confirmed_change = state.observe_task_hash_candidate(
                candidate_hash,
                required_stable_count=self._required_stable_count_for(basis_message_id),
            )
            return TaskBoundaryObservation(
                decision=normalized_decision,
                basis_message_id=basis_message_id,
                candidate_hash=candidate_hash,
                candidate_basis_message_id=candidate_basis_message_id,
                confirmed_change=confirmed_change,
                should_trigger_compaction=confirmed_change,
                stable_count=state.task_hash_stable_count,
                active_task_hash=state.active_task_hash,
                triggered_compaction=confirmed_change,
                confirmation_reason=_confirmation_reason(
                    confirmed_change,
                    single_observation_policy=False,
                ),
                required_stable_count=self.required_stable_count,
                created_at=utc_now_iso(),
            )

        if normalized_decision in {TaskBoundaryDecision.SAME, TaskBoundaryDecision.UNCERTAIN}:
            state.candidate_task_hash = None
            state.candidate_task_basis_message_id = None
            state.task_hash_stable_count = 0
            return TaskBoundaryObservation(
                decision=normalized_decision,
                basis_message_id=basis_message_id,
                candidate_hash=None,
                candidate_basis_message_id=None,
                confirmed_change=False,
                should_trigger_compaction=False,
                stable_count=0,
                active_task_hash=state.active_task_hash,
                triggered_compaction=False,
                confirmation_reason="reset_candidate",
                required_stable_count=self.required_stable_count,
                created_at=utc_now_iso(),
            )

        candidate_hash = self.candidate_hash(
            session_id=state.session_id,
            basis_message_id=basis_message_id,
        )
        if state.active_task_hash is None and state.candidate_task_hash is None:
            state.active_task_hash = candidate_hash
            state.candidate_task_basis_message_id = None
            state.task_hash_stable_count = 0
            return TaskBoundaryObservation(
                decision=normalized_decision,
                basis_message_id=basis_message_id,
                candidate_hash=candidate_hash,
                candidate_basis_message_id=basis_message_id,
                confirmed_change=True,
                should_trigger_compaction=False,
                stable_count=0,
                active_task_hash=state.active_task_hash,
                triggered_compaction=False,
                confirmation_reason="initial_task",
                required_stable_count=self.required_stable_count,
                created_at=utc_now_iso(),
            )
        required_stable_count = self._required_stable_count_for(basis_message_id)
        single_observation_policy = self._uses_single_observation_policy(basis_message_id)
        previous_candidate_hash = state.candidate_task_hash
        confirmed_change = state.observe_task_hash_candidate(
            candidate_hash,
            required_stable_count=required_stable_count,
        )
        if not confirmed_change and candidate_hash != previous_candidate_hash:
            state.candidate_task_basis_message_id = basis_message_id
        candidate_basis_message_id = basis_message_id if confirmed_change else state.candidate_task_basis_message_id
        return TaskBoundaryObservation(
            decision=normalized_decision,
            basis_message_id=basis_message_id,
            candidate_hash=candidate_hash,
            candidate_basis_message_id=candidate_basis_message_id,
            confirmed_change=confirmed_change,
            should_trigger_compaction=confirmed_change,
            stable_count=state.task_hash_stable_count,
            active_task_hash=state.active_task_hash,
            triggered_compaction=confirmed_change,
            confirmation_reason=_confirmation_reason(
                confirmed_change,
                single_observation_policy=single_observation_policy,
            ),
            required_stable_count=required_stable_count,
            created_at=utc_now_iso(),
        )

    def to_event(self, *, session_id: str, observation: TaskBoundaryObservation) -> SessionEvent:
        return SessionEvent(
            id=new_event_id(),
            session_id=session_id,
            type="task_boundary_observed",
            payload={
                "event_version": observation.event_version,
                "strategy_version": observation.strategy_version,
                "created_at": observation.created_at or utc_now_iso(),
                "decision": observation.decision.value,
                "basis_message_id": observation.basis_message_id,
                "candidate_hash": observation.candidate_hash,
                "candidate_basis_message_id": observation.candidate_basis_message_id,
                "active_hash": observation.active_task_hash,
                "active_task_hash": observation.active_task_hash,
                "confirmed_change": observation.confirmed_change,
                "should_trigger_compaction": observation.should_trigger_compaction,
                "triggered_compaction": observation.triggered_compaction,
                "stable_count": observation.stable_count,
                "required_stable_count": observation.required_stable_count,
                "confirmation_reason": observation.confirmation_reason,
            },
        )

    def _validate_basis_message_id(self, basis_message_id: str) -> None:
        if self.known_message_ids is None:
            return
        if basis_message_id not in self.known_message_ids:
            raise ValueError("basis_message_id 不属于当前 session")

    def _required_stable_count_for(self, basis_message_id: str) -> int:
        if self._uses_single_observation_policy(basis_message_id):
            return 1
        return self.required_stable_count

    def _uses_single_observation_policy(self, basis_message_id: str) -> bool:
        return basis_message_id in set(self.policy.single_observation_basis_message_ids)

    def initialize_active_task(
        self,
        state: SessionRuntimeState,
        *,
        basis_message_id: str,
        confirmation_reason: str = "implicit_initial_task",
    ) -> TaskBoundaryObservation | None:
        """Initialize the first active task without relying on model tool use."""

        if state.active_task_hash is not None:
            return None
        self._validate_basis_message_id(basis_message_id)
        candidate_hash = self.candidate_hash(session_id=state.session_id, basis_message_id=basis_message_id)
        state.active_task_hash = candidate_hash
        state.candidate_task_hash = None
        state.candidate_task_basis_message_id = None
        state.task_hash_stable_count = 0
        return TaskBoundaryObservation(
            decision=TaskBoundaryDecision.NEW,
            basis_message_id=basis_message_id,
            candidate_hash=candidate_hash,
            candidate_basis_message_id=basis_message_id,
            confirmed_change=True,
            should_trigger_compaction=False,
            stable_count=0,
            active_task_hash=state.active_task_hash,
            triggered_compaction=False,
            confirmation_reason=confirmation_reason,
            required_stable_count=self.required_stable_count,
            created_at=utc_now_iso(),
        )


def _confirmation_reason(confirmed_change: bool, *, single_observation_policy: bool) -> str:
    if confirmed_change and single_observation_policy:
        return "single_observation_policy"
    if confirmed_change:
        return "stable_window"
    return "stable_window_pending"


def observation_from_tool_result_data(data: dict[str, object]) -> TaskBoundaryObservation | None:
    """从 task_boundary 工具结果 data 还原可持久化 observation。"""

    decision = data.get("decision")
    basis_message_id = data.get("basis_message_id")
    if not decision or not basis_message_id:
        return None

    try:
        normalized_decision = TaskBoundaryDecision(str(decision))
    except ValueError:
        return None

    return TaskBoundaryObservation(
        decision=normalized_decision,
        basis_message_id=str(basis_message_id),
        candidate_hash=optional_str(data.get("candidate_hash")),
        candidate_basis_message_id=optional_str(data.get("candidate_basis_message_id")),
        active_task_hash=optional_str(data.get("active_task_hash")),
        confirmed_change=bool(data.get("confirmed_change")),
        should_trigger_compaction=bool(data.get("should_trigger_compaction")),
        triggered_compaction=bool(data.get("triggered_compaction")),
        stable_count=int(data.get("stable_count") or 0),
        confirmation_reason=str(data.get("confirmation_reason") or "not_confirmed"),
        required_stable_count=int(data.get("required_stable_count") or 2),
        event_version=str(data.get("event_version") or CONTEXT_EVENT_SCHEMA_VERSION),
        strategy_version=str(data.get("strategy_version") or TASK_BOUNDARY_TOOL_VERSION),
        created_at=str(data.get("created_at") or ""),
    )
