"""FirstCoder TUI 组装工厂。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.agent.session import AgentSession
from firstcoder.app.commands import ContextCommandHandler
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import AgentChatRunner, CurrentSessionState
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.app.tui import FirstCoderApp, FirstCoderTuiConfig
from firstcoder.context.identity import new_session_id
from firstcoder.context.manager import ContextWindowManager
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.factory import create_provider
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.resume import ResumeService
from firstcoder.session.share import SessionShareService
from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.tools.types import Tool


def create_firstcoder_app(
    *,
    project_root: str | Path = ".",
    data_root: str | Path | None = None,
    provider: ChatProvider | None = None,
    session_id: str | None = None,
    tools: list[Tool] | None = None,
    config: FirstCoderTuiConfig | None = None,
) -> FirstCoderApp:
    """组装可运行的 FirstCoder TUI。

    `data_root` 默认是 `<project_root>/.firstcoder`，并传给 context/session 各组件作为
    统一数据根。
    """

    project_path = Path(project_root)
    resolved_data_root = Path(data_root) if data_root is not None else project_path / ".firstcoder"
    store = JsonlSessionStore(resolved_data_root)
    resolved_tools = tools if tools is not None else create_builtin_registry(project_path).tools()
    session = AgentSession.from_project(
        store=store,
        session_id=session_id or new_session_id(),
        project_root=project_path,
        tools=resolved_tools,
    )
    current = CurrentSessionState(session)
    context_manager = ContextWindowManager(store=store)
    catalog = SessionCatalog(resolved_data_root)
    resolved_provider = provider or create_provider()
    resume_service = ResumeService(
        store=store,
        project_root=project_path,
        tools=resolved_tools,
        catalog=catalog,
    )
    session_handler = SessionCommandHandler(
        catalog=catalog,
        current_session=current.session,
        resume_service=resume_service,
        share_service=SessionShareService(store),
        store=store,
        on_resume=current.set_session,
    )
    context_handler = ContextCommandHandler(session=current, context_manager=context_manager)
    command_handler = CompositeCommandHandler([session_handler, context_handler])
    chat_runner = AgentChatRunner(
        current_session=current,
        provider=resolved_provider,
        tools=resolved_tools,
        context_manager=context_manager,
    )
    return FirstCoderApp(
        command_handler=command_handler,
        chat_runner=chat_runner,
        config=config or FirstCoderTuiConfig(),
    )
