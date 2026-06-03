from dataclasses import dataclass, field

import pytest

from firstcoder.app.runtime import AgentChatRunner, CurrentSessionState
from firstcoder.agent.session import AgentSession
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ChatStreamEvent, ToolCall
from firstcoder.tools.types import make_text_result, Tool


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
class FakeStreamingProvider(ChatProvider):
    responses: list[ChatResponse]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake-stream"

    @property
    def model(self) -> str:
        return "fake-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming runtime should not call complete")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        response = self.responses.pop(0)
        yield ChatStreamEvent(kind="message_started")
        if response.content:
            yield ChatStreamEvent(kind="text_delta", text=response.content)
        yield ChatStreamEvent(kind="message_completed", response=response)


@dataclass
class FailingStreamingProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "failing-stream"

    @property
    def model(self) -> str:
        return "failing-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming runtime should not call complete")

    async def astream(self, request: ChatRequest):
        yield ChatStreamEvent(kind="message_started")
        raise RuntimeError("stream failed")


def test_current_session_state_proxies_replaced_session(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    first = AgentSession.create(store=store, session_id="sess_first", agents_md="")
    second = AgentSession.create(store=store, session_id="sess_second", agents_md="")
    state = CurrentSessionState(first)

    state.set_session(second)

    assert state.session_id == "sess_second"
    assert state.runtime_state is second.runtime_state
    assert state.rebuild_view().session_id == "sess_second"


def test_agent_chat_runner_uses_current_session_and_can_follow_resume(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    first = AgentSession.create(store=store, session_id="sess_first", agents_md="")
    second = AgentSession.create(store=store, session_id="sess_second", agents_md="")
    state = CurrentSessionState(first)
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="first reply"),
            ChatResponse(provider="fake", model="fake-model", content="second reply"),
        ]
    )
    runner = AgentChatRunner(current_session=state, provider=provider)

    first_response = runner.run_user_turn("第一轮")
    state.set_session(second)
    second_response = runner.run_user_turn("第二轮")

    assert first_response.content == "first reply"
    assert second_response.content == "second reply"
    assert [message.parts[0].content for message in store.rebuild_session_view("sess_first").messages] == [
        "第一轮",
        "first reply",
    ]
    assert [message.parts[0].content for message in store.rebuild_session_view("sess_second").messages] == [
        "第二轮",
        "second reply",
    ]


def test_agent_chat_runner_records_tool_call_and_result_display_lines(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_tools",
        agents_md="",
        tools=[
            Tool(
                definition=ToolCallEchoDefinition(),
                executor=lambda path: make_text_result("echo_path", f"read {path}"),
            )
        ],
    )
    state = CurrentSessionState(session)
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo_path", arguments={"path": "README.md"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )
    runner = AgentChatRunner(current_session=state, provider=provider)

    response = runner.run_user_turn("读一下")

    assert response.content == "done"
    assert runner.last_display_lines == [
        'Tool call: echo_path {"path": "README.md"}',
        "Tool result: echo_path success: read README.md",
        "done",
    ]


@pytest.mark.anyio
async def test_agent_chat_runner_async_entry_can_use_streaming_loop(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream", agents_md="")
    state = CurrentSessionState(session)
    provider = FakeStreamingProvider(
        [ChatResponse(provider="fake-stream", model="fake-stream-model", content="streamed")]
    )
    runner = AgentChatRunner(current_session=state, provider=provider, use_streaming=True)

    response = await runner.arun_user_turn("你好")

    assert response.content == "streamed"
    assert [event.kind for event in runner.last_stream_events] == [
        "message_started",
        "text_delta",
        "message_completed",
    ]
    assert runner.last_display_lines == ["streamed"]
    assert len(provider.requests) == 1


@pytest.mark.anyio
async def test_agent_chat_runner_streaming_error_clears_stale_display_lines(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_error", agents_md="")
    state = CurrentSessionState(session)
    runner = AgentChatRunner(current_session=state, provider=FailingStreamingProvider(), use_streaming=True)
    runner.last_display_lines = ["old"]
    runner.last_stream_events = [ChatStreamEvent(kind="message_completed")]

    with pytest.raises(RuntimeError):
        await runner.arun_user_turn("你好")

    assert runner.last_display_lines == []
    assert runner.last_stream_events == []


def ToolCallEchoDefinition():
    from firstcoder.providers.types import ToolDefinition

    return ToolDefinition(
        name="echo_path",
        description="回显路径。",
        parameters={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    )
