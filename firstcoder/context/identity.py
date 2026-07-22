"""会话存储使用的 ID 与稳定指纹工具。"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from firstcoder.context.models import SessionView


def _new_id(prefix: str) -> str:
    """生成带业务前缀的 ID，方便日志和 JSONL 文件人工排查。"""

    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def new_session_id() -> str:
    return _new_id("sess")


def new_message_id() -> str:
    return _new_id("msg")


def new_part_id() -> str:
    return _new_id("part")


def new_event_id() -> str:
    return _new_id("evt")


def new_checkpoint_id() -> str:
    return _new_id("ckpt")


def stable_json_hash(value: Any, *, length: int = 16) -> str:
    """对 JSON 可序列化对象计算稳定 hash。

    压缩、system prompt cache 和 task hash 都需要跨运行稳定的指纹，所以这里固定
    `sort_keys=True` 和紧凑分隔符，避免 dict 插入顺序影响结果。
    """

    encoded = json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:length]


def content_fingerprint(text: str, *, length: int = 16) -> str:
    """计算文本内容指纹，默认短 hash 便于写入 metadata 和日志。"""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def session_view_fingerprint(view: SessionView) -> str:
    """计算会话消息视图的稳定指纹。"""

    return stable_json_hash(
        {
            "session_id": view.session_id,
            "messages": [message.to_dict() for message in view.messages],
        },
        length=24,
    )


def _json_default(value: Any) -> str:
    """给配置指纹提供稳定兜底，避免常见配置对象让 hash 计算崩掉。"""

    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    return str(value)
