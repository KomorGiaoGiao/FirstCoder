from pathlib import Path

from firstcoder.context.events import SessionEvent
from firstcoder.context.models import MessagePart
from firstcoder.context.store import JsonlSessionStore


def test_jsonl_store_rebuilds_session_view_from_events(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    session_id = "sess_test"
    user_message_id = "msg_user"
    assistant_message_id = "msg_assistant"

    store.append_event(
        SessionEvent(
            id="evt_1",
            session_id=session_id,
            type="session_created",
            payload={"title": "demo"},
            created_at="2026-06-01T00:00:00Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_2",
            session_id=session_id,
            type="user_message",
            payload={
                "message_id": user_message_id,
                "parts": [
                    {
                        "id": "part_user_text",
                        "message_id": user_message_id,
                        "kind": "text",
                        "content": "实现 context store",
                        "metadata": {"task_hash": "A"},
                    }
                ],
            },
            created_at="2026-06-01T00:00:01Z",
        )
    )
    store.append_event(
        SessionEvent(
            id="evt_3",
            session_id=session_id,
            type="assistant_message",
            payload={
                "message_id": assistant_message_id,
                "parts": [
                    MessagePart(
                        id="part_assistant_text",
                        message_id=assistant_message_id,
                        kind="text",
                        content="先写测试。",
                    ).to_dict()
                ],
            },
            created_at="2026-06-01T00:00:02Z",
        )
    )

    view = store.rebuild_session_view(session_id)

    assert view.session_id == session_id
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[0].parts[0].content == "实现 context store"
    assert view.messages[1].parts[0].metadata == {}


def test_jsonl_store_lists_events_in_append_order(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)

    for index in range(3):
        store.append_event(
            SessionEvent(
                id=f"evt_{index}",
                session_id="sess_test",
                type="runtime_state_updated",
                payload={"index": index},
                created_at=f"2026-06-01T00:00:0{index}Z",
            )
        )

    assert [event.id for event in store.list_events("sess_test")] == [
        "evt_0",
        "evt_1",
        "evt_2",
    ]
