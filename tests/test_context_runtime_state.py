from firstcoder.context.runtime_state import (
    SessionRuntimeState,
    active_auto_compact_disabled_until,
    auto_compact_circuit_is_open,
)


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


def test_auto_compact_circuit_open_only_while_disabled_until_is_active() -> None:
    active = SessionRuntimeState(
        session_id="sess_1",
        auto_compact_disabled_until="2999-06-01T00:00:00Z",
    )
    expired = SessionRuntimeState(
        session_id="sess_1",
        auto_compact_disabled_until="2000-06-01T00:00:00Z",
    )

    assert active_auto_compact_disabled_until(active) == "2999-06-01T00:00:00Z"
    assert auto_compact_circuit_is_open(active) is True
    assert active_auto_compact_disabled_until(expired) is None
    assert auto_compact_circuit_is_open(expired) is False
    assert expired.auto_compact_disabled_until is None
