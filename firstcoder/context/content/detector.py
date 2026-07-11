"""L1-L3 程序化压缩使用的内容检测器。"""

from __future__ import annotations

from firstcoder.context.models import MessagePart
from firstcoder.context.token_budget import estimate_text_tokens


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


def is_large_tool_result(part: MessagePart, *, min_tokens: int) -> bool:
    if is_already_compacted(part):
        return False
    return part.kind == "tool_result" and estimate_text_tokens(part.content) >= min_tokens


def is_current_task_cold_part(
    part: MessagePart,
    *,
    active_task_hash: str | None,
    current_turn: int,
    cold_turn_distance: int,
) -> bool:
    if is_already_compacted(part):
        return False
    if part.kind != "text":
        return False
    if not active_task_hash or part.metadata.get("task_hash") != active_task_hash:
        return False

    created_turn = part.metadata.get("created_turn")
    if not isinstance(created_turn, int):
        return False
    return current_turn - created_turn >= cold_turn_distance
