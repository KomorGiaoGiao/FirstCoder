"""FirstCoder TUI 组装工厂。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from typing import Protocol

from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.app.commands import ContextCommandHandler
from firstcoder.app.help_commands import HelpCommandHandler
from firstcoder.app.mcp_commands import McpCommandHandler
from firstcoder.app.model_commands import ModelCommandHandler, ModelState
from firstcoder.app.model_state import ModelSelectionState, ModelStateStore
from firstcoder.app.permission_commands import PermissionCommandHandler
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import AgentChatRunner, CurrentSessionState
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.app.skill_commands import SkillCommandHandler
from firstcoder.app.tui import FirstCoderApp, FirstCoderTuiConfig
from firstcoder.config.models import ModelCatalog, ModelProfile
from firstcoder.config.settings import AppConfig, load_config
from firstcoder.context.llm_compact import LlmCompactService
from firstcoder.context.manager import ContextWindowManager
from firstcoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from firstcoder.context.store import JsonlSessionStore
from firstcoder.mcp.adapter import adapt_mcp_tool
from firstcoder.mcp.config import load_mcp_configs
from firstcoder.mcp.manager import McpManager
from firstcoder.mcp.models import McpServerStatus, McpToolDescription
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.factory import (
    ProviderConfigError,
    create_provider,
    create_provider_for_model,
    create_provider_from_config,
)
from firstcoder.providers.types import MainRequestOptions
from firstcoder.providers.presets import PROVIDER_PRESETS
from firstcoder.session.bootstrap import SessionBootstrap
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.fork import ForkSessionService
from firstcoder.session.new import NewSessionService
from firstcoder.session.resume import ResumeService
from firstcoder.session.share import SessionShareService
from firstcoder.skills.discovery import discover_all_skills
from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.agent.background import BackgroundJobManager
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


class McpManagerLike(Protocol):
    """Factory-level MCP lifecycle and discovery boundary."""

    def connect_all(self) -> None: ...

    def connect_all_in_background(self) -> None: ...

    def tools(self) -> tuple[tuple[str, McpToolDescription], ...]: ...

    def statuses(self) -> tuple[McpServerStatus, ...]: ...

    def doctor(self, name: str) -> McpServerStatus | None: ...

    def reconnect(self, name: str | None = None) -> bool: ...

    def close(self) -> None: ...


class McpToolProvider:
    """Merge a stable base tool set with the manager's current MCP catalog."""

    def __init__(self, base_tools: list[Tool], manager: McpManagerLike, *, include_mcp: bool) -> None:
        self._base_tools = list(base_tools)
        self._manager = manager
        self._include_mcp = include_mcp

    def __call__(self) -> list[Tool]:
        tools = list(self._base_tools)
        if not self._include_mcp:
            return tools
        names = {tool.name for tool in tools}
        try:
            catalog = self._manager.tools()
        except Exception:
            return tools
        for server, discovered_tool in catalog:
            try:
                tool = adapt_mcp_tool(self._manager, server, discovered_tool, existing_names=names)
            except ValueError:
                continue
            tools.append(tool)
            names.add(tool.name)
        return tools


