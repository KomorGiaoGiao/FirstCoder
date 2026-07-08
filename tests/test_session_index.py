import json
from pathlib import Path

from firstcoder.context.events import SessionEvent
from firstcoder.context.store import JsonlSessionStore
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.index import SessionIndex


def test_store_append_event_updates_session_index(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)

    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_test",
            type="session_created",
            payload={"title": "Demo"},
            created_at="2026-06-01T00:00:00Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_user",
            session_id="sess_test",
            type="user_message",
            payload={
                "message_id": "msg_user",
                "parts": [{"id": "part_user", "kind": "text", "content": "hello"}],
            },
            created_at="2026-06-01T00:00:01Z",
        )
    )

    index_path = tmp_path / "session_index.json"
    assert index_path.exists()
    data = json.loads(index_path.read_text(encoding="utf-8"))
    record = data["sessions"]["sess_test"]
    assert record["title"] == "Demo"
    assert record["updated_at"] == "2026-06-01T00:00:01Z"
    assert record["message_count"] == 1
    assert record["user_turn_count"] == 1
    assert record["latest_user_input"] == "hello"


def test_catalog_lists_sessions_from_index_without_reading_jsonl(tmp_path: Path, monkeypatch) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_test",
            type="session_created",
            payload={"title": "Indexed"},
            created_at="2026-06-01T00:00:00Z",
        )
    )

    def fail_record_from_path(path):  # noqa: ANN001 - test hook
        raise AssertionError(f"catalog should not scan JSONL for indexed list: {path}")

    monkeypatch.setattr(SessionCatalog, "_record_from_path", fail_record_from_path)

    records = SessionCatalog(tmp_path).list_sessions()

    assert [record.session_id for record in records] == ["sess_test"]
    assert records[0].title == "Indexed"


def test_session_index_rebuilds_missing_index_from_existing_jsonl(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    store.append_event(
        SessionEvent(
            id="evt_created",
            session_id="sess_old",
            type="session_created",
            payload={"title": "Old"},
            created_at="2026-06-01T00:00:00Z",
        )
    )
    (tmp_path / "session_index.json").unlink()

    records = SessionIndex(tmp_path).list_records()

    assert [record.session_id for record in records] == ["sess_old"]
    assert (tmp_path / "session_index.json").exists()
