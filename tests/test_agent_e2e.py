from __future__ import annotations

from dataclasses import dataclass, field

from firstcoder.app.factory import create_firstcoder_app
from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.llm_compact import LlmCompactService
from firstcoder.context.manager import ContextWindowManager
from firstcoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.triggers import ContextCompactionConfig
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall
from firstcoder.tools.view import create_view_tool


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse | ProviderError]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("FakeProvider 没有剩余响应")
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response


def test_agent_single_turn_e2e_writes_and_rebuilds_session(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_e2e", agents_md="项目规则")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="收到")])

    response = AgentLoop(session=session, provider=provider).run_user_turn("你好")

    assert response.content == "收到"
    assert len(provider.requests) == 1
    assert provider.requests[0].messages[0].role == "system"
    assert "项目规则" in provider.requests[0].messages[0].content

    view = store.rebuild_session_view("sess_e2e")
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[0].parts[0].content == "你好"
    assert view.messages[1].parts[0].content == "收到"


def test_agent_tool_call_e2e_uses_real_view_tool_and_persists_result(tmp_path) -> None:
    (tmp_path / "README.md").write_text("标题\n正文", encoding="utf-8")
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_e2e", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_view",
                        name="view",
                        arguments={"path": "README.md", "limit": 2},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="README 已读取"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[create_view_tool(tmp_path)],
    ).run_user_turn("读 README")

    assert response.content == "README 已读取"
    assert len(provider.requests) == 2
    assert "view" in [tool.name for tool in provider.requests[0].tools]
    assert provider.requests[1].messages[-2].role == "assistant"
    assert provider.requests[1].messages[-2].tool_calls[0].name == "view"
    assert provider.requests[1].messages[-1].role == "tool"
    assert provider.requests[1].messages[-1].tool_call_id == "call_view"
    assert "1: 标题" in provider.requests[1].messages[-1].content
    assert "2: 正文" in provider.requests[1].messages[-1].content

    view = store.rebuild_session_view("sess_e2e")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    assert view.messages[1].parts[0].kind == "tool_call"
    assert view.messages[2].parts[0].kind == "tool_result"
    assert view.messages[2].parts[0].metadata["tool_name"] == "view"
    assert view.messages[2].parts[0].metadata["ok"] is True


def test_agent_resume_e2e_replays_history_and_continues_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    original = AgentSession.create(store=store, session_id="sess_e2e", agents_md="规则")
    first_provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="第一轮回复")])

    AgentLoop(session=original, provider=first_provider).run_user_turn("第一轮")

    resumed = AgentSession.resume(store=store, session_id="sess_e2e", agents_md="规则")
    second_provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="第二轮回复")])
    response = AgentLoop(session=resumed, provider=second_provider).run_user_turn("第二轮")

    assert response.content == "第二轮回复"
    assert len(second_provider.requests) == 1
    provider_roles = [message.role for message in second_provider.requests[0].messages]
    assert provider_roles == ["system", "user", "assistant", "user"]
    assert second_provider.requests[0].messages[1].content.endswith("第一轮")
    assert second_provider.requests[0].messages[2].content == "第一轮回复"
    assert second_provider.requests[0].messages[3].content.endswith("第二轮")

    view = store.rebuild_session_view("sess_e2e")
    assert [message.role for message in view.messages] == ["user", "assistant", "user", "assistant"]
    assert view.messages[-1].parts[0].content == "第二轮回复"


def test_app_user_flow_e2e_reads_file_renames_shares_resumes_and_continues(tmp_path) -> None:
    """模拟用户从 TUI 组装入口完成一段真实工作流。"""

    (tmp_path / "AGENTS.md").write_text("项目规则：保持清晰。", encoding="utf-8")
    (tmp_path / "README.md").write_text("标题\n正文", encoding="utf-8")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_view",
                        name="view",
                        arguments={"path": "README.md", "limit": 2},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="README 已读取"),
            ChatResponse(provider="fake", model="fake-model", content="继续完成"),
        ]
    )
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=provider,
        session_id="sess_app_flow",
        tools=[create_view_tool(tmp_path)],
    )

    first_response = app.chat_runner.run_user_turn("读 README")
    rename_result = app.command_handler.handle("/rename README 阅读")
    share_result = app.command_handler.handle("/share sess_app_flow")
    resume_result = app.command_handler.handle("/resume sess_app_flow")
    second_response = app.chat_runner.run_user_turn("继续")

    assert first_response.content == "README 已读取"
    assert second_response.content == "继续完成"
    assert "Renamed session: sess_app_flow README 阅读" in rename_result.output
    assert "Share exported:" in share_result.output
    assert "Resumed session: sess_app_flow README 阅读" in resume_result.output
    assert app.current_session.session_id == "sess_app_flow"

    share_path = tmp_path / ".firstcoder" / "shares" / "sess_app_flow.md"
    share_text = share_path.read_text(encoding="utf-8")
    assert "# README 阅读" in share_text
    assert "读 README" in share_text
    assert "README 已读取" in share_text
    assert "view" in share_text

    view = JsonlSessionStore(tmp_path / ".firstcoder").rebuild_session_view("sess_app_flow")
    assert [message.role for message in view.messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "user",
        "assistant",
    ]
    assert provider.requests[-1].messages[-1].content.endswith("继续")


def test_prompt_too_long_e2e_writes_l4_checkpoint_and_retries_with_summary(tmp_path) -> None:
    """模拟上下文过长后的 L4 摘要恢复链路。"""

    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_prompt_retry", agents_md="规则")
    seed_provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="旧回复")])
    AgentLoop(session=session, provider=seed_provider).run_user_turn("旧问题")

    provider = FakeProvider(
        [
            ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "context too long"),
            ChatResponse(provider="fake", model="fake-model", content="压缩摘要：旧问题已经回答。"),
            ChatResponse(provider="fake", model="fake-model", content="恢复后完成"),
        ]
    )
    context_manager = ContextWindowManager(
        store=store,
        config=ContextCompactionConfig(
            auto_compact_threshold=1_000_000,
            target_tokens=1,
            blocking_target_tokens=1,
        ),
        l4_service=LlmCompactService(
            store=store,
            summarizer=ProviderLlmCompactSummarizer(provider),
        ),
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        context_manager=context_manager,
    ).run_user_turn("新问题")

    assert response.content == "恢复后完成"
    event_types = [event.type for event in store.list_events("sess_prompt_retry")]
    assert "compaction_completed" in event_types
    assert "checkpoint_created" in event_types
    assert "llm_compaction_completed" in event_types

    retry_request = provider.requests[-1]
    retry_contents = [message.content for message in retry_request.messages]
    assert any("压缩摘要：旧问题已经回答。" in content for content in retry_contents)
    assert retry_request.messages[-1].content.endswith("新问题")
    assert all("旧问题" not in content for content in retry_contents[2:])