def create_firstcoder_app(
    *,
    project_root: str | Path = ".",
    data_root: str | Path | None = None,
    provider: ChatProvider | None = None,
    session_id: str | None = None,
    tools: list[Tool] | None = None,
    config: FirstCoderTuiConfig | None = None,
    app_config: AppConfig | None = None,
    mcp_manager_factory: Callable[[tuple], McpManagerLike] | None = None,
    model_spec: str | None = None,
) -> FirstCoderApp:
    """组装可运行的 FirstCoder TUI。

    `data_root` 默认是 `<project_root>/.firstcoder`，并传给 context/session 各组件作为
    统一数据根。
    """

    project_path = Path(project_root)
    resolved_data_root = Path(data_root) if data_root is not None else project_path / ".firstcoder"
    resolved_app_config = app_config or load_config(project_root=project_path)
    model_state_store = ModelStateStore(resolved_data_root / "model_state.json")
    model_catalog = resolved_app_config.model_catalog()
    selected_profile: ModelProfile | None = None
    if provider is None and model_catalog.profiles:
        selected_profile = _initial_model_profile(
            model_catalog,
            model_spec=model_spec,
            state=model_state_store.load(),
        )
        try:
            provider = create_provider_for_model(resolved_app_config, selected_profile)
        except ProviderConfigError as error:
            raise ValueError(str(error)) from error
    store = JsonlSessionStore(resolved_data_root)
    sandbox_access = SandboxAccess()
    background_manager = BackgroundJobManager()
    resolved_tools = (
        tools
        if tools is not None
        else create_builtin_registry(
            project_path,
            include_mutation_tools=True,
            include_execution_tools=True,
            include_network_tools=True,
            access=sandbox_access,
        ).tools()
    )
    mcp_manager = (mcp_manager_factory or McpManager)(load_mcp_configs(resolved_app_config))
    try:
        mcp_manager.connect_all_in_background()
    except Exception:
        pass
    tool_provider = McpToolProvider(resolved_tools, mcp_manager, include_mcp=tools is None)
    current_tools = tool_provider()
    resolved_provider = provider or create_provider(project_root=project_path)
    session = SessionBootstrap(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools=current_tools,
        sandbox_access=sandbox_access,
    ).from_project(session_id=session_id)
    current = CurrentSessionState(session)
    compact_summarizer = ProviderLlmCompactSummarizer(resolved_provider)
    context_manager = ContextWindowManager(
        store=store,
        l4_service=LlmCompactService(
            store=store,
            summarizer=compact_summarizer,
        ),
    )
    catalog = SessionCatalog(resolved_data_root)
    resume_service = ResumeService(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools_provider=tool_provider,
        sandbox_access=sandbox_access,
        catalog=catalog,
    )
    new_service = NewSessionService(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools_provider=tool_provider,
        sandbox_access=sandbox_access,
    )
    fork_service = ForkSessionService(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools_provider=tool_provider,
        sandbox_access=sandbox_access,
        catalog=catalog,
    )
    session_handler = SessionCommandHandler(
        catalog=catalog,
        current_session=current.session,
        new_service=new_service,
        fork_service=fork_service,
        resume_service=resume_service,
        share_service=SessionShareService(store),
        store=store,
        on_resume=current.set_session,
    )
    context_handler = ContextCommandHandler(session=current, context_manager=context_manager)
    permission_handler = PermissionCommandHandler(session=current)
    skill_catalog_provider = lambda: discover_all_skills(project_path)
    skill_handler = SkillCommandHandler(catalog_provider=skill_catalog_provider)
    chat_runner = AgentChatRunner(
        current_session=current,
        provider=resolved_provider,
        tools=current_tools,
        tools_provider=tool_provider,
        context_manager=context_manager,
        limits=AgentLoopLimits.default(),
        use_streaming=_should_use_streaming(resolved_provider, resolved_app_config),
        request_options=_main_request_options(selected_profile),
        background_manager=background_manager,
    )
    model_switcher = RuntimeModelSwitcher(
        app_config=resolved_app_config,
        chat_runner=chat_runner,
        compact_summarizer=compact_summarizer,
        catalog=model_catalog,
        state_store=model_state_store,
    )
    command_handler = CompositeCommandHandler(
        [
            HelpCommandHandler(),
            McpCommandHandler(mcp_manager),
            ModelCommandHandler(model_switcher),
            session_handler,
            context_handler,
            permission_handler,
            skill_handler,
        ]
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
        on_shutdown=mcp_manager.close,
    )


def _should_use_streaming(provider: ChatProvider, config: AppConfig) -> bool:
    if not bool(getattr(getattr(provider, "capabilities", None), "supports_streaming", False)):
        return False
    configured = config.get_provider_bool("streaming", env="FIRSTCODER_STREAMING", provider_name=provider.name)
    if configured is None:
        return True
    return configured


