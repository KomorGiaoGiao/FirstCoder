from firstcoder.context.content.compressors import compact_cold_text_part, compact_old_task_part
from firstcoder.context.models import MessagePart


def test_old_task_compressor_marks_part_as_trimmed() -> None:
    part = MessagePart(
        id="part_1",
        message_id="msg_1",
        kind="text",
        content="旧任务内容" * 100,
        metadata={"task_hash": "task_old"},
    )

    compacted = compact_old_task_part(part)

    assert compacted.metadata["compaction_state"] == "trimmed"
    assert compacted.metadata["compacted_by"] == "l1_old_task_dialogue"
    assert compacted.content == ""


def test_cold_text_compressor_marks_part_as_route_compacted() -> None:
    part = MessagePart(
        id="part_1",
        message_id="msg_1",
        kind="text",
        content="abcdef" * 200,
        metadata={"task_hash": "task_current"},
    )

    compacted = compact_cold_text_part(part, preview_chars=24)

    assert compacted.metadata["compaction_state"] == "route_compacted"
    assert compacted.metadata["compacted_by"] == "l2_current_task_cold"
    assert "preview=abcdefabcdefabcdefabcdef" in compacted.content
