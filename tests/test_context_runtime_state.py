from firstcoder.context.runtime_state import SessionRuntimeState


def test_runtime_state_tracks_task_hash_stability() -> None:
    state = SessionRuntimeState(session_id="sess_1", active_task_hash="task_a")

    assert state.observe_task_hash_candidate("task_b") is False
    assert state.candidate_task_hash == "task_b"
    assert state.task_hash_stable_count == 1

    assert state.observe_task_hash_candidate("task_b", required_stable_count=2) is True
    assert state.active_task_hash == "task_b"
    assert state.candidate_task_hash is None
    assert state.task_hash_stable_count == 0


def test_runtime_state_records_compact_failure_and_circuit_breaker() -> None:
    state = SessionRuntimeState(session_id="sess_1")

    assert state.record_auto_compact_failure("timeout") is False
    assert state.record_auto_compact_failure("timeout") is False
    assert state.record_auto_compact_failure("timeout") is True
    assert state.auto_compact_failure_count == 3
    assert state.last_auto_compact_failure_reason == "timeout"
    assert state.auto_compact_disabled_until is not None

    state.record_auto_compact_success()

    assert state.auto_compact_failure_count == 0
    assert state.auto_compact_disabled_until is None
