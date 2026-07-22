"""Lightweight session list index."""

from __future__ import annotations

import json
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

from firstcoder.context.events import SessionEvent
from firstcoder.session.catalog import session_sort_key
from firstcoder.session.models import SessionRecord
from firstcoder.utils.text import optional_str

INDEX_VERSION = 1
_INDEX_LOCK = threading.RLock()


class SessionIndex:
    """Cache user-visible session summaries for fast `/sessions` and `/resume`."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.path = self.root / "session_index.json"

    def update_event(self, event: SessionEvent) -> None:
        from firstcoder.session.catalog import build_record_from_events

        with _INDEX_LOCK:
            data = self._load_data()
            events = self._load_session_events(event.session_id)
            if not events:
                return
            try:
                record = build_record_from_events(session_id=event.session_id, events=events)
            except Exception as exc:  # noqa: BLE001 - index must not block event persistence.
                record = SessionRecord(session_id=event.session_id, title=event.session_id, status="corrupt", error=str(exc))
            data["sessions"][event.session_id] = _record_to_dict(record)
            self._write_data(data)

    def list_records(self) -> list[SessionRecord]:
        if not self.path.exists():
            self.rebuild()
        else:
            self._reconcile_missing_files()
        data = self._load_data()
        records = [_record_from_dict(item) for item in data.get("sessions", {}).values() if isinstance(item, dict)]
        return sorted(records, key=session_sort_key, reverse=True)

    def rebuild(self) -> None:
        from firstcoder.session.catalog import record_from_path

        with _INDEX_LOCK:
            sessions_dir = self.root / "sessions"
            data = _empty_data()
            if sessions_dir.exists():
                for path in sessions_dir.glob("*.jsonl"):
                    record = record_from_path(path)
                    data["sessions"][record.session_id] = _record_to_dict(record)
            self._write_data(data)

    def _reconcile_missing_files(self) -> None:
        from firstcoder.session.catalog import record_from_path

        sessions_dir = self.root / "sessions"
        if not sessions_dir.exists():
            return
        with _INDEX_LOCK:
            data = self._load_data()
            sessions = data["sessions"]
            changed = False
            for path in sessions_dir.glob("*.jsonl"):
                if path.stem in sessions:
                    continue
                record = record_from_path(path)
                sessions[record.session_id] = _record_to_dict(record)
                changed = True
            if changed:
                self._write_data(data)

    def _load_data(self) -> dict[str, Any]:
        if not self.path.exists():
            return _empty_data()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - corrupt index can be rebuilt.
            return _empty_data()
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return _empty_data()
        sessions = data.get("sessions")
        if not isinstance(sessions, dict):
            data["sessions"] = {}
        return data

    def _write_data(self, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
        tmp_path.replace(self.path)

    def _load_session_events(self, session_id: str) -> list[SessionEvent]:
        path = self.root / "sessions" / f"{session_id}.jsonl"
        if not path.exists():
            return []
        events: list[SessionEvent] = []
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if line.strip():
                    events.append(SessionEvent.from_dict(json.loads(line)))
        return events


def _empty_data() -> dict[str, Any]:
    return {"version": INDEX_VERSION, "sessions": {}}


def _record_to_dict(record: SessionRecord) -> dict[str, Any]:
    return asdict(record)


def _record_from_dict(data: dict[str, Any]) -> SessionRecord:
    return SessionRecord(
        session_id=str(data.get("session_id") or ""),
        title=str(data.get("title") or data.get("session_id") or ""),
        created_at=optional_str(data.get("created_at")),
        updated_at=optional_str(data.get("updated_at")),
        workspace=optional_str(data.get("workspace")),
        provider=optional_str(data.get("provider")),
        model=optional_str(data.get("model")),
        message_count=int(data.get("message_count") or 0),
        user_turn_count=int(data.get("user_turn_count") or 0),
        checkpoint_count=int(data.get("checkpoint_count") or 0),
        archive_count=int(data.get("archive_count") or 0),
        latest_user_input=optional_str(data.get("latest_user_input")),
        latest_assistant_output=optional_str(data.get("latest_assistant_output")),
        latest_checkpoint_id=optional_str(data.get("latest_checkpoint_id")),
        status=str(data.get("status") or "ok"),
        error=optional_str(data.get("error")),
        metadata=dict(data.get("metadata") or {}),
    )
