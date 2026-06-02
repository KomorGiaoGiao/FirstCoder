from __future__ import annotations

from dataclasses import dataclass, field
import re

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.runtime_replay import replay_runtime_state
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall, ToolDefinition
from firstcoder.tools.task_boundary import create_task_boundary_tool
from firstcoder.tools.types import Tool, ToolResult


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
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


@dataclass
class BoundaryProvider(ChatProvider):
    requests: list[ChatRequest] = field(default_factory=list)
    boundary_calls: int = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self.boundary_calls >= 2:
            return ChatResponse(provider="fake", model="fake-model", content="ok")

        basis_message_id = _extract_basis_message_id(request)
        self.boundary_calls += 1
        return ChatResponse(
            provider="fake",
            model="fake-model",
            content="",
            tool_calls=[
                ToolCall(
                    id=f"call_boundary_{self.boundary_calls}",
                    name="task_boundary",
                    arguments={"decision": "new", "basis_message_id": basis_message_id},
                )
            ],
            finish_reason="tool_calls",
        )


def _echo_tool() -> Tool:
    def execute(text: str) -> ToolResult:
        return ToolResult(name="echo", ok=True, content=f"echo:{text}")

    return Tool(
        definition=ToolDefinition(
            name="echo",
            description="回显文本",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        executor=execute,
    )


def _extract_basis_message_id(request: ChatRequest) -> str:
    for message in reversed(request.messages):
        match = re.search(r"basis_message_id=([A-Za-z0-9_]+)", message.content)
        if match:
            return match.group(1)
    raise AssertionError("request did not expose basis_message_id")


def test_agent_loop_appends_user_and_assistant_messages(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="项目规则")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="收到")])

    result = AgentLoop(session=session, provider=provider).run_user_turn("你好")

    assert result.content == "收到"
    view = store.rebuild_session_view("sess_test")
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[0].parts[0].content == "你好"
    assert view.messages[1].parts[0].content == "收到"


def test_agent_loop_builds_context_with_system_prefix_without_storing_it(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="AGENTS 规则")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("问题")

    request = provider.requests[0]
    assert request.messages[0].role == "system"
    assert "AGENTS 规则" in request.messages[0].content
    assert request.messages[1].role == "user"
    assert "问题" in request.messages[1].content

    view = store.rebuild_session_view("sess_test")
    assert all(message.role != "system" for message in view.messages)
    assert session.runtime_state.system_prompt_fingerprint is not None


def test_agent_loop_exposes_user_message_id_for_task_boundary(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("新需求")

    user_message_id = store.rebuild_session_view("sess_test").messages[0].id
    request_user_message = provider.requests[0].messages[-1]
    assert request_user_message.role == "user"
    assert f"basis_message_id={user_message_id}" in request_user_message.content
    assert "新需求" in request_user_message.content


def test_agent_loop_executes_tool_call_and_appends_tool_result(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "abc"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
        ]
    )

    result = AgentLoop(session=session, provider=provider, tools=[_echo_tool()]).run_user_turn("调用工具")

    assert result.content == "完成"
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-2].role == "assistant"
    assert provider.requests[1].messages[-2].tool_calls[0].id == "call_1"
    assert provider.requests[1].messages[-1].role == "tool"
    assert provider.requests[1].messages[-1].tool_call_id == "call_1"
    assert provider.requests[1].messages[-1].content == "echo:abc"

    view = store.rebuild_session_view("sess_test")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    assert view.messages[1].parts[0].kind == "tool_call"
    assert view.messages[2].parts[0].metadata["tool_call_id"] == "call_1"


def test_agent_loop_injects_stateful_task_boundary_tool(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("新问题")

    tools = provider.requests[0].tools
    user_message_id = store.rebuild_session_view("sess_test").messages[0].id
    assert "task_boundary" in [tool.name for tool in tools]
    result = session.tool_registry.execute(
        "task_boundary",
        {"decision": "new", "basis_message_id": user_message_id},
    )
    assert result.ok
    assert result.data["candidate_hash"].startswith("task_")


def test_agent_loop_persists_task_boundary_observation_for_replay(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = BoundaryProvider()

    AgentLoop(session=session, provider=provider).run_user_turn("换一个任务")

    event_types = [event.type for event in store.list_events("sess_test")]
    replayed = replay_runtime_state(store, "sess_test")
    assert "task_boundary_observed" in event_types
    assert session.runtime_state.active_task_hash is not None
    assert replayed.active_task_hash == session.runtime_state.active_task_hash


def test_agent_loop_rejects_task_boundary_unknown_basis_message_id(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_boundary",
                        name="task_boundary",
                        arguments={"decision": "new", "basis_message_id": "msg_not_in_context"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="ok"),
        ]
    )

    AgentLoop(session=session, provider=provider).run_user_turn("新任务")

    view = store.rebuild_session_view("sess_test")
    tool_result = next(message for message in view.messages if message.role == "tool").parts[0]
    event_types = [event.type for event in store.list_events("sess_test")]
    replayed = replay_runtime_state(store, "sess_test")
    assert tool_result.metadata["ok"] is False
    assert "basis_message_id 不属于当前 session" in tool_result.content
    assert "task_boundary_observed" not in event_types
    assert session.runtime_state.active_task_hash is None
    assert replayed.active_task_hash is None


def test_task_boundary_tool_result_append_preserves_stable_window_metadata(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    basis_message_id = session.append_user_message("新任务")
    tool = create_task_boundary_tool(session.runtime_state, required_stable_count=3)
    tool_call = ToolCall(
        id="call_boundary",
        name="task_boundary",
        arguments={"decision": "new", "basis_message_id": basis_message_id},
    )

    result = tool.executor(decision="new", basis_message_id=basis_message_id)
    session.append_tool_result(tool_call=tool_call, result=result)

    event = next(event for event in store.list_events("sess_test") if event.type == "task_boundary_observed")
    assert result.data["required_stable_count"] == 3
    assert result.data["event_version"]
    assert result.data["strategy_version"]
    assert result.data["created_at"]
    assert event.payload["required_stable_count"] == 3
    assert event.payload["event_version"] == result.data["event_version"]
    assert event.payload["strategy_version"] == result.data["strategy_version"]
    assert event.payload["created_at"] == result.data["created_at"]


def test_agent_loop_does_not_persist_unexecuted_tool_calls_after_round_limit(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "first"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_2", name="echo", arguments={"text": "second"})],
                finish_reason="tool_calls",
            ),
        ]
    )

    result = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        max_tool_rounds=1,
    ).run_user_turn("连续工具")

    assert not result.tool_calls
    assert "工具调用轮次达到上限" in result.content
    view = store.rebuild_session_view("sess_test")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    assert view.messages[1].parts[0].metadata["tool_call_id"] == "call_1"
    assert view.messages[2].parts[0].metadata["tool_call_id"] == "call_1"
    assert view.messages[3].parts[0].kind == "text"
    assert all(part.kind != "tool_call" for part in view.messages[3].parts)
