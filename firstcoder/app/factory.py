"""FirstCoder TUI 组装工厂。"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable
from dataclasses import replace
from typing import Protocol

from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.processes import ProcessManager
from firstcoder.agent.runtime_capabilities import AgentRuntimeCapabilities
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
from firstcoder.mcp.search import McpSearchEntry, create_mcp_tool_search
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.factory import (
    ProviderConfigError,
    create_provider_for_model,
)
from firstcoder.providers.types import MainRequestOptions
from firstcoder.session.bootstrap import SessionBootstrap
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.fork import ForkSessionService
from firstcoder.session.models import PiiOptions
from firstcoder.session.new import NewSessionService
from firstcoder.session.resume import ResumeService
from firstcoder.session.share import SessionShareService
from firstcoder.skills.discovery import discover_all_skills
from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.agent.background import BackgroundJobManager
from firstcoder.tools.types import Tool
from firstcoder.utils.sandbox_access import SandboxAccess


class McpManagerLike(Protocol):
    """工厂层所需的 MCP 生命周期与发现接口边界。"""

    def connect_all(self) -> None: ...

    def connect_all_in_background(self) -> None: ...

    def tools(self) -> tuple[tuple[str, McpToolDescription], ...]: ...

    def statuses(self) -> tuple[McpServerStatus, ...]: ...

    def doctor(self, name: str) -> McpServerStatus | None: ...

    def reconnect(self, name: str | None = None) -> bool: ...

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]) -> object: ...

    def close(self) -> None: ...


class McpToolProvider:
    """把稳定的内置工具集与 MCP manager 当前发现的工具目录合并。

    每次调用都重新拉目录(MCP 连接是后台异步建立的, 工具集随时间变化)。
    容错策略是降级而不是失败: manager.tools() 抛错时静默回退到内置工具,
    单个工具适配失败(如名字冲突的 ValueError)只跳过该工具。
    """

    def __init__(self, base_tools: list[Tool], manager: McpManagerLike, *, include_mcp: bool) -> None:
        self._base_tools = list(base_tools)
        self._manager = manager
        self._include_mcp = include_mcp

    def __call__(self) -> list[Tool]:
        tools = list(self._base_tools)
        if not self._include_mcp:
            return tools
        names = {tool.name for tool in tools}
        entries: list[McpSearchEntry] = []
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
            entries.append(
                McpSearchEntry(
                    server=server,
                    tool=discovered_tool.name,
                    definition=tool.definition,
                )
            )
        # MCP 工具多时塞一个聚合搜索工具, 让模型按描述检索而不是全量进上下文。
        if entries and "mcp_tool_search" not in names:
            tools.append(create_mcp_tool_search(tuple(entries)))
        return tools


def create_firstcoder_app(
    *,
    project_root: str | Path = ".",
    data_root: str | Path | None = None,
    provider: ChatProvider | None = None,
    session_id: str | None = None,
    resume_session: bool = False,
    tools: list[Tool] | None = None,
    config: FirstCoderTuiConfig | None = None,
    app_config: AppConfig | None = None,
    mcp_manager_factory: Callable[[tuple], McpManagerLike] | None = None,
    model_spec: str | None = None,
    allow_user_input: bool | None = None,
    runtime_capabilities: AgentRuntimeCapabilities | None = None,
) -> FirstCoderApp:
    """组装可运行的 FirstCoder TUI。

    `data_root` 默认是 `<project_root>/.firstcoder`，并传给 context/session 各组件作为
    统一数据根。
    """

    # 确定项目根目录 与 数据根目录
    project_path = Path(project_root)
    resolved_data_root = Path(data_root) if data_root is not None else project_path / ".firstcoder"
    # 此处根据app_config || 从配置文件、.env 和系统环境变量加载应用配置
    resolved_app_config = app_config or load_config(project_root=project_path)

    # 创建ModelState类
    model_state_store = ModelStateStore(resolved_data_root / "model_state.json")
    model_catalog = resolved_app_config.model_catalog()
    selected_profile: ModelProfile | None = None
    if provider is None:
        if not model_catalog.profiles:
            raise ValueError("模型目录为空；请配置 default_model、[providers] 和 [models]")
        selected_profile = _initial_model_profile(
            model_catalog,
            model_spec=model_spec,
            state=model_state_store.load(),
        )
        try:
            # 创建provider实例(不同模型厂商不一样, 也可兼容OpenAI)
            provider = create_provider_for_model(resolved_app_config, selected_profile)
        except ProviderConfigError as error:
            raise ValueError(str(error)) from error

    # 任务边界分类模型
    classifier_provider: ChatProvider | None = None
    classifier_request_options: MainRequestOptions | None = None
    classifier_model_ref = resolved_app_config.get_config_value("task_boundary_classifier_model")
    if classifier_model_ref is not None:
        classifier_model_ref = classifier_model_ref.strip()
        if not classifier_model_ref:
            raise ValueError("task_boundary_classifier_model 不能为空")
        classifier_profile = model_catalog.get(classifier_model_ref)
        if classifier_profile is None:
            raise ValueError(f"任务边界分类模型未配置：{classifier_model_ref}")
        try:
            classifier_provider = create_provider_for_model(resolved_app_config, classifier_profile)
        except ProviderConfigError as error:
            raise ValueError(f"任务边界分类模型配置错误：{error}") from error
        classifier_request_options = _main_request_options(classifier_profile)
    
    store = JsonlSessionStore(resolved_data_root)   # 会话持久化(JSONL 追加写)
    sandbox_access = SandboxAccess()                # 文件系统沙箱访问控制
    background_manager = BackgroundJobManager()     # 后台任务(子进程等)管理
    # 所能使用的能力
    resolved_capabilities = runtime_capabilities or AgentRuntimeCapabilities.interactive()
    if allow_user_input is not None:
        resolved_capabilities = replace(
            resolved_capabilities,
            allow_user_input=allow_user_input,
        )

    # 进程管理 + 日志
    process_manager = ProcessManager(log_root=resolved_data_root / "processes")

    # 内置工具
    resolved_tools = (
        tools
        if tools is not None
        else create_builtin_registry(
            project_path,
            include_mutation_tools=True,
            include_execution_tools=True,
            include_network_tools=True,
            access=sandbox_access,
            include_ask_user=resolved_capabilities.allow_user_input,
            include_think=resolved_capabilities.expose_think_tool,
            include_web_search=resolved_capabilities.expose_web_search_tool,
            process_manager=(
                process_manager if resolved_capabilities.enable_process_tools else None
            ),
        ).tools()
    )

    # 异步连接mcp
    mcp_manager = (mcp_manager_factory or McpManager)(load_mcp_configs(resolved_app_config))
    try:
        mcp_manager.connect_all_in_background()
    except Exception:
        pass
    tool_provider = McpToolProvider(resolved_tools, mcp_manager, include_mcp=tools is None)
    current_tools = tool_provider()
    resolved_provider = provider

    # 会话生命周期管理
    bootstrap = SessionBootstrap(
        store=store,
        project_root=project_path,
        data_root=resolved_data_root,
        tools=current_tools,
        sandbox_access=sandbox_access,
    )
    session = (
        bootstrap.resume(session_id)
        if resume_session and session_id is not None
        else bootstrap.from_project(session_id=session_id)
    )
    current = CurrentSessionState(session)

    # 上下文管理
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
        share_service=SessionShareService(store, pii_options=_pii_for_export(resolved_app_config)),
        store=store,
        on_resume=current.set_session,
    )
    permission_handler = PermissionCommandHandler(session=current)
    skill_catalog_provider = lambda: discover_all_skills(project_path)
    skill_handler = SkillCommandHandler(catalog_provider=skill_catalog_provider)

    # 核心装配
    chat_runner = AgentChatRunner(
        current_session=current,
        provider=resolved_provider,
        classifier_provider=classifier_provider,
        tools=current_tools,
        tools_provider=tool_provider,
        context_manager=context_manager,
        limits=AgentLoopLimits.default(),
        use_streaming=_should_use_streaming(resolved_provider, resolved_app_config),
        request_options=_main_request_options(selected_profile),
        classifier_request_options=classifier_request_options,
        context_window=selected_profile.context_window if selected_profile is not None else None,
        background_manager=background_manager,
        runtime_capabilities=resolved_capabilities,
    )
    context_handler = ContextCommandHandler(
        session=current,
        context_manager=context_manager,
        budget_provider=chat_runner.context_budget,
    )
    model_switcher = RuntimeModelSwitcher(
        app_config=resolved_app_config,
        chat_runner=chat_runner,
        compact_summarizer=compact_summarizer,
        catalog=model_catalog,
        state_store=model_state_store,
    )

    # "/xxx"命令装配
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
        on_shutdown=lambda: _shutdown_runtime(
            mcp_manager=mcp_manager,
            background_manager=background_manager,
            process_manager=process_manager,
        ),
    )


def _shutdown_runtime(*, mcp_manager, background_manager, process_manager) -> None:
    """按本地进程、后台线程、外部连接的顺序清理 TUI 运行时。"""

    process_manager.shutdown()
    background_manager.shutdown()
    mcp_manager.close()


def _should_use_streaming(provider: ChatProvider, config: AppConfig) -> bool:
    """provider 声称支持流式且配置未显式关闭时才启用; 默认开。"""
    if not bool(getattr(getattr(provider, "capabilities", None), "supports_streaming", False)):
        return False
    configured = config.get_provider_bool("streaming", env="FIRSTCODER_STREAMING", provider_name=provider.name)
    if configured is None:
        return True
    return configured


class RuntimeModelSwitcher:
    """运行期模型切换: 换 provider 实例并同步到 chat_runner 与压缩摘要器。

    compact_summarizer 也必须跟着换, 否则切换模型后上下文压缩仍在用旧模型出摘要。
    persist=True 时把选择记入 ModelStateStore, 下次启动优先恢复。
    """

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
        return _unique_model_states([ModelState(provider=profile.provider_id, model=profile.model_id) for profile in self._catalog.list()])

    def switch_model(self, spec: str) -> ModelState:
        """/model <provider>/<model> 切换入口; 裸模型名只在目录可解析时可用。"""
        selected_provider, model = _parse_model_spec(spec)
        if selected_provider is None:
            raise ValueError("模型目录模式需要使用 <provider>/<model>")
        ref = f"{selected_provider}/{model}"
        profile = self._catalog.get(ref)
        if profile is None:
            raise ValueError(f"未配置模型：{ref}。请在 [models] 中添加它。")
        return self._apply_profile(profile, persist=True)

    def _apply_profile(self, profile: ModelProfile, *, persist: bool) -> ModelState:
        try:
            provider = create_provider_for_model(self._app_config, profile)
        except ProviderConfigError as error:
            raise ValueError(str(error)) from error
        self._chat_runner.set_model(
            provider,
            request_options=_main_request_options(profile),
            context_window=profile.context_window,
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


def _pii_for_export(app_config: AppConfig) -> PiiOptions | None:
    """share/transcript 导出表面的 PII 配置; 表面未启用或策略为 off 时返回 None。

    transcript 与 share 共用同一条导出管线, 由 share 表面开关一并控制。
    """

    options = app_config.pii_options()
    if options.strategy == "off" or "share" not in options.surfaces:
        return None
    return options


def _initial_model_profile(
    catalog: ModelCatalog,
    *,
    model_spec: str | None,
    state: ModelSelectionState,
) -> ModelProfile:
    """选启动模型, 优先级: 命令行显式指定 > 配置默认 > 上次选择 > 目录第一个。"""
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
