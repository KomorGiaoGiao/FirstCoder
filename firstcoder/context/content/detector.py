"""L1-L3 程序化压缩使用的内容检测器。"""

from __future__ import annotations

from firstcoder.context.models import MessagePart

COMPACTED_STATES = {
    "archived",
    "trimmed",
    "micro_compacted",
    "route_compacted",
    "l2_route_compacted",
    "checkpointed",
    "pinned",
}


def is_already_compacted(part: MessagePart) -> bool:
    return str(part.metadata.get("compaction_state") or "raw") in COMPACTED_STATES


def is_old_task_part(part: MessagePart, *, active_task_hash: str | None) -> bool:
    if is_already_compacted(part):
        return False
    if part.kind != "text":
        return False
    task_hash = part.metadata.get("task_hash")
    return bool(active_task_hash and task_hash and task_hash != active_task_hash)
