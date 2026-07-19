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
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall, ToolDefinition
from firstcoder.tools.view import create_view_tool
from firstcoder.tools.types import Tool, ToolResult


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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            basis_message_id = next(
                message.content.split("basis_message_id=", 1)[1].split("]", 1)[0]
                for message in request.messages
                if "basis_message_id=" in message.content
            )
            return ChatResponse(
                provider="fake",
                model="fake-model",
                content=f'{{"decision":"uncertain","basis_message_id":"{basis_message_id}"}}',
            )
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
    assert len(second_provider.requests) == 4
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
            target_tokens=100_000,
            blocking_target_tokens=100_000,
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


def _compact_manager(store: JsonlSessionStore, provider: ChatProvider, *, reason: str) -> ContextWindowManager:
    threshold = 1 if reason == "token_threshold" else 1_000_000
    max_tail_messages = 2 if reason == "tail_message_count" else 1_000
    large_tool_result_tokens = 10 if reason == "large_tool_result" else 1_000_000
    max_turn_tool_result_tokens = 10 if reason == "turn_tool_results" else 1_000_000
    target_tokens = 10_000 if reason == "large_tool_result" else 1
    return ContextWindowManager(
        store=store,
        config=ContextCompactionConfig(
            auto_compact_threshold=threshold,
            target_tokens=target_tokens,
            large_tool_result_tokens=large_tool_result_tokens,
            max_turn_tool_result_tokens=max_turn_tool_result_tokens,
            max_tail_messages=max_tail_messages,
            max_tail_tokens=1_000_000,
        ),
        l4_service=LlmCompactService(
            store=store,
            summarizer=ProviderLlmCompactSummarizer(provider),
        ),
    )


def _events(store: JsonlSessionStore, session_id: str, event_type: str) -> list:
    return [event for event in store.list_events(session_id) if event.type == event_type]


def _compact_events(store: JsonlSessionStore, session_id: str) -> list:
    return _events(store, session_id, "compaction_completed")


def _checkpoint_events(store: JsonlSessionStore, session_id: str) -> list:
    return _events(store, session_id, "checkpoint_created")


def _llm_compact_events(store: JsonlSessionStore, session_id: str) -> list:
    return _events(store, session_id, "llm_compaction_completed")


def _echo_tool(*, output: str) -> Tool:
    def echo(text: str) -> ToolResult:
        return ToolResult(name="echo", ok=True, content=output)

    return Tool(
        definition=ToolDefinition(
            name="echo",
            description="返回测试输出",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        ),
        executor=echo,
    )


def test_auto_token_threshold_e2e_writes_compaction_and_checkpoint(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_auto_token", agents_md="")
    seed = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="旧回复")])
    AgentLoop(session=session, provider=seed).run_user_turn("旧问题")
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="自动压缩摘要"),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
            ChatResponse(provider="fake", model="fake-model", content="最终压缩摘要"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        context_manager=_compact_manager(store, provider, reason="token_threshold"),
    ).run_user_turn("触发 token 阈值")

    assert response.content == "完成"
    compact = _compact_events(store, "sess_auto_token")[0]
    assert compact.payload["trigger"] == "auto"
    assert compact.payload["reason"] == "not_reached"
    assert _llm_compact_events(store, "sess_auto_token")[0].payload["trigger"] == "auto"
    assert _checkpoint_events(store, "sess_auto_token")
    normal_requests = [request for request in provider.requests if request.tools]
    assert normal_requests[-1].messages[-1].content.endswith("触发 token 阈值")


def test_auto_large_single_tool_result_e2e_writes_auto_compaction(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_large_tool", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={"text": "x"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool(output="large output\n" * 50)],
        context_manager=_compact_manager(store, provider, reason="large_tool_result"),
    ).run_user_turn("调用大工具")

    assert response.content == "完成"
    skipped = _events(store, "sess_large_tool", "compaction_skipped")
    assert skipped[-1].payload["trigger"] == "auto"
    assert skipped[-1].payload["reason"] == "skipped_no_effect"
    assert _compact_events(store, "sess_large_tool") == []
    assert not _checkpoint_events(store, "sess_large_tool")


