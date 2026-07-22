"""从 session JSONL 事件恢复运行期状态。"""

from __future__ import annotations

from firstcoder.utils.text import optional_str

from firstcoder.context.events import SessionEvent
from firstcoder.context.runtime_state import CompactionHistoryEntry, SessionRuntimeState
from firstcoder.context.store import JsonlSessionStore


def replay_runtime_state(store: JsonlSessionStore, session_id: str) -> SessionRuntimeState:
    """重放不属于自然语言历史的 session runtime state。

    `JsonlSessionStore.rebuild_session_view()` 负责消息和 checkpoint 投影；这里只恢复
    active task hash、compact failure、latest checkpoint 等运行期事实。
    """

    state = SessionRuntimeState(session_id=session_id)
    for event in store.list_events(session_id):
        _apply_event(state, event)
    return state


def _apply_event(state: SessionRuntimeState, event: SessionEvent) -> None:
    if event.type == "task_boundary_observed":
        _apply_task_boundary(state, event)
        return

    if event.type == "checkpoint_created":
        state.latest_checkpoint_id = str(event.payload.get("id") or "")
        source_fingerprint = event.payload.get("source_fingerprint")
        if source_fingerprint:
            state.last_compaction_input_fingerprint = str(source_fingerprint)
        return

    if event.type == "compaction_completed":
        compaction_event = _event_payload(event)
        input_fingerprint = compaction_event.get("input_fingerprint")
        if input_fingerprint:
            state.last_compaction_input_fingerprint = str(input_fingerprint)
        state.record_compaction_event(_compaction_history_entry(event, compaction_event))
        return

    if event.type == "compaction_skipped":
        if event.payload.get("reason") == "skipped_no_effect":
            state.last_no_effect_compaction_fingerprint = optional_str(event.payload.get("input_fingerprint"))
        return

    if event.type == "llm_compaction_completed":
        _apply_l4_compaction(state, event)


def _apply_task_boundary(state: SessionRuntimeState, event: SessionEvent) -> None:
    payload = event.payload
    candidate_hash = payload.get("candidate_hash")
    if payload.get("confirmed_change") and candidate_hash:
        state.active_task_hash = str(candidate_hash)
        state.candidate_task_hash = None
        state.task_hash_stable_count = 0
        return

    decision = str(payload.get("decision") or "")
    if decision in {"same", "uncertain"}:
        state.candidate_task_hash = None
        state.task_hash_stable_count = 0
        return

    if decision == "new" and candidate_hash:
        state.candidate_task_hash = str(candidate_hash)
        state.task_hash_stable_count = int(payload.get("stable_count") or 1)


def _apply_l4_compaction(state: SessionRuntimeState, event: SessionEvent) -> None:
    payload = event.payload
    l4_event = _event_payload(event)
    source_fingerprint = l4_event.get("source_fingerprint")
    if source_fingerprint:
        state.last_compaction_input_fingerprint = str(source_fingerprint)
    state.record_compaction_event(_compaction_history_entry(event, l4_event))

    if l4_event.get("status") == "success":
        checkpoint_id = l4_event.get("checkpoint_id")
        if checkpoint_id:
            state.latest_checkpoint_id = str(checkpoint_id)
        state.record_auto_compact_success()
        return

    if payload.get("trigger") == "auto" and l4_event.get("status") == "failed":
        state.record_auto_compact_failure(str(l4_event.get("failure_reason") or "unknown"))


def _event_payload(event: SessionEvent) -> dict[str, object]:
    nested = event.payload.get("event")
    return dict(nested) if isinstance(nested, dict) else {}


def _compaction_history_entry(event: SessionEvent, nested_event: dict[str, object]) -> CompactionHistoryEntry:
    payload = event.payload
    input_fingerprint = payload.get("input_fingerprint") or nested_event.get("input_fingerprint")
    if input_fingerprint is None:
        input_fingerprint = nested_event.get("source_fingerprint")

    return CompactionHistoryEntry(
        event_type=event.type,
        trigger=str(payload.get("trigger") or ""),
        target_tokens=_optional_int(payload.get("target_tokens")),
        input_fingerprint=optional_str(input_fingerprint),
        status=str(payload.get("status") or nested_event.get("status") or _status_from_compaction(nested_event)),
        reason=optional_str(payload.get("reason") or nested_event.get("reason") or nested_event.get("failure_reason")),
        before_tokens=_optional_int(payload.get("before_tokens") or nested_event.get("before_tokens")),
        after_tokens=_optional_int(payload.get("after_tokens") or nested_event.get("after_tokens")),
        checkpoint_id=optional_str(payload.get("checkpoint_id") or nested_event.get("checkpoint_id")),
        created_at=optional_str(payload.get("created_at") or nested_event.get("created_at")),
    )


def _status_from_compaction(compaction_event: dict[str, object]) -> str:
    if "success" in compaction_event:
        return "success" if compaction_event.get("success") else "failed"
    return "success"


def _optional_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(value)
