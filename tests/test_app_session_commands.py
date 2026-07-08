from pathlib import Path

from firstcoder.app.commands import ContextCommandHandler
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import CurrentSessionState
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.agent.session import AgentSession
from firstcoder.context.events import SessionEvent
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.fork import ForkSessionService
from firstcoder.session.new import NewSessionService
from firstcoder.session.resume import ResumeService
from firstcoder.session.share import SessionShareService


class CurrentSession:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id


def _make_session(store: JsonlSessionStore, session_id: str, *, title: str = "demo") -> None:
    writer = SessionEventWriter(store=store, session_id=session_id)
    writer.append_session_created(title=title)
    writer.append_user_message(f"{title} 用户消息")


def test_sessions_command_lists_catalog_records(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _make_session(store, "sess_one", title="第一个")
    _make_session(store, "sess_two", title="第二个")
    handler = SessionCommandHandler(catalog=SessionCatalog(tmp_path))

    result = handler.handle("/sessions")

    assert result.handled is True
    assert "Sessions:" in result.output
    assert "sess_one 第一个" in result.output
    assert "sess_two 第二个" in result.output


def test_sessions_command_limits_initial_output_for_large_catalog(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    for index in range(25):
        _make_session(store, f"sess_{index:02d}", title=f"标题{index:02d}")
    handler = SessionCommandHandler(catalog=SessionCatalog(tmp_path))

    result = handler.handle("/sessions")

    assert result.handled is True
    assert "Showing 20 of 25 sessions" in result.output
    assert "sess_24 标题24" in result.output
    assert "sess_05 标题05" in result.output
    assert "sess_04 标题04" not in result.output


def test_session_command_renders_single_session_summary(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _make_session(store, "sess_one", title="第一个")
    handler = SessionCommandHandler(catalog=SessionCatalog(tmp_path))

    result = handler.handle("/session sess_one")

    assert result.handled is True
    assert "Session: sess_one" in result.output
    assert "Title: 第一个" in result.output
    assert "Messages: 1" in result.output


def test_resume_command_uses_resume_service_and_callback(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    AgentSession.create(store=store, session_id="sess_one", agents_md="")
    resumed = []
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        resume_service=ResumeService(store=store, project_root=tmp_path),
        on_resume=resumed.append,
    )

    result = handler.handle("/resume sess_one")

    assert result.handled is True
    assert "Resumed session: sess_one" in result.output
    assert handler.current_session is resumed[0]
    assert resumed[0].session_id == "sess_one"


def test_resume_without_id_returns_picker_action(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _make_session(store, "sess_one", title="第一个")
    _make_session(store, "sess_two", title="第二个")
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        resume_service=ResumeService(store=store, project_root=tmp_path),
    )

    result = handler.handle("/resume")

    assert result.handled is True
    assert result.action == {
        "type": "resume_picker",
        "selected_index": 0,
        "sessions": [
            {"session_id": "sess_two", "title": "第二个", "message_count": 1, "status": "ok"},
            {"session_id": "sess_one", "title": "第一个", "message_count": 1, "status": "ok"},
        ],
    }


def test_share_command_exports_current_or_selected_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _make_session(store, "sess_one", title="第一个")
    _make_session(store, "sess_two", title="第二个")
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=CurrentSession("sess_one"),
        share_service=SessionShareService(store),
    )

    current = handler.handle("/share")
    selected = handler.handle("/share sess_two --tool-results")

    assert "Share exported:" in current.output
    assert (tmp_path / "shares" / "sess_one.md").exists()
    assert "Share exported:" in selected.output
    assert (tmp_path / "shares" / "sess_two.md").exists()


def test_rename_command_writes_metadata_update(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _make_session(store, "sess_one", title="旧标题")
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=CurrentSession("sess_one"),
        store=store,
    )

    result = handler.handle("/rename 新标题")

    assert result.output == "Renamed session: sess_one 新标题"
    assert SessionCatalog(tmp_path).get_session("sess_one").title == "新标题"


def test_new_command_creates_session_and_updates_current_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    state = CurrentSessionState(AgentSession.create(store=store, session_id="sess_one", agents_md=""))
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        new_service=NewSessionService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )

    result = handler.handle("/new 新会话")

    assert result.handled is True
    assert result.output.startswith("New session: sess_")
    assert "新会话" in result.output
    assert result.action == {"type": "new_session"}
    assert state.session.session_id != "sess_one"
    assert SessionCatalog(tmp_path).get_session(state.session.session_id).title == "新会话"


def test_fork_command_copies_current_session_archives_and_updates_current_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_one")
    writer.append_session_created(title="旧会话")
    writer.append_user_message("原始问题")
    archive_dir = tmp_path / "archives" / "sess_one"
    archive_dir.mkdir(parents=True)
    (archive_dir / "ar_1.txt").write_text("archived output", encoding="utf-8")
    state = CurrentSessionState(AgentSession.resume(store=store, session_id="sess_one", agents_md=""))
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        fork_service=ForkSessionService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )

    result = handler.handle("/fork 分支会话")

    assert result.handled is True
    assert result.output.startswith("Forked session: sess_one -> sess_")
    forked_id = state.session.session_id
    assert forked_id != "sess_one"
    record = SessionCatalog(tmp_path).get_session(forked_id)
    assert record.title == "分支会话"
    assert record.metadata["forked_from"] == "sess_one"
    assert store.rebuild_session_view(forked_id).messages[0].parts[0].content == "原始问题"
    assert (tmp_path / "archives" / forked_id / "ar_1.txt").read_text(encoding="utf-8") == "archived output"


