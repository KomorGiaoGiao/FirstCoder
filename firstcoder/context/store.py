"""基于 JSONL 的会话事件存储。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from firstcoder.context.events import SessionEvent
from firstcoder.context.models import AgentMessage, MessagePart, SessionView


EVENT_ROLE_MAP = {
    "user_message": "user",
    "assistant_message": "assistant",
    "tool_result": "tool",
}


class JsonlSessionStore:
    """append-only JSONL store。

    当前阶段选择 JSONL 是为了让 resume、压缩事件和调试记录都能被人工阅读。后续迁移
    SQLite 时，外部仍应保留 `append_event/list_events/rebuild_session_view` 这组边界。
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.sessions_dir = self.root / "sessions"
        self.sessions_dir.mkdir(parents=True, exist_ok=True)

    def append_event(self, event: SessionEvent) -> None:
        path = self._session_path(event.session_id)
        with path.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True))
            file.write("\n")

    def list_events(self, session_id: str) -> list[SessionEvent]:
        path = self._session_path(session_id)
        if not path.exists():
            return []

        events: list[SessionEvent] = []
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    events.append(SessionEvent.from_dict(json.loads(line)))
        return events

    def rebuild_session_view(self, session_id: str) -> SessionView:
        view = SessionView(session_id=session_id)
        for event in self.list_events(session_id):
            self._apply_event(view, event)
        return view

    def _session_path(self, session_id: str) -> Path:
        return self.sessions_dir / f"{session_id}.jsonl"

    def _apply_event(self, view: SessionView, event: SessionEvent) -> None:
        if event.type == "session_created":
            view.metadata.update(event.payload)
            return

        role = EVENT_ROLE_MAP.get(event.type)
        if role is None:
            return

        view.messages.append(_message_from_event(event, role=role))


def _message_from_event(event: SessionEvent, *, role: str) -> AgentMessage:
    payload = event.payload
    message_id = str(payload["message_id"])
    parts = _parts_from_payload(payload.get("parts", []), message_id=message_id)
    return AgentMessage(
        id=message_id,
        session_id=event.session_id,
        role=role,
        parts=parts,
        created_at=event.created_at,
        metadata=dict(payload.get("metadata") or {}),
    )


def _parts_from_payload(parts: Iterable[dict[str, object]], *, message_id: str) -> list[MessagePart]:
    result: list[MessagePart] = []
    for part in parts:
        data = dict(part)
        data.setdefault("message_id", message_id)
        result.append(MessagePart.from_dict(data))
    return result
