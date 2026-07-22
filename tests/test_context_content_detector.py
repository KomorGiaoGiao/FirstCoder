from firstcoder.context.content.detector import is_old_task_part
from firstcoder.context.models import MessagePart


def _part(
    *,
    kind: str = "text",
    content: str = "content",
    task_hash: str = "task_current",
    created_turn: int = 1,
    compaction_state: str = "raw",
) -> MessagePart:
    return MessagePart(
        id="part_1",
        message_id="msg_1",
        kind=kind,
        content=content,
        metadata={
            "task_hash": task_hash,
            "created_turn": created_turn,
            "compaction_state": compaction_state,
        },
    )


def test_detects_old_task_part_but_skips_current_task() -> None:
    assert is_old_task_part(_part(task_hash="task_old"), active_task_hash="task_current") is True
    assert is_old_task_part(_part(task_hash="task_current"), active_task_hash="task_current") is False


def test_old_task_detector_skips_tool_call_and_tool_result_parts() -> None:
    assert (
        is_old_task_part(
            _part(kind="tool_call", task_hash="task_old"),
            active_task_hash="task_current",
        )
        is False
    )
    assert (
        is_old_task_part(
            _part(kind="tool_result", task_hash="task_old"),
            active_task_hash="task_current",
        )
        is False
    )
