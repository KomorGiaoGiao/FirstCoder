from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.inspector import ContextInspector
from firstcoder.context.models import AgentMessage, MessagePart, SessionView
from firstcoder.context.runtime_state import CompactionHistoryEntry, SessionRuntimeState
from firstcoder.context.token_budget import ContextBudget, build_context_budget


def _budget() -> ContextBudget:
    return build_context_budget(
        messages=[],
        tools=[],
        context_window=32_768,
        max_output_tokens=4_096,
    )


def _message(
    message_id: str,
    *,
    role: str = "user",
    kind: str = "text",
    content: str = "content",
    metadata: dict[str, object] | None = None,
) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role=role,
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind=kind,
                content=content,
                metadata=dict(metadata or {}),
            )
        ],
    )


def test_inspector_reports_runtime_and_context_fields() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1", content="旧消息"),
            _message("msg_2", content="最近消息"),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_latest",
                session_id="sess_test",
                summary="checkpoint 摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
                source_fingerprint="source_1",
                sequence=1,
            )
        ],
    )
    runtime = SessionRuntimeState(
        session_id="sess_test",
        active_task_hash="task_active",
        candidate_task_hash="task_candidate",
        system_prompt_fingerprint="sys_fp",
        last_compaction_input_fingerprint="compact_fp",
    )

    report = ContextInspector().inspect(view, runtime, budget=_budget())

    assert report.session_id == "sess_test"
    assert report.active_task_hash == "task_active"
    assert report.candidate_task_hash == "task_candidate"
    assert report.system_prompt_fingerprint == "sys_fp"
    assert report.latest_checkpoint_id == "ckpt_latest"
    assert report.tail_message_count == 1
    assert report.input_tokens == 0
    assert report.archive_count == 0
    assert report.last_compaction_input_fingerprint == "compact_fp"


def test_inspector_counts_archived_parts_from_metadata() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_archived_by_state",
                role="tool",
                kind="tool_result",
                content="[archived]",
                metadata={"compaction_state": "archived"},
            ),
            _message(
                "msg_archived_by_id",
                role="tool",
                kind="tool_result",
                content="[archived]",
                metadata={"archive_id": "ar_1"},
            ),
            _message("msg_normal", content="普通消息"),
        ],
    )

    report = ContextInspector().inspect(view, SessionRuntimeState(session_id="sess_test"), budget=_budget())

    assert report.archive_count == 2


def test_inspector_uses_latest_checkpoint_sequence_for_tail_count() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_1"),
            _message("msg_2"),
            _message("msg_3"),
        ],
        checkpoints=[
            Checkpoint(
                id="ckpt_old",
                session_id="sess_test",
                summary="旧摘要",
                tail_start_message_id="msg_2",
                covered_until_message_id="msg_1",
                source_fingerprint="source_old",
                created_at="2026-06-01T00:00:00Z",
                sequence=1,
            ),
            Checkpoint(
                id="ckpt_new",
                session_id="sess_test",
                summary="新摘要",
                tail_start_message_id="msg_3",
                covered_until_message_id="msg_2",
                source_fingerprint="source_new",
                created_at="2026-06-01T00:00:00Z",
                sequence=2,
            ),
        ],
    )

    report = ContextInspector().inspect(view, SessionRuntimeState(session_id="sess_test"), budget=_budget())

    assert report.latest_checkpoint_id == "ckpt_new"
    assert report.tail_message_count == 1


def test_inspector_reports_auto_compact_circuit_breaker_status() -> None:
    runtime = SessionRuntimeState(
        session_id="sess_test",
        auto_compact_disabled_until="2999-06-01T00:30:00Z",
        last_auto_compact_failure_reason="timeout",
    )

    report = ContextInspector().inspect(SessionView(session_id="sess_test"), runtime, budget=_budget())

    assert report.auto_compact_disabled_until == "2999-06-01T00:30:00Z"
    assert report.last_failure_reason == "timeout"
    assert report.auto_compact_status == "disabled"


def test_inspector_does_not_report_expired_circuit_breaker_as_disabled() -> None:
    runtime = SessionRuntimeState(
        session_id="sess_test",
        auto_compact_disabled_until="2000-06-01T00:30:00Z",
        last_auto_compact_failure_reason="timeout",
    )

    report = ContextInspector().inspect(SessionView(session_id="sess_test"), runtime, budget=_budget())

    assert report.auto_compact_disabled_until is None
    assert report.auto_compact_status == "failed"


def test_inspector_reports_missing_checkpoint_tail_boundary() -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_1"), _message("msg_2")],
        checkpoints=[
            Checkpoint(
                id="ckpt_bad",
                session_id="sess_test",
                summary="损坏 checkpoint",
                tail_start_message_id="msg_missing",
                covered_until_message_id="msg_1",
                source_fingerprint="source_bad",
                sequence=1,
            )
        ],
    )

    report = ContextInspector().inspect(view, SessionRuntimeState(session_id="sess_test"), budget=_budget())

    assert report.latest_checkpoint_id == "ckpt_bad"
    assert report.checkpoint_boundary_status == "missing_tail"


def test_inspector_report_can_be_serialized_for_tui_status() -> None:
    runtime = SessionRuntimeState(session_id="sess_test")
    runtime.record_compaction_event(
        CompactionHistoryEntry(
            event_type="compaction_completed",
            trigger="auto",
            target_tokens=100,
            input_fingerprint="fp_1",
            status="success",
            reason="l1",
            before_tokens=500,
            after_tokens=100,
            checkpoint_id=None,
            created_at="2026-06-02T00:00:00Z",
        )
    )
    report = ContextInspector().inspect(
        SessionView(session_id="sess_test"),
        runtime,
        budget=_budget(),
    )

    assert report.to_dict()["session_id"] == "sess_test"
    assert report.to_dict()["recent_compaction_events"][0]["input_fingerprint"] == "fp_1"
    assert set(report.to_dict()) >= {
        "session_id",
        "active_task_hash",
        "candidate_task_hash",
        "system_prompt_fingerprint",
        "latest_checkpoint_id",
        "tail_message_count",
        "input_tokens",
        "archive_count",
        "last_compaction_input_fingerprint",
        "auto_compact_disabled_until",
        "last_failure_reason",
        "auto_compact_status",
        "checkpoint_boundary_status",
        "recent_compaction_events",
    }


def test_inspector_reports_shared_budget_and_unconsumed_count() -> None:
    runtime = SessionRuntimeState(
        session_id="sess_test",
        consumed_tool_result_part_ids={"part_consumed"},
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="result",
                metadata={"tool_call_id": "call_1"},
            )
        ],
    )
    budget = ContextBudget(
        context_window=128_000,
        output_reserve=8_192,
        input_capacity=113_408,
        fixed_tokens=18_000,
        history_tokens=42_000,
        input_tokens=60_000,
        high_watermark=102_067,
        low_watermark=81_653,
        source="configured",
    )

    report = ContextInspector().inspect(view, runtime, budget=budget)

    assert report.context_window == 128_000
    assert report.fixed_tokens == 18_000
    assert report.history_tokens == 42_000
    assert report.input_tokens == 60_000
    assert report.unconsumed_tool_result_count == 1
