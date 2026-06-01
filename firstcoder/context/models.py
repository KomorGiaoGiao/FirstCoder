"""FirstCoder 内部会话事实模型。

这些模型表示长期会话事实，不等同于某个 provider 的请求格式。provider 请求由
`ContextBuilder` 在每轮调用前投影出来。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


MessageRole = Literal["user", "assistant", "tool", "system_meta"]
PartKind = Literal[
    "text",
    "tool_call",
    "tool_result",
    "checkpoint_summary",
    "compaction_event_ref",
    "archive_placeholder",
]


def utc_now_iso() -> str:
    """返回稳定的 UTC ISO 时间字符串，统一 JSONL 里的时间格式。"""

    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass(slots=True)
class MessagePart:
    id: str
    message_id: str
    kind: PartKind | str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MessagePart":
        return cls(
            id=str(value["id"]),
            message_id=str(value["message_id"]),
            kind=str(value["kind"]),
            content=str(value.get("content", "")),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "message_id": self.message_id,
            "kind": self.kind,
            "content": self.content,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class AgentMessage:
    id: str
    session_id: str
    role: MessageRole | str
    parts: list[MessagePart]
    created_at: str = field(default_factory=utc_now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "AgentMessage":
        return cls(
            id=str(value["id"]),
            session_id=str(value["session_id"]),
            role=str(value["role"]),
            parts=[MessagePart.from_dict(part) for part in value.get("parts", [])],
            created_at=str(value.get("created_at") or utc_now_iso()),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "parts": [part.to_dict() for part in self.parts],
            "created_at": self.created_at,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class SessionView:
    """由事件日志重放得到的当前会话视图。"""

    session_id: str
    messages: list[AgentMessage] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
