"""context 事件重放会用到的 metadata patch helper。"""

from __future__ import annotations

from typing import Any

RESERVED_METADATA_KEYS = frozenset({"session_id"})


def merge_metadata_patch(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """按 patch 语义合并 session metadata。

    `None` 表示调用方没有提供该字段，不用于删除已有值。后续如果需要显式删除，
    应该引入独立 marker，避免 rename 等入口误删 session metadata。
    """

    merged = dict(current)
    for key, value in patch.items():
        key = str(key)
        if key in RESERVED_METADATA_KEYS:
            continue
        if value is not None:
            merged[key] = value
    return merged


def metadata_without_reserved_keys(metadata: dict[str, Any]) -> dict[str, Any]:
    """移除不能由 metadata patch 覆盖的身份字段。"""

    return {str(key): value for key, value in metadata.items() if str(key) not in RESERVED_METADATA_KEYS}
