import json
from pathlib import Path

import pytest

from firstcoder.context.events import SessionEvent
from firstcoder.context.store import JsonlSessionStore
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.errors import SessionInvalidIdError, SessionNotFoundError


def _append(store: JsonlSessionStore, event: SessionEvent) -> None:
    store.append_event(event)


def test_session_catalog_returns_empty_list_for_empty_store(tmp_path: Path) -> None:
    catalog = SessionCatalog(tmp_path)

    assert catalog.list_sessions() == []


def test_session_catalog_builds_records_sorted_by_updated_at(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _append(
        store,
        SessionEvent(
            id="evt_old_created",
            session_id="sess_old",
            type="session_created",
            payload={"workspace": "D:\\Old"},
            created_at="2026-06-01T00:00:00Z",
        ),
    )
    _append(
        store,
        SessionEvent(
            id="evt_old_user",
            session_id="sess_old",
            type="user_message",
            payload={
                "message_id": "msg_old_user",
                "parts": [{"id": "part_old", "kind": "text", "content": "旧会话问题"}],
            },
            created_at="2026-06-01T00:00:01Z",
        ),
    )
    _append(
        store,
        SessionEvent(
            id="evt_new_created",
            session_id="sess_new",
            type="session_created",
            payload={"title": "新会话", "workspace": "D:\\New"},
            created_at="2026-06-02T00:00:00Z",
        ),
    )
    _append(
        store,
        SessionEvent(
            id="evt_new_user",
            session_id="sess_new",
            type="user_message",
            payload={
                "message_id": "msg_new_user",
                "parts": [{"id": "part_new_user", "kind": "text", "content": "新会话问题"}],
            },
            created_at="2026-06-02T00:00:01Z",
        ),
    )
    _append(
        store,
        SessionEvent(
            id="evt_new_assistant",
            session_id="sess_new",
            type="assistant_message",
            payload={
                "message_id": "msg_new_assistant",
                "metadata": {"provider": "openai", "model": "gpt-test"},
                "parts": [{"id": "part_new_assistant", "kind": "text", "content": "新会话回答"}],
            },
            created_at="2026-06-02T00:00:02Z",
        ),
    )

    records = SessionCatalog(tmp_path).list_sessions()

    assert [record.session_id for record in records] == ["sess_new", "sess_old"]
    assert records[0].title == "新会话"
    assert records[0].workspace == "D:\\New"
    assert records[0].provider == "openai"
    assert records[0].model == "gpt-test"
    assert records[0].message_count == 2
    assert records[0].user_turn_count == 1
    assert records[0].latest_user_input == "新会话问题"
    assert records[0].latest_assistant_output == "新会话回答"
    assert records[1].title == "旧会话问题"


def test_session_catalog_counts_checkpoints_and_archives(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _append(
        store,
        SessionEvent(
            id="evt_created",
            session_id="sess_test",
            type="session_created",
            payload={},
            created_at="2026-06-01T00:00:00Z",
        ),
    )
    _append(
        store,
        SessionEvent(
            id="evt_tool",
            session_id="sess_test",
            type="tool_result",
            payload={
                "message_id": "msg_tool",
                "parts": [
                    {
                        "id": "part_tool",
                        "kind": "tool_result",
                        "content": "archive placeholder",
                        "metadata": {"archive_id": "archive_1"},
                    }
                ],
            },
            created_at="2026-06-01T00:00:01Z",
        ),
    )
    _append(
        store,
        SessionEvent(
            id="evt_checkpoint",
            session_id="sess_test",
            type="checkpoint_created",
            payload={
                "id": "ckpt_1",
                "summary": "摘要",
                "tail_start_message_id": "msg_tool",
                "covered_until_message_id": "msg_tool",
                "source_fingerprint": "source",
            },
            created_at="2026-06-01T00:00:02Z",
        ),
    )

    record = SessionCatalog(tmp_path).get_session("sess_test")

    assert record.checkpoint_count == 1
    assert record.latest_checkpoint_id == "ckpt_1"
    assert record.archive_count == 1


def test_session_catalog_counts_archives_from_compaction_replacements(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _append(
        store,
        SessionEvent(
            id="evt_tool",
            session_id="sess_test",
            type="tool_result",
            payload={
                "message_id": "msg_tool",
                "parts": [
                    {
                        "id": "part_tool",
                        "kind": "tool_result",
                        "content": "large raw output",
                        "metadata": {"tool_name": "shell"},
                    }
                ],
            },
            created_at="2026-06-01T00:00:00Z",
        ),
    )
    _append(
        store,
        SessionEvent(
            id="evt_compaction",
            session_id="sess_test",
            type="compaction_completed",
            payload={
                "event": {
                    "replacements": [
                        {
                            "message_id": "msg_tool",
                            "source_part_id": "part_tool",
                            "replacement_part": {
                                "id": "part_tool_archived",
                                "kind": "tool_result",
                                "content": "archive_id=archive_from_compaction",
                                "metadata": {
                                    "archive_id": "archive_from_compaction",
                                    "compaction_state": "archived",
                                },
                            },
                        }
                    ]
                }
            },
            created_at="2026-06-01T00:00:01Z",
        ),
    )

    record = SessionCatalog(tmp_path).get_session("sess_test")

    assert record.archive_count == 1


def test_session_catalog_marks_corrupt_session_without_blocking_others(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _append(
        store,
        SessionEvent(
            id="evt_valid",
            session_id="sess_valid",
            type="session_created",
            payload={"title": "有效会话"},
            created_at="2026-06-01T00:00:00Z",
        ),
    )
    corrupt_path = tmp_path / "sessions" / "sess_corrupt.jsonl"
    corrupt_path.write_text("{not json}\n", encoding="utf-8")

    records = SessionCatalog(tmp_path).list_sessions()
    by_id = {record.session_id: record for record in records}

    assert by_id["sess_valid"].status == "ok"
    assert by_id["sess_corrupt"].status == "corrupt"
    assert by_id["sess_corrupt"].error


def test_session_catalog_marks_empty_session(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "sess_empty.jsonl").write_text("", encoding="utf-8")

    record = SessionCatalog(tmp_path).get_session("sess_empty")

    assert record.status == "empty"
    assert record.title == "sess_empty"
    assert record.message_count == 0


def test_session_catalog_get_missing_session_raises(tmp_path: Path) -> None:
    catalog = SessionCatalog(tmp_path)

    with pytest.raises(SessionNotFoundError):
        catalog.get_session("sess_missing")


@pytest.mark.parametrize(
    "session_id",
    [
        "../other",
        "..\\other",
        "nested/session",
        "nested\\session",
        "",
        ".",
        "..",
        "sess test",
    ],
)
def test_session_catalog_rejects_unsafe_session_ids(tmp_path: Path, session_id: str) -> None:
    catalog = SessionCatalog(tmp_path)

    with pytest.raises(SessionInvalidIdError):
        catalog.get_session(session_id)
    assert catalog.exists(session_id) is False


def test_session_catalog_exists_checks_jsonl_path(tmp_path: Path) -> None:
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "sess_test.jsonl").write_text(
        json.dumps(
            {
                "id": "evt_1",
                "session_id": "sess_test",
                "type": "session_created",
                "payload": {},
                "created_at": "2026-06-01T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    catalog = SessionCatalog(tmp_path)

    assert catalog.exists("sess_test") is True
    assert catalog.exists("sess_missing") is False
