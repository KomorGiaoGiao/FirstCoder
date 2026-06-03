from dataclasses import dataclass, field
from pathlib import Path

from firstcoder.app.factory import create_firstcoder_app
from firstcoder.app.router import CompositeCommandHandler
from firstcoder.app.runtime import AgentChatRunner
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ProviderCapabilities


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
    response = app.chat_runner.run_user_turn("你好")
    assert response.content == "收到"
    assert "项目规则" in provider.requests[0].messages[0].content


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
