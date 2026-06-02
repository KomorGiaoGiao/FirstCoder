import pytest

from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.task_boundary import (
    TaskBoundaryPolicy,
    TaskBoundaryDecision,
    TaskBoundaryObservation,
    TaskBoundaryService,
)


def test_same_keeps_active_task_hash() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")

    observation = TaskBoundaryService().observe(
        state,
        decision=TaskBoundaryDecision.SAME,
        basis_message_id="msg_1",
    )

    assert observation.decision == TaskBoundaryDecision.SAME
    assert observation.basis_message_id == "msg_1"
    assert observation.candidate_hash is None
    assert observation.confirmed_change is False
    assert observation.should_trigger_compaction is False
    assert observation.active_task_hash == "task_active"
    assert state.active_task_hash == "task_active"
    assert state.candidate_task_hash is None


def test_uncertain_keeps_active_task_hash() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")

    observation = TaskBoundaryService().observe(
        state,
        decision=TaskBoundaryDecision.UNCERTAIN,
        basis_message_id="msg_1",
    )

    assert observation.confirmed_change is False
    assert observation.should_trigger_compaction is False
    assert observation.candidate_hash is None
    assert state.active_task_hash == "task_active"


def test_same_resets_pending_new_candidate_window() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    service = TaskBoundaryService(required_stable_count=2)

    first = service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_new",
    )
    service.observe(
        state,
        decision=TaskBoundaryDecision.SAME,
        basis_message_id="msg_same",
    )
    second = service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_new",
    )

    assert first.confirmed_change is False
    assert second.confirmed_change is False
    assert second.should_trigger_compaction is False
    assert state.candidate_task_hash == second.candidate_hash
    assert state.task_hash_stable_count == 1


def test_uncertain_resets_pending_new_candidate_window() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    service = TaskBoundaryService(required_stable_count=2)

    service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_new",
    )
    service.observe(
        state,
        decision=TaskBoundaryDecision.UNCERTAIN,
        basis_message_id="msg_uncertain",
    )
    observation = service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_new",
    )

    assert observation.confirmed_change is False
    assert observation.should_trigger_compaction is False
    assert state.candidate_task_hash == observation.candidate_hash
    assert state.task_hash_stable_count == 1


def test_new_requires_stable_window() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    service = TaskBoundaryService(required_stable_count=2)

    first = service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_new",
    )
    second = service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_new",
    )

    assert first.confirmed_change is False
    assert first.should_trigger_compaction is False
    assert first.candidate_hash == second.candidate_hash
    assert second.confirmed_change is True
    assert second.should_trigger_compaction is True
    assert state.active_task_hash == second.candidate_hash


def test_new_candidate_hash_is_program_generated_and_stable() -> None:
    service = TaskBoundaryService()

    first = service.candidate_hash(session_id="sess_test", basis_message_id="msg_new")
    second = service.candidate_hash(session_id="sess_test", basis_message_id="msg_new")
    different = service.candidate_hash(session_id="sess_test", basis_message_id="msg_other")

    assert first.startswith("task_")
    assert first == second
    assert first != different


def test_task_hash_event_records_candidate_and_confirmation() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    service = TaskBoundaryService(required_stable_count=1)

    observation = service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_new",
    )
    event = service.to_event(session_id="sess_test", observation=observation)

    assert event.type == "task_boundary_observed"
    assert event.payload["decision"] == "new"
    assert event.payload["basis_message_id"] == "msg_new"
    assert event.payload["candidate_hash"] == observation.candidate_hash
    assert event.payload["confirmed_change"] is True
    assert event.payload["should_trigger_compaction"] is True
    assert event.payload["stable_count"] == 0


def test_task_hash_event_records_pending_stable_count() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    service = TaskBoundaryService(required_stable_count=3)

    observation = service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_new",
    )
    event = service.to_event(session_id="sess_test", observation=observation)

    assert observation.confirmed_change is False
    assert event.payload["stable_count"] == 1


def test_task_boundary_rejects_unknown_basis_message_id() -> None:
    state = SessionRuntimeState(session_id="sess_test")
    service = TaskBoundaryService(known_message_ids={"msg_known"})

    with pytest.raises(ValueError, match="basis_message_id 不属于当前 session"):
        service.observe(state, decision=TaskBoundaryDecision.NEW, basis_message_id="msg_missing")


def test_explicit_topic_change_can_use_single_observation_policy() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    service = TaskBoundaryService(
        required_stable_count=3,
        policy=TaskBoundaryPolicy(single_observation_basis_message_ids={"msg_explicit"}),
    )

    observation = service.observe(
        state,
        decision=TaskBoundaryDecision.NEW,
        basis_message_id="msg_explicit",
    )

    assert observation.confirmed_change is True
    assert observation.should_trigger_compaction is True
    assert observation.stable_count == 0
    assert state.active_task_hash == observation.candidate_hash


def test_task_hash_event_records_active_hash_and_trigger_reason() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    service = TaskBoundaryService(required_stable_count=1)

    observation = service.observe(state, decision=TaskBoundaryDecision.NEW, basis_message_id="msg_new")
    event = service.to_event(session_id="sess_test", observation=observation)

    assert event.payload["active_task_hash"] == observation.candidate_hash
    assert event.payload["triggered_compaction"] is True
    assert event.payload["confirmation_reason"] == "stable_window"


def test_task_hash_event_records_stable_window_state() -> None:
    state = SessionRuntimeState(session_id="sess_test", active_task_hash="task_active")
    service = TaskBoundaryService(required_stable_count=3)

    observation = service.observe(state, decision=TaskBoundaryDecision.NEW, basis_message_id="msg_new")
    event = service.to_event(session_id="sess_test", observation=observation)

    assert event.payload["event_version"] == "v1"
    assert event.payload["strategy_version"] == "v1"
    assert event.payload["active_hash"] == "task_active"
    assert event.payload["active_task_hash"] == "task_active"
    assert event.payload["candidate_hash"] == observation.candidate_hash
    assert event.payload["stable_count"] == 1
    assert event.payload["required_stable_count"] == 3
    assert event.payload["triggered_compaction"] is False
    assert event.payload["confirmation_reason"] == "stable_window_pending"
    assert event.payload["created_at"].endswith("Z")
