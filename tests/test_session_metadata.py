from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.metadata import merge_metadata_patch
from firstcoder.context.writer import SessionEventWriter
from firstcoder.session.catalog import SessionCatalog


def test_session_metadata_updated_patch_is_merged_into_session_view(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    writer.append_session_created(title="旧标题", workspace="D:\\Old")
    writer.append_session_metadata_updated(title="新标题")

    view = store.rebuild_session_view("sess_test")

    assert view.metadata["session_id"] == "sess_test"
    assert view.metadata["title"] == "新标题"
    assert view.metadata["workspace"] == "D:\\Old"


def test_session_metadata_cannot_override_session_id(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_real")

    writer.append_session_created(session_id="sess_fake", title="demo")
    writer.append_session_metadata_updated(session_id="sess_fake_2", title="renamed")

    view = store.rebuild_session_view("sess_real")
    record = SessionCatalog(tmp_path).get_session("sess_real")
    events = store.list_events("sess_real")

    assert view.session_id == "sess_real"
    assert view.metadata["session_id"] == "sess_real"
    assert view.metadata["title"] == "renamed"
    assert record.session_id == "sess_real"
    assert record.metadata["session_id"] == "sess_real"
    assert events[0].payload["session_id"] == "sess_real"
    assert "session_id" not in events[1].payload


def test_session_metadata_updated_event_does_not_create_message(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    writer.append_session_created(title="demo")
    writer.append_session_metadata_updated(title="renamed")
    writer.append_user_message("真正的用户消息")

    view = store.rebuild_session_view("sess_test")

    assert [message.role for message in view.messages] == ["user"]
    assert view.messages[0].parts[0].content == "真正的用户消息"


def test_session_catalog_uses_metadata_update_title(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    writer.append_session_created(title="旧标题")
    writer.append_user_message("第一条用户消息")
    writer.append_session_metadata_updated(title="新标题", workspace="D:\\Project")

    record = SessionCatalog(tmp_path).get_session("sess_test")

    assert record.title == "新标题"
    assert record.workspace == "D:\\Project"
    assert record.latest_user_input == "第一条用户消息"


def test_merge_metadata_patch_ignores_none_values() -> None:
    metadata = merge_metadata_patch({"title": "旧标题", "workspace": "D:\\Project"}, {"title": None})

    assert metadata == {"title": "旧标题", "workspace": "D:\\Project"}
