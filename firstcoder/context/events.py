"""append-only 会话事件模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from firstcoder.context.models import utc_now_iso


@dataclass(slots=True)
class SessionEvent:
    """写入 JSONL 的最小事件单位。

    第一版让事件 payload 保持 dict，避免过早引入复杂事件继承树。事件类型决定
    store 重放时如何把 payload 转成 `AgentMessage`。
    """

    id: str
    session_id: str
    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SessionEvent":
        return cls(
            id=str(value["id"]),
            session_id=str(value["session_id"]),
            type=str(value["type"]),
            payload=dict(value.get("payload") or {}),
            created_at=str(value.get("created_at") or utc_now_iso()),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "type": self.type,
            "payload": self.payload,
            "created_at": self.created_at,
        }