class RuntimeModelSwitcher:
    def __init__(
        self,
        *,
        app_config: AppConfig,
        chat_runner: AgentChatRunner,
        compact_summarizer: ProviderLlmCompactSummarizer,
        catalog: ModelCatalog | None = None,
        state_store: ModelStateStore | None = None,
    ) -> None:
        self._app_config = app_config
        self._chat_runner = chat_runner
        self._compact_summarizer = compact_summarizer
        self._catalog = catalog or app_config.model_catalog()
        self._state_store = state_store

    def current_model(self) -> ModelState:
        provider = self._chat_runner.provider
        return ModelState(provider=provider.name, model=provider.model)

    def model_choices(self) -> list[ModelState]:
        current = self.current_model()
        if self._catalog.profiles:
            return _unique_model_states([ModelState(provider=profile.provider_id, model=profile.model_id) for profile in self._catalog.list()])
        choices = [current]
        for provider_name, preset in PROVIDER_PRESETS.items():
            choices.append(ModelState(provider=provider_name, model=preset.default_model))
        return _unique_model_states(choices)

    def switch_model(self, spec: str) -> ModelState:
        selected_provider, model = _parse_model_spec(spec)
        if self._catalog.profiles:
            if selected_provider is None:
                raise ValueError("模型目录模式需要使用 <provider>/<model>")
            ref = f"{selected_provider}/{model}"
            profile = self._catalog.get(ref)
            if profile is None:
                raise ValueError(f"未配置模型：{ref}。请在 [models] 中添加它。")
            return self._apply_profile(profile, persist=True)

        config = _config_for_model_switch(
            self._app_config,
            current_provider=self._chat_runner.provider,
            selected_provider=selected_provider.lower() if selected_provider else None,
            model=model,
        )
        try:
            provider = create_provider_from_config(config)
        except ProviderConfigError as error:
            raise ValueError(str(error)) from error

        self._app_config = config
        self._chat_runner.set_provider(provider, use_streaming=_should_use_streaming(provider, config))
        self._compact_summarizer.provider = provider
        return ModelState(provider=provider.name, model=provider.model)

    def _apply_profile(self, profile: ModelProfile, *, persist: bool) -> ModelState:
        try:
            provider = create_provider_for_model(self._app_config, profile)
        except ProviderConfigError as error:
            raise ValueError(str(error)) from error
        self._chat_runner.set_model(
            provider,
            request_options=_main_request_options(profile),
            use_streaming=_should_use_streaming(provider, self._app_config),
        )
        self._compact_summarizer.provider = provider
        if persist and self._state_store is not None:
            self._state_store.record_selection(profile.ref)
        return ModelState(provider=provider.name, model=provider.model)


def _main_request_options(profile: ModelProfile | None) -> MainRequestOptions:
    if profile is None:
        return MainRequestOptions()
    request = profile.request
    return MainRequestOptions(
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        extra_body=request.extra_body,
    )


def _initial_model_profile(
    catalog: ModelCatalog,
    *,
    model_spec: str | None,
    state: ModelSelectionState,
) -> ModelProfile:
    for ref in (model_spec, catalog.default_ref, state.last_selected):
        if ref and catalog.get(ref):
            return catalog.require(ref)
    profiles = catalog.list()
    if not profiles:
        raise ValueError("模型目录为空")
    return profiles[0]


def _parse_model_spec(spec: str) -> tuple[str | None, str]:
    value = spec.strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError("usage: /model <model> or /model <provider>/<model>")
    provider, model = value.split("/", 1) if "/" in value else (None, value)
    provider = provider.strip() if provider else None
    model = model.strip()
    if not model:
        raise ValueError("model name is required")
    return provider, model


def _unique_model_states(states: list[ModelState]) -> list[ModelState]:
    unique: list[ModelState] = []
    seen: set[tuple[str, str]] = set()
    for state in states:
        key = (state.provider, state.model)
        if key in seen:
            continue
        seen.add(key)
        unique.append(state)
    return unique


def _config_for_model_switch(
    config: AppConfig,
    *,
    current_provider: ChatProvider,
    selected_provider: str | None,
    model: str,
) -> AppConfig:
    provider_name = config.provider_name
    env = dict(config.env)
    if selected_provider:
        if selected_provider in PROVIDER_PRESETS:
            provider_name = selected_provider
        elif selected_provider != current_provider.name:
            raise ValueError(f"unsupported provider: {selected_provider}")
    if provider_name in PROVIDER_PRESETS:
        env[PROVIDER_PRESETS[provider_name].model_env] = model
    else:
        env["FIRSTCODER_MODEL"] = model
    return AppConfig(
        provider_name=provider_name,
        env=env,
        project_config=config.project_config,
        global_config=config.global_config,
        project_config_path=config.project_config_path,
        global_config_path=config.global_config_path,
    )