def test_auto_large_turn_tool_results_e2e_writes_auto_compaction(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_turn_tools", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(id="call_echo_1", name="echo", arguments={"text": "one"}),
                    ToolCall(id="call_echo_2", name="echo", arguments={"text": "two"}),
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="turn 摘要"),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
            ChatResponse(provider="fake", model="fake-model", content="最终 turn 摘要"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool(output="turn output\n" * 8)],
        context_manager=_compact_manager(store, provider, reason="turn_tool_results"),
    ).run_user_turn("调用两个工具")

    assert response.content == "完成"
    compact = _compact_events(store, "sess_turn_tools")[0]
    assert compact.payload["trigger"] == "auto"
    assert compact.payload["reason"] == "not_reached"
    assert _llm_compact_events(store, "sess_turn_tools")[0].payload["trigger"] == "auto"
    assert _checkpoint_events(store, "sess_turn_tools")


def test_auto_tail_message_count_e2e_writes_auto_compaction(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_tail_count", agents_md="")
    seed = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="旧回复")])
    AgentLoop(session=session, provider=seed).run_user_turn("旧问题")
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="tail 摘要"),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        context_manager=_compact_manager(store, provider, reason="tail_message_count"),
    ).run_user_turn("新问题")

    assert response.content == "完成"
    compact = _compact_events(store, "sess_tail_count")[0]
    assert compact.payload["trigger"] == "auto"
    assert compact.payload["reason"] == "not_reached"
    assert _llm_compact_events(store, "sess_tail_count")[0].payload["trigger"] == "auto"
    assert _checkpoint_events(store, "sess_tail_count")


def test_task_boundary_e2e_writes_task_hash_changed_compaction(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_task_boundary", agents_md="")
    session.runtime_state.active_task_hash = "task_previous"
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="记录任务切换候选"),
            ChatResponse(provider="fake", model="fake-model", content="任务切换完成"),
        ]
    )

    original_complete = provider.complete

    def complete_with_basis(request: ChatRequest) -> ChatResponse:
        user_messages = [message for message in session.rebuild_view().messages if message.role == "user"]
        user_message_id = user_messages[-1].id
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            decision = "new" if len(user_messages) == 1 else "same"
            return ChatResponse(
                provider="fake",
                model="fake-model",
                content=f'{{"decision":"{decision}","basis_message_id":"{user_message_id}"}}',
            )
        return original_complete(request)

    provider.complete = complete_with_basis

    loop = AgentLoop(
        session=session,
        provider=provider,
        context_manager=ContextWindowManager(store=store),
    )
    loop.run_user_turn("换一个任务")
    response = loop.run_user_turn("继续新任务")

    assert response.content == "任务切换完成"
    task_boundary_events = _events(store, "sess_task_boundary", "task_boundary_observed")
    assert task_boundary_events[-1].payload["should_trigger_compaction"] is True
    compact_events = _compact_events(store, "sess_task_boundary")
    assert any(event.payload["trigger"] == "task_hash_changed" for event in compact_events)


def test_task_boundary_e2e_compacts_old_task_content_when_under_token_budget(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_task_boundary_old_task", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="旧任务记录"),
            ChatResponse(provider="fake", model="fake-model", content="新任务第一轮"),
            ChatResponse(provider="fake", model="fake-model", content="新任务继续"),
        ]
    )

    loop = AgentLoop(
        session=session,
        provider=provider,
        context_manager=ContextWindowManager(store=store),
    )
    original_complete = provider.complete

    def complete_with_latest_user_basis(request: ChatRequest) -> ChatResponse:
        user_messages = [message for message in session.rebuild_view().messages if message.role == "user"]
        user_message_id = user_messages[-1].id
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            decision = "same" if len(user_messages) >= 3 else "new"
            return ChatResponse(
                provider="fake",
                model="fake-model",
                content=f'{{"decision":"{decision}","basis_message_id":"{user_message_id}"}}',
            )
        return original_complete(request)

    provider.complete = complete_with_latest_user_basis

    loop.run_user_turn("旧任务内容 " + ("alpha " * 80))
    first_task_hash = session.runtime_state.active_task_hash
    loop.run_user_turn("换一个任务 " + ("beta " * 20))
    loop.run_user_turn("继续新任务")

    compact_events = [
        event.payload["event"]
        for event in _compact_events(store, "sess_task_boundary_old_task")
        if event.payload["trigger"] == "task_hash_changed"
    ]
    assert compact_events
    assert compact_events[-1]["changed_parts"] >= 1
    assert "l1" in compact_events[-1]["levels_attempted"]

    view = store.rebuild_session_view("sess_task_boundary_old_task")
    old_task_parts = [
        part
        for message in view.messages
        for part in message.parts
        if part.metadata.get("task_hash") == first_task_hash
    ]
    assert old_task_parts
    trimmed_old_parts = [
        part
        for part in old_task_parts
        if part.metadata.get("compacted_by") == "l1_old_task_dialogue"
    ]
    assert trimmed_old_parts
    assert all(part.metadata.get("compaction_state") == "trimmed" for part in trimmed_old_parts)
    assert all(part.content == "" for part in trimmed_old_parts)
    latest_user = [message for message in view.messages if message.role == "user"][-1]
    assert latest_user.parts[0].metadata.get("task_hash") == session.runtime_state.active_task_hash
    assert latest_user.parts[0].metadata.get("task_hash") != first_task_hash
    new_task_user_parts = [
        message.parts[0]
        for message in view.messages
        if message.role == "user" and "换一个任务" in message.parts[0].content
    ]
    assert new_task_user_parts
    assert all(part.metadata.get("task_hash") == session.runtime_state.active_task_hash for part in new_task_user_parts)


