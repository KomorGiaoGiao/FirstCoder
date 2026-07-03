"""FirstCoder TUI 组装工厂。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession, create_project_permission_manager
from firstcoder.app.commands import ContextCommandHandler
from firstcoder.app.permission_commands import PermissionCommandHandler
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import AgentChatRunner, CurrentSessionState
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.app.tui import FirstCoderApp, FirstCoderTuiConfig
from firstcoder.context.identity import new_session_id
from firstcoder.context.llm_compact import LlmCompactService
from firstcoder.context.manager import ContextWindowManager
from firstcoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.factory import create_provider
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.resume import ResumeService
from firstcoder.session.share import SessionShareService
from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


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
    sandbox_access = SandboxAccess()
    resolved_tools = tools if tools is not None else create_builtin_registry(
        project_path,
        include_mutation_tools=True,
        include_execution_tools=True,
        include_network_tools=True,
        access=sandbox_access,
    ).tools()
    resolved_provider = provider or create_provider(project_root=project_path)
    grant_store = FilePermissionGrantStore(resolved_data_root / "permissions.json")
    permission_manager = create_project_permission_manager(project_path, grants=grant_store)
    session = AgentSession.from_project(
        store=store,
        session_id=session_id or new_session_id(),
        project_root=project_path,
        tools=resolved_tools,
        permission_manager=permission_manager,
        sandbox_access=sandbox_access,
    )
    current = CurrentSessionState(session)
    context_manager = ContextWindowManager(
        store=store,
        l4_service=LlmCompactService(
            store=store,
            summarizer=ProviderLlmCompactSummarizer(resolved_provider),
        ),
    )
    catalog = SessionCatalog(resolved_data_root)
    resume_service = ResumeService(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools=resolved_tools,
        sandbox_access=sandbox_access,
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
    permission_handler = PermissionCommandHandler(session=current)
    command_handler = CompositeCommandHandler([session_handler, context_handler, permission_handler])
    chat_runner = AgentChatRunner(
        current_session=current,
        provider=resolved_provider,
        tools=resolved_tools,
        context_manager=context_manager,
        limits=AgentLoopLimits.default(),
        use_streaming=bool(getattr(getattr(resolved_provider, "capabilities", None), "supports_streaming", False)),
    )
    return FirstCoderApp(
        command_handler=command_handler,
        chat_runner=chat_runner,
        current_session=current,
        config=config
        or FirstCoderTuiConfig(
            provider_name=resolved_provider.name,
            provider_model=resolved_provider.model,
            project_name=project_path.resolve().name,
        ),
    )
