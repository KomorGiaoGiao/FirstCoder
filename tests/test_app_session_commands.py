from pathlib import Path

from firstcoder.app.commands import ContextCommandHandler
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import CurrentSessionState
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.writer import SessionEventWriter
from firstcoder.session.catalog import SessionCatalog
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