def test_task_boundary_e2e_confirms_pending_new_when_next_turn_is_same_task(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_task_boundary_new_then_same", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="旧任务记录"),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_boundary_new",
                        name="task_boundary",
                        arguments={"decision": "new", "basis_message_id": ""},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="新任务第一轮"),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_boundary_same",
                        name="task_boundary",
                        arguments={"decision": "same", "basis_message_id": ""},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="新任务第二轮"),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        context_manager=ContextWindowManager(store=store),
    )
    original_complete = provider.complete

    def complete_with_latest_user_basis(request: ChatRequest) -> ChatResponse:
        user_messages = [message for message in session.rebuild_view().messages if message.role == "user"]
        user_message_id = user_messages[-1].id
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            decision = "same" if len(user_messages) >= 3 else "new"
            return ChatResponse(
                provider="fake",
                model="fake-model",
                content=f'{{"decision":"{decision}","basis_message_id":"{user_message_id}"}}',
            )
        response = original_complete(request)
        for tool_call in response.tool_calls:
            if tool_call.name == "task_boundary":
                tool_call.arguments["basis_message_id"] = user_message_id
        return response

    provider.complete = complete_with_latest_user_basis

    loop.run_user_turn("旧任务内容 " + ("alpha " * 80))
    first_task_hash = session.runtime_state.active_task_hash
    loop.run_user_turn("任务B：HTTP 缓存头解释")
    loop.run_user_turn("任务B：继续 HTTP 缓存头解释")

    compact_events = [
        event.payload["event"]
        for event in _compact_events(store, "sess_task_boundary_new_then_same")
        if event.payload["trigger"] == "task_hash_changed"
    ]
    assert compact_events
    assert compact_events[-1]["changed_parts"] >= 1
    assert session.runtime_state.active_task_hash != first_task_hash

    view = store.rebuild_session_view("sess_task_boundary_new_then_same")
    new_task_user_parts = [
        message.parts[0]
        for message in view.messages
        if message.role == "user" and message.parts[0].content.startswith("任务B")
    ]
    assert len(new_task_user_parts) == 2
    assert all(part.metadata.get("task_hash") == session.runtime_state.active_task_hash for part in new_task_user_parts)


def test_manual_compact_command_e2e_writes_l4_handoff_when_only_current_plain_dialogue_remains(tmp_path) -> None:
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="旧回复"),
            ChatResponse(provider="fake", model="fake-model", content="手动压缩摘要"),
        ]
    )
    app = create_firstcoder_app(
        project_root=tmp_path,
        data_root=tmp_path / ".firstcoder",
        provider=provider,
        session_id="sess_manual_compact",
        tools=[],
    )

    first = app.chat_runner.run_user_turn("旧问题 " * 10_000)
    result = app.command_handler.handle("/compact")

    assert first.content == "旧回复"
    assert "Manual compact success" in result.output
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    compact = _compact_events(store, "sess_manual_compact")[0]
    assert compact.payload["trigger"] == "manual"
    event = compact.payload["event"]
    # L1-L3 must not route/trim current plain dialogue. It remains for the
    # semantic L4 checkpoint, rather than becoming the old L3 text preview.
    assert event["changed_parts"] == 0
    assert event["replacements"] == []
    checkpoints = _checkpoint_events(store, "sess_manual_compact")
    assert len(checkpoints) == 1
    assert checkpoints[0].payload["summary"].count("## ") == 7
    assert _llm_compact_events(store, "sess_manual_compact")[-1].payload["status"] == "success"
