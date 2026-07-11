"""FirstCoder TUI 组装工厂。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession, create_project_permission_manager
from firstcoder.app.commands import ContextCommandHandler
from firstcoder.app.help_commands import HelpCommandHandler
from firstcoder.app.model_commands import ModelCommandHandler, ModelState
from firstcoder.app.permission_commands import PermissionCommandHandler
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import AgentChatRunner, CurrentSessionState
from firstcoder.app.session_commands import SessionCommandHandler
from firstcoder.app.skill_commands import SkillCommandHandler
from firstcoder.app.tui import FirstCoderApp, FirstCoderTuiConfig
from firstcoder.config.settings import AppConfig, load_config
from firstcoder.context.identity import new_session_id
from firstcoder.context.llm_compact import LlmCompactService
from firstcoder.context.manager import ContextWindowManager
from firstcoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.factory import ProviderConfigError, create_provider, create_provider_from_config
from firstcoder.providers.presets import PROVIDER_PRESETS
from firstcoder.permissions.grants import FilePermissionGrantStore
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.fork import ForkSessionService
from firstcoder.session.new import NewSessionService
from firstcoder.session.resume import ResumeService
from firstcoder.session.share import SessionShareService
from firstcoder.skills.discovery import discover_all_skills
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
    app_config: AppConfig | None = None,
) -> FirstCoderApp:
    """组装可运行的 FirstCoder TUI。

    `data_root` 默认是 `<project_root>/.firstcoder`，并传给 context/session 各组件作为
    统一数据根。
    """

    project_path = Path(project_root)
    resolved_data_root = Path(data_root) if data_root is not None else project_path / ".firstcoder"
    resolved_app_config = app_config or load_config(project_root=project_path)
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
        tools=resolved_tools,
        sandbox_access=sandbox_access,
        catalog=catalog,
    )
    new_service = NewSessionService(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools=resolved_tools,
        sandbox_access=sandbox_access,
    )
    fork_service = ForkSessionService(
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
        tools=resolved_tools,
        context_manager=context_manager,
        limits=AgentLoopLimits.default(),
        use_streaming=_should_use_streaming(resolved_provider, resolved_app_config),
    )
    model_switcher = RuntimeModelSwitcher(
        app_config=resolved_app_config,
        chat_runner=chat_runner,
        compact_summarizer=compact_summarizer,
    )
    command_handler = CompositeCommandHandler(
        [
            HelpCommandHandler(),
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
    ) -> None:
        self._app_config = app_config
        self._chat_runner = chat_runner
        self._compact_summarizer = compact_summarizer

    def current_model(self) -> ModelState:
        provider = self._chat_runner.provider
        return ModelState(provider=provider.name, model=provider.model)

    def model_choices(self) -> list[ModelState]:
        current = self.current_model()
        choices = [current]
        configured = self._app_config.get_config_value("model")
        if configured:
            choices.append(_model_state_from_ref(configured, fallback_provider=current.provider))
        for provider_name, preset in PROVIDER_PRESETS.items():
            choices.append(ModelState(provider=provider_name, model=preset.default_model))
        return _unique_model_states(choices)

    def switch_model(self, spec: str) -> ModelState:
        selected_provider, model = _parse_model_spec(spec)
        config = _config_for_model_switch(
            self._app_config,
            current_provider=self._chat_runner.provider,
            selected_provider=selected_provider,
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


def _parse_model_spec(spec: str) -> tuple[str | None, str]:
    parts = spec.strip().split()
    if len(parts) > 2:
        raise ValueError("usage: /model <model> or /model <provider>/<model>")
    if len(parts) == 2:
        provider, model = parts
    else:
        value = parts[0] if parts else ""
        provider, model = value.split("/", 1) if "/" in value else (None, value)
    provider = provider.strip().lower() if provider else None
    model = model.strip()
    if not model:
        raise ValueError("model name is required")
    return provider, model


def _model_state_from_ref(ref: str, *, fallback_provider: str) -> ModelState:
    if "/" in ref:
        provider, model = ref.split("/", 1)
        return ModelState(provider=provider, model=model)
    return ModelState(provider=fallback_provider, model=ref)


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
    model_ref = model
    if selected_provider:
        if selected_provider in PROVIDER_PRESETS:
            provider_name = selected_provider
        elif selected_provider != current_provider.name:
            raise ValueError(f"unsupported provider: {selected_provider}")
        model_ref = f"{selected_provider}/{model}"
    elif current_provider.name in PROVIDER_PRESETS:
        model_ref = f"{current_provider.name}/{model}"

    project_config = dict(config.project_config or {})
    if selected_provider in PROVIDER_PRESETS:
        project_config["provider"] = _preset_provider_config(config.project_config, provider_name=selected_provider)
    project_config["model"] = model_ref
    return AppConfig(
        provider_name=provider_name,
        env=config.env,
        project_config=project_config,
        global_config=config.global_config,
        project_config_path=config.project_config_path,
        global_config_path=config.global_config_path,
    )


def _preset_provider_config(project_config: dict | None, *, provider_name: str) -> dict:
    preset = PROVIDER_PRESETS[provider_name]
    clean: dict[str, object] = {"api_key_env": preset.api_key_env}
    if preset.base_url_env or preset.default_base_url is not None:
        clean["base_url"] = preset.default_base_url or ""
    provider_config = (project_config or {}).get("provider")
    if not isinstance(provider_config, dict):
        return clean
    nested = provider_config.get(provider_name)
    if isinstance(nested, dict):
        clean[provider_name] = dict(nested)
    return clean
