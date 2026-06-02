from firstcoder.context.runtime_state import SessionRuntimeState
from firstcoder.context.task_boundary import (
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

    assert observation == TaskBoundaryObservation(
        decision=TaskBoundaryDecision.SAME,
        basis_message_id="msg_1",
        candidate_hash=None,
        confirmed_change=False,
        should_trigger_compaction=False,
    )
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
    assert event.payload == {
        "decision": "new",
        "basis_message_id": "msg_new",
        "candidate_hash": observation.candidate_hash,
        "confirmed_change": True,
        "should_trigger_compaction": True,
    }
