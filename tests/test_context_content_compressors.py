from firstcoder.context.content.compressors import compact_old_task_part
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
