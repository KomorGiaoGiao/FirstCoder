from dataclasses import asdict

from firstcoder.context.compaction import CompactionEvent
from firstcoder.context.events import SessionEvent
from firstcoder.context.llm_compact import LlmCompactEvent
from firstcoder.context.runtime_replay import replay_runtime_state
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.task_boundary import TaskBoundaryService


def test_replay_restores_active_task_hash_from_confirmed_task_boundary(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    service = TaskBoundaryService()
    candidate = service.candidate_hash(session_id="sess_test", basis_message_id="msg_new")
    store.append_event(
        SessionEvent(
            id="evt_task",
            session_id="sess_test",
            type="task_boundary_observed",
            payload={
                "decision": "new",
                "basis_message_id": "msg_new",
                "candidate_hash": candidate,
                "confirmed_change": True,
                "should_trigger_compaction": True,
            },
        )
    )

    state = replay_runtime_state(store, "sess_test")

    assert state.active_task_hash == candidate
    assert state.candidate_task_hash is None
    assert state.task_hash_stable_count == 0


def test_replay_restores_candidate_hash_window(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    service = TaskBoundaryService()
    candidate = service.candidate_hash(session_id="sess_test", basis_message_id="msg_new")
    store.append_event(
        SessionEvent(
            id="evt_task",
            session_id="sess_test",
            type="task_boundary_observed",
            payload={
                "decision": "new",
                "basis_message_id": "msg_new",
                "candidate_hash": candidate,
                "confirmed_change": False,
                "should_trigger_compaction": False,
                "stable_count": 1,
            },
        )
    )

    state = replay_runtime_state(store, "sess_test")

    assert state.active_task_hash is None
    assert state.candidate_task_hash == candidate
    assert state.task_hash_stable_count == 1


def test_task_boundary_event_replays_active_hash_and_stable_window(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    service = TaskBoundaryService()
    candidate = service.candidate_hash(session_id="sess_test", basis_message_id="msg_new")
    store.append_event(
        SessionEvent(
            id="evt_pending",
            session_id="sess_test",
            type="task_boundary_observed",
            payload={
                "decision": "new",
                "basis_message_id": "msg_new",
                "candidate_hash": candidate,
                "active_task_hash": None,
                "confirmed_change": False,
                "should_trigger_compaction": False,
                "triggered_compaction": False,
                "stable_count": 1,
                "confirmation_reason": "stable_window_pending",
            },
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_confirmed",
            session_id="sess_test",
            type="task_boundary_observed",
            payload={
                "decision": "new",
                "basis_message_id": "msg_new",
                "candidate_hash": candidate,
                "active_task_hash": candidate,
                "confirmed_change": True,
                "should_trigger_compaction": True,
                "triggered_compaction": True,
                "stable_count": 0,
                "confirmation_reason": "stable_window",
            },
        )
    )

    state = replay_runtime_state(store, "sess_test")

    assert state.active_task_hash == candidate
    assert state.candidate_task_hash is None
    assert state.task_hash_stable_count == 0


def test_replay_restores_latest_checkpoint_id(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_ckpt",
            session_id="sess_test",
            type="checkpoint_created",
            payload={
                "id": "ckpt_1",
                "session_id": "sess_test",
                "summary": "摘要",
                "tail_start_message_id": "msg_2",
                "covered_until_message_id": "msg_1",
                "source_fingerprint": "fp_ckpt",
            },
        )
    )

    state = replay_runtime_state(store, "sess_test")

    assert state.latest_checkpoint_id == "ckpt_1"
    assert state.last_compaction_input_fingerprint == "fp_ckpt"


def test_replay_restores_auto_compact_failure_state(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_l4_failed",
            session_id="sess_test",
            type="llm_compaction_completed",
            payload={
                "trigger": "auto",
                "target_tokens": 100,
                "event": {
                    "status": "failed",
                    "source_fingerprint": "fp_l4",
                    "retry_count": 1,
                    "failure_reason": "no_summary",
                    "checkpoint_id": None,
                },
            },
        )
    )

    state = replay_runtime_state(store, "sess_test")

    assert state.auto_compact_failure_count == 1
    assert state.last_auto_compact_failure_reason == "no_summary"


def test_replay_restores_manager_level_auto_compact_failure_state(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_l4_missing",
            session_id="sess_test",
            type="llm_compaction_completed",
            payload={
                "trigger": "auto",
                "target_tokens": 100,
                "status": "failed",
                "reason": "l4_service_missing",
                "input_fingerprint": "fp_programmatic",
                "event": {
                    "status": "failed",
                    "source_fingerprint": "fp_programmatic",
                    "retry_count": 0,
                    "failure_reason": "l4_service_missing",
                    "checkpoint_id": None,
                },
            },
        )
    )

    state = replay_runtime_state(store, "sess_test")

    assert state.auto_compact_failure_count == 1
    assert state.last_auto_compact_failure_reason == "l4_service_missing"


def test_replay_records_compaction_input_fingerprint(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    event = CompactionEvent(
        input_fingerprint="fp_programmatic",
        before_tokens=500,
        after_tokens=100,
        levels_attempted=["l1"],
        stopped_at="l1",
        changed_parts=1,
    )
    store.append_event(
        SessionEvent(
            id="evt_compact",
            session_id="sess_test",
            type="compaction_completed",
            payload={
                "trigger": "auto",
                "target_tokens": 100,
                "event": asdict(event),
            },
        )
    )

    state = replay_runtime_state(store, "sess_test")

    assert state.last_compaction_input_fingerprint == "fp_programmatic"


def test_replay_exposes_recent_compaction_events_for_inspector(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    event = CompactionEvent(
        input_fingerprint="fp_programmatic",
        before_tokens=500,
        after_tokens=100,
        levels_attempted=["l1"],
        stopped_at="l1",
        changed_parts=1,
        reason="l1",
        target_tokens=100,
        source_part_ids=["part_old"],
        output_part_ids=["part_old"],
    )
    store.append_event(
        SessionEvent(
            id="evt_compact",
            session_id="sess_test",
            type="compaction_completed",
            payload={
                "event_version": "v1",
                "trigger": "auto",
                "target_tokens": 100,
                "created_at": event.created_at,
                "input_fingerprint": event.input_fingerprint,
                "status": "success",
                "reason": event.reason,
                "before_tokens": event.before_tokens,
                "after_tokens": event.after_tokens,
                "checkpoint_id": None,
                "event": asdict(event),
            },
        )
    )

    state = replay_runtime_state(store, "sess_test")

    assert len(state.recent_compaction_events) == 1
    recent = state.recent_compaction_events[0]
    assert recent.event_type == "compaction_completed"
    assert recent.trigger == "auto"
    assert recent.input_fingerprint == "fp_programmatic"
    assert recent.status == "success"
    assert recent.reason == "l1"
    assert recent.before_tokens == 500
    assert recent.after_tokens == 100
