from dataclasses import dataclass, field
from pathlib import Path

from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.app.factory import create_firstcoder_app
from firstcoder.app.model_state import ModelStateStore
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import AgentChatRunner
from firstcoder.config.settings import AppConfig
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.llm_compact import LlmCompactService
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ProviderCapabilities, ToolCall
from firstcoder.tools.write import create_write_tool
from firstcoder.tools.types import Tool
from firstcoder.providers.types import ToolDefinition
from firstcoder.mcp.models import McpServerStatus, McpToolDescription


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = ProviderCapabilities()
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)


class FakeMcpManager:
    def __init__(self, tools=(), statuses=()) -> None:
        self.tools_value = tools
        self.statuses_value = statuses
        self.connect_calls = 0
        self.close_calls = 0

    def connect_all(self) -> None:
        self.connect_calls += 1

    def connect_all_in_background(self) -> None:
        self.connect_calls += 1

    def tools(self):
        return self.tools_value

    def statuses(self):
        return self.statuses_value

    def doctor(self, name: str):
        return next((status for status in self.statuses_value if status.name == name), None)

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]):
        return {"content": [{"type": "text", "text": "ok"}]}

    def close(self) -> None:
        self.close_calls += 1


def test_factory_connects_mcp_once_and_merges_discovered_tools(tmp_path: Path) -> None:
    manager = FakeMcpManager(
        tools=(("demo", McpToolDescription("ping", "Ping", {"type": "object", "properties": {}})),),
        statuses=(McpServerStatus("demo", "connected", tool_count=1),),
    )
    app = create_firstcoder_app(
        project_root=tmp_path,
        provider=FakeProvider([]),
        session_id="sess_test",
        mcp_manager_factory=lambda configs: manager,
    )

    assert manager.connect_calls == 1
    assert "write" in [tool.name for tool in app.current_session.session.tool_registry.tools()]
    assert "mcp__demo__ping" in [tool.name for tool in app.current_session.session.tool_registry.tools()]
    assert "/mcp list" in app.command_handler.handle("/help").output


def test_factory_keeps_builtin_tools_when_mcp_connection_fails(tmp_path: Path) -> None:
    manager = FakeMcpManager(statuses=(McpServerStatus("demo", "failed", error="safe failure"),))
    app = create_firstcoder_app(
        project_root=tmp_path,
        provider=FakeProvider([]),
        session_id="sess_test",
        mcp_manager_factory=lambda configs: manager,
    )

    assert "write" in [tool.name for tool in app.current_session.session.tool_registry.tools()]
    assert "mcp__demo__ping" not in [tool.name for tool in app.current_session.session.tool_registry.tools()]


def test_factory_custom_tools_mode_does_not_append_mcp_tools(tmp_path: Path) -> None:
    manager = FakeMcpManager(
        tools=(("demo", McpToolDescription("ping", "Ping", {"type": "object", "properties": {}})),),
    )
    app = create_firstcoder_app(
        project_root=tmp_path,
        provider=FakeProvider([]),
        session_id="sess_test",
        tools=[],
        mcp_manager_factory=lambda configs: manager,
    )

    assert "mcp__demo__ping" not in app.current_session.session.tool_registry.names()


def test_app_unmount_closes_mcp_manager_once(tmp_path: Path) -> None:
    manager = FakeMcpManager()
    app = create_firstcoder_app(
        project_root=tmp_path,
        provider=FakeProvider([]),
        session_id="sess_test",
        tools=[],
        mcp_manager_factory=lambda configs: manager,
    )

    app.on_unmount()
    app.on_unmount()

    assert manager.close_calls == 1


def test_create_firstcoder_app_wires_session_commands_context_and_chat(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("项目规则", encoding="utf-8")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="收到")])

    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=provider,
        session_id="sess_test",
        tools=[],
    )

    assert isinstance(app.command_handler, CompositeCommandHandler)
    assert isinstance(app.chat_runner, AgentChatRunner)
    assert (tmp_path / ".firstcoder" / "sessions" / "sess_test.jsonl").exists()
    assert "Session: sess_test" in app.command_handler.handle("/context").output
    assert "Sessions:" in app.command_handler.handle("/sessions").output
    assert "/resume" in app.command_handler.handle("/help").output
    response = app.chat_runner.run_user_turn("你好")
    assert response.content == "收到"
    assert "项目规则" in provider.requests[0].messages[0].content


def test_create_firstcoder_app_wires_new_fork_and_skill_commands(tmp_path: Path) -> None:
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    (skills_dir / "brief.md").write_text("# Brief\n", encoding="utf-8")
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
        tools=[],
    )

    new_result = app.command_handler.handle("/new 新会话")
    assert new_result.output.startswith("New session: sess_")
    new_session_id = app.current_session.session.session_id
    assert new_session_id != "sess_test"

    fork_result = app.command_handler.handle("/fork 分支")
    assert fork_result.output.startswith(f"Forked session: {new_session_id} -> sess_")
    assert app.current_session.session.session_id != new_session_id

    skills_result = app.command_handler.handle("/skills")
    assert "brief project skills/brief.md" in skills_result.output
    skill_result = app.command_handler.handle("/skill brief")
    assert "Skill: brief" in skill_result.output