def test_fork_command_rewrites_nested_session_ids(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_one")
    writer.append_session_created(title="旧会话")
    store.append_event(
        SessionEvent(
            id="evt_checkpoint",
            session_id="sess_one",
            type="checkpoint_created",
            payload={
                "id": "ckpt_1",
                "session_id": "sess_one",
                "summary": "摘要",
                "tail_start_message_id": "msg_tail",
                "covered_until_message_id": "msg_tail",
                "source_fingerprint": "source",
            },
        )
    )
    state = CurrentSessionState(AgentSession.resume(store=store, session_id="sess_one", agents_md=""))
    handler = SessionCommandHandler(
        catalog=SessionCatalog(tmp_path),
        current_session=state.session,
        fork_service=ForkSessionService(store=store, project_root=tmp_path),
        on_resume=state.set_session,
    )

    handler.handle("/fork 分支会话")

    forked_id = state.session.session_id
    checkpoint_event = next(event for event in store.list_events(forked_id) if event.type == "checkpoint_created")
    assert checkpoint_event.payload["session_id"] == forked_id
    assert state.session.rebuild_view().checkpoints[0].session_id == forked_id


def test_composite_handler_routes_context_and_session_commands(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    _make_session(store, "sess_one")
    session = AgentSession.resume(store=store, session_id="sess_one", agents_md="")
    router = CompositeCommandHandler(
        [
            SessionCommandHandler(catalog=SessionCatalog(tmp_path), current_session=session),
            ContextCommandHandler(session=session),
        ]
    )

    assert "Sessions:" in router.handle("/sessions").output
    assert "Session: sess_one" in router.handle("/context").output
    assert router.handle("hello").handled is False
    assert "Unknown command: /missing" in router.handle("/missing").output


def test_resume_command_updates_context_command_current_session(tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path)
    AgentSession.create(store=store, session_id="sess_one", agents_md="")
    AgentSession.create(store=store, session_id="sess_two", agents_md="")
    state = CurrentSessionState(AgentSession.resume(store=store, session_id="sess_one", agents_md=""))
    router = CompositeCommandHandler(
        [
            SessionCommandHandler(
                catalog=SessionCatalog(tmp_path),
                current_session=state.session,
                resume_service=ResumeService(store=store, project_root=tmp_path),
                on_resume=state.set_session,
            ),
            ContextCommandHandler(session=state),
        ]
    )

    assert "Session: sess_one" in router.handle("/context").output
    assert "Resumed session: sess_two" in router.handle("/resume sess_two").output
    assert "Session: sess_two" in router.handle("/context").output
