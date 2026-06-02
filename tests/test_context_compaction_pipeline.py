from pathlib import Path

from firstcoder.context.compaction import CompactionPipeline, CompactionRequest
from firstcoder.context.models import AgentMessage, MessagePart, SessionView


def _message(
    message_id: str,
    *,
    role: str = "user",
    kind: str = "text",
    content: str = "content",
    task_hash: str = "task_current",
    created_turn: int = 10,
    metadata: dict[str, object] | None = None,
) -> AgentMessage:
    part_metadata = {
        "task_hash": task_hash,
        "created_turn": created_turn,
    }
    if metadata:
        part_metadata.update(metadata)
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
                metadata=part_metadata,
            )
        ],
    )


def test_l1_skips_current_task_content(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 80, task_hash="task_old"),
            _message("msg_current", content="当前任务内容" * 80, task_hash="task_current"),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
        )
    )

    old_part = result.view.messages[0].parts[0]
    current_part = result.view.messages[1].parts[0]
    assert old_part.metadata["compaction_state"] == "micro_compacted"
    assert old_part.metadata["compacted_by"] == "l1_old_task"
    assert current_part.content == "当前任务内容" * 80
    assert result.event.stopped_at in {"l1", "l2", "l3", "not_reached"}


def test_l2_archives_large_tool_result_and_skips_already_archived_part(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_tool_large",
                role="tool",
                kind="tool_result",
                content="large tool output\n" * 200,
                task_hash="task_current",
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
            _message(
                "msg_tool_archived",
                role="tool",
                kind="tool_result",
                content="[Tool result archived]\narchive_id=ar_existing",
                task_hash="task_current",
                metadata={
                    "tool_name": "shell",
                    "tool_call_id": "call_2",
                    "compaction_state": "archived",
                    "archive_id": "ar_existing",
                },
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path, large_tool_result_tokens=20).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
        )
    )

    large_part = result.view.messages[0].parts[0]
    archived_part = result.view.messages[1].parts[0]
    assert large_part.metadata["compaction_state"] == "archived"
    assert large_part.metadata["archive_id"]
    assert "archive_id=" in large_part.content
    assert archived_part.metadata["archive_id"] == "ar_existing"
    assert archived_part.content == "[Tool result archived]\narchive_id=ar_existing"


def test_l3_only_handles_current_task_cold_content(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_cold",
                content="当前任务冷信息" * 120,
                task_hash="task_current",
                created_turn=1,
            ),
            _message(
                "msg_hot",
                content="当前任务热信息" * 120,
                task_hash="task_current",
                created_turn=9,
            ),
            _message(
                "msg_other",
                content="其他任务内容" * 120,
                task_hash="task_other",
                created_turn=1,
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path, cold_turn_distance=5).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    assert result.view.messages[0].parts[0].metadata["compaction_state"] == "route_compacted"
    assert result.view.messages[1].parts[0].content == "当前任务热信息" * 120
    assert result.view.messages[2].parts[0].content == "其他任务内容" * 120


def test_pipeline_stops_after_budget_target_is_met(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 200, task_hash="task_old"),
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="large tool output\n" * 200,
                task_hash="task_current",
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1000,
            current_turn=10,
        )
    )

    assert result.event.stopped_at == "l1"
    assert result.event.levels_attempted == ["l1"]
    assert result.view.messages[1].parts[0].metadata.get("compaction_state") != "archived"


def test_pipeline_does_nothing_when_already_within_budget(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message("msg_old", content="旧任务内容" * 80, task_hash="task_old"),
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="large tool output\n" * 200,
                task_hash="task_current",
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path, large_tool_result_tokens=20).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=10_000,
            current_turn=10,
        )
    )

    assert result.event.noop is True
    assert result.event.levels_attempted == []
    assert result.event.stopped_at == "already_within_budget"
    assert result.view.messages[0].parts[0].content == "旧任务内容" * 80
    assert result.view.messages[1].parts[0].content == "large tool output\n" * 200
    assert not (tmp_path / ".firstcoder").exists()


def test_l1_does_not_compact_old_task_tool_call_or_tool_result_chain(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            AgentMessage(
                id="msg_assistant",
                session_id="sess_test",
                role="assistant",
                parts=[
                    MessagePart(
                        id="part_call",
                        message_id="msg_assistant",
                        kind="tool_call",
                        content="",
                        metadata={
                            "task_hash": "task_old",
                            "tool_call_id": "call_1",
                            "tool_name": "shell",
                            "arguments": {"command": "git status"},
                        },
                    )
                ],
            ),
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="旧任务工具结果" * 120,
                task_hash="task_old",
                metadata={"tool_name": "shell", "tool_call_id": "call_1"},
            ),
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l1",),
        )
    )

    assert result.view.messages[0].parts[0].kind == "tool_call"
    assert result.view.messages[0].parts[0].metadata["tool_call_id"] == "call_1"
    assert result.view.messages[1].parts[0].kind == "tool_result"
    assert result.view.messages[1].parts[0].content == "旧任务工具结果" * 120
    assert result.event.noop is True


def test_noop_compaction_is_recorded_and_deduped(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_current", content="short", task_hash="task_current")],
    )
    pipeline = CompactionPipeline(root=tmp_path)
    request = CompactionRequest(
        view=view,
        active_task_hash="task_current",
        target_tokens=1,
        current_turn=10,
    )

    first = pipeline.compact(request)
    second = pipeline.compact(request)

    assert first.event.noop is True
    assert first.event.input_fingerprint == second.event.input_fingerprint
    assert second.event.deduped is True


def test_pipeline_does_not_replace_part_when_compaction_would_increase_tokens(tmp_path: Path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_short_cold",
                content="短",
                task_hash="task_current",
                created_turn=1,
            )
        ],
    )

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash="task_current",
            target_tokens=1,
            current_turn=10,
            enabled_levels=("l3",),
        )
    )

    assert result.view.messages[0].parts[0].content == "短"
    assert result.event.noop is True