def test_create_firstcoder_app_enables_streaming_for_capable_provider(tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="fake-model", content="ok")],
        capabilities=ProviderCapabilities(supports_streaming=True),
    )

    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=provider,
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.use_streaming is True


def test_create_firstcoder_app_honors_streaming_disabled_config(tmp_path: Path) -> None:
    provider = FakeProvider(
        responses=[ChatResponse(provider="fake", model="fake-model", content="ok")],
        capabilities=ProviderCapabilities(supports_streaming=True),
    )
    config = AppConfig(
        provider_name="fake",
        env={},
        project_config={"provider": {"streaming": False}},
    )

    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=provider,
        session_id="sess_test",
        tools=[],
        app_config=config,
    )

    assert app.chat_runner.use_streaming is False


def test_model_command_switches_runtime_provider_and_compact_summarizer(tmp_path: Path) -> None:
    initial_provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])
    config = AppConfig(
        provider_name="openai-compatible",
        env={"YURENAPI_API_KEY": "test-key"},
        project_config={
            "model": "yurenapi/old-model",
            "provider": {
                "type": "openai-compatible",
                "name": "yurenapi",
                "base_url": "https://example.test/v1",
                "api_key_env": "YURENAPI_API_KEY",
                "parallel_tool_calls": True,
            },
        },
    )
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=initial_provider,
        session_id="sess_test",
        tools=[],
        app_config=config,
    )

    result = app.command_handler.handle("/model new-model")

    assert result.output == "Model switched: yurenapi/new-model"
    assert result.action == {"type": "model_changed", "provider": "yurenapi", "model": "new-model"}
    assert app.chat_runner.provider.name == "yurenapi"
    assert app.chat_runner.provider.model == "new-model"
    assert app.chat_runner.use_streaming is True
    assert app.chat_runner.context_manager.l4_service.summarizer.provider is app.chat_runner.provider


def _catalog_config(*, default_model: str | None = "yuren/main") -> AppConfig:
    project = {
        "providers": {
            "yuren": {
                "type": "openai-compatible",
                "base_url": "https://example.test/v1",
                "api_key_env": "YUREN_KEY",
            },
            "mimo": {
                "type": "openai-compatible",
                "base_url": "https://mimo.example/v1",
                "api_key_env": "MIMO_KEY",
            },
        },
        "models": {
            "yuren/main": {"request": {"temperature": 0.2}},
            "mimo/pro": {},
        },
    }
    if default_model is not None:
        project["default_model"] = default_model
    return AppConfig(
        provider_name="openai-compatible",
        env={"YUREN_KEY": "test-key", "MIMO_KEY": "mimo-key"},
        project_config=project,
    )


def test_factory_catalog_startup_honors_model_spec_over_default(tmp_path: Path) -> None:
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        app_config=_catalog_config(),
        model_spec="mimo/pro",
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.provider.name == "mimo"
    assert app.chat_runner.provider.model == "pro"


def test_factory_catalog_startup_honors_default_over_saved_state(tmp_path: Path) -> None:
    data_root = tmp_path / ".firstcoder"
    ModelStateStore(data_root / "model_state.json").record_selection("mimo/pro")
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=data_root,
        app_config=_catalog_config(default_model="yuren/main"),
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.provider.name == "yuren"
    assert app.chat_runner.provider.model == "main"


def test_factory_catalog_startup_falls_back_from_stale_saved_state(tmp_path: Path) -> None:
    data_root = tmp_path / ".firstcoder"
    ModelStateStore(data_root / "model_state.json").record_selection("gone/model")
    config = _catalog_config(default_model=None)
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=data_root,
        app_config=config,
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.provider.name == "mimo"
    assert app.chat_runner.provider.model == "pro"


def test_factory_legacy_custom_provider_keeps_base_url_environment_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("FIRSTCODER_API_KEY", "legacy-key")
    monkeypatch.setenv("FIRSTCODER_BASE_URL", "https://legacy.example/v1")
    monkeypatch.delenv("FIRSTCODER_PROVIDER_NAME", raising=False)
    (tmp_path / "firstcoder.toml").write_text(
        '\n'.join(
            [
                'model = "legacy/model"',
                '',
                '[provider]',
                'type = "openai-compatible"',
                'name = "legacy"',
            ]
        ),
        encoding="utf-8",
    )

    app = create_firstcoder_app(
        project_root=tmp_path,
        provider=None,
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.provider.name == "legacy"
    assert app.chat_runner.provider.model == "model"
    assert app.chat_runner.provider.base_url == "https://legacy.example/v1"


def test_catalog_model_switch_records_selection_and_request_options(tmp_path: Path) -> None:
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        app_config=_catalog_config(),
        session_id="sess_test",
        tools=[],
    )

    result = app.command_handler.handle("/model mimo/pro")

    assert result.output == "Model switched: mimo/pro"
    assert app.chat_runner.request_options.temperature is None
    assert ModelStateStore(tmp_path / ".firstcoder" / "model_state.json").load().last_selected == "mimo/pro"


def test_catalog_picker_can_switch_mixed_case_provider_ref(tmp_path: Path) -> None:
    config = AppConfig(
        provider_name="openai-compatible",
        env={"YUREN_KEY": "test-key"},
        project_config={
            "default_model": "Yuren/main",
            "providers": {
                "Yuren": {
                    "type": "openai-compatible",
                    "base_url": "https://example.test/v1",
                    "api_key_env": "YUREN_KEY",
                }
            },
            "models": {
                "Yuren/main": {},
                "Yuren/pro": {},
            },
        },
    )
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        app_config=config,
        session_id="sess_test",
        tools=[],
    )

    picker = app.command_handler.handle("/models")
    selected = picker.action["models"][1]
    result = app.command_handler.handle(f"/model {selected['provider']}/{selected['model']}")

    assert selected == {"provider": "Yuren", "model": "pro"}
    assert result.output == "Model switched: Yuren/pro"
    assert app.chat_runner.provider.name == "Yuren"
    assert ModelStateStore(tmp_path / ".firstcoder" / "model_state.json").load().last_selected == "Yuren/pro"


def test_catalog_anthropic_alias_is_current_model_and_picker_selection(tmp_path: Path) -> None:
    config = AppConfig(
        provider_name="anthropic",
        env={"ANTHROPIC_API_KEY": "test-key"},
        project_config={
            "default_model": "claude/sonnet",
            "providers": {
                "claude": {
                    "type": "anthropic",
                    "api_key_env": "ANTHROPIC_API_KEY",
                }
            },
            "models": {
                "claude/sonnet": {},
                "claude/opus": {},
            },
        },
    )
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        app_config=config,
        session_id="sess_test",
        tools=[],
    )

    picker = app.command_handler.handle("/models")

    assert app.chat_runner.provider.name == "claude"
    assert "Current model: claude/sonnet" in picker.output
    assert picker.action["models"] == [
        {"provider": "claude", "model": "opus"},
        {"provider": "claude", "model": "sonnet"},
    ]
    assert picker.action["selected_index"] == 1


def test_app_factory_configures_default_loop_limits(tmp_path: Path) -> None:
    app = create_firstcoder_app(project_root=tmp_path, provider=FakeProvider([]), tools=[])

    assert app.chat_runner.limits == AgentLoopLimits.default()


def test_create_firstcoder_app_keeps_streaming_disabled_without_capability(tmp_path: Path) -> None:
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
        tools=[],
    )

    assert app.chat_runner.use_streaming is False


def test_create_firstcoder_app_uses_consistent_data_root_for_share(tmp_path: Path) -> None:
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
        tools=[],
    )

    result = app.command_handler.handle("/share sess_test")

    assert "Share exported:" in result.output
    assert (tmp_path / ".firstcoder" / "shares" / "sess_test.md").exists()
    assert JsonlSessionStore(tmp_path / ".firstcoder").rebuild_session_view("sess_test").session_id == "sess_test"


def test_create_firstcoder_app_can_use_default_builtin_tools(tmp_path: Path) -> None:
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
    )

    assert app.chat_runner.tools
    names = [tool.name for tool in app.chat_runner.tools or []]
    assert "write" in names
    assert "edit" in names
    assert "apply_patch" in names
    assert "shell" in names
    assert "fetch" in names
    assert "web_search" in names


def test_create_firstcoder_app_hides_task_boundary_from_main_model(tmp_path: Path) -> None:
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=provider,
        session_id="sess_test",
    )

    app.chat_runner.run_user_turn("你好")

    tool_names = [tool.name for tool in provider.requests[0].tools]
    assert "task_boundary" in app.chat_runner.current_session.session.tool_registry.names()
    assert "task_boundary" not in tool_names
    assert "fetch" in tool_names
    assert "web_search" in tool_names
    assert "Task boundaries are internal runtime state, not an agent tool" in provider.requests[0].messages[0].content
    assert "task_boundary" not in provider.requests[0].messages[0].content


def test_create_firstcoder_app_wires_l4_service_for_default_context_manager(tmp_path: Path) -> None:
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_test",
        tools=[],
    )

    assert isinstance(app.chat_runner.context_manager.l4_service, LlmCompactService)


def test_create_firstcoder_app_persists_permission_grants(tmp_path: Path) -> None:
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_write",
                        name="write",
                        arguments={"path": "README.md", "content": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=provider,
        session_id="sess_test",
        tools=[create_write_tool(tmp_path)],
    )

    waiting = app.chat_runner.run_user_turn("写 README")
    assert waiting.finish_reason == "waiting_for_user_input"
    assert app.chat_runner.last_pending_input is not None
    app.chat_runner.resume_with_user_input(app.chat_runner.last_pending_input.id, "allow_always_same_scope")

    assert (tmp_path / ".firstcoder" / "permissions.json").exists()

    second = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")]),
        session_id="sess_second",
        tools=[create_write_tool(tmp_path)],
    )
    result = second.chat_runner.current_session.session.execute_tool_call(
        ToolCall(id="call_write_again", name="write", arguments={"path": "README.md", "content": "again"})
    )

    assert result.ok is True
    assert result.data.get("request_type") != "permission_confirmation"
