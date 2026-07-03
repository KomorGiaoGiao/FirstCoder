from dataclasses import dataclass, field

import pytest

from firstcoder.app.runtime import AgentChatRunner, CurrentSessionState, _display_lines_from_messages
from firstcoder.agent.loop import ToolExecutionEvent
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.session import AgentSession
from firstcoder.agent.user_input import AgentTurnStatus
from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.models import AgentMessage, MessagePart
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse, ChatStreamEvent, ProviderDiagnostics, ToolCall
from firstcoder.tools.ask_user import create_ask_user_tool
from firstcoder.tools.write import create_write_tool
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
        if response.diagnostics.reasoning:
            yield ChatStreamEvent(kind="reasoning_delta", text=response.diagnostics.reasoning)
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


def test_chat_runner_passes_loop_limits_to_agent_loop(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_runner_limits", agents_md="")
    state = CurrentSessionState(session)
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])
    limits = AgentLoopLimits(max_tool_rounds=7, max_provider_calls=8, max_turn_seconds=9)
    runner = AgentChatRunner(current_session=state, provider=provider, limits=limits)

    runner.run_user_turn("hi")

    assert runner.loops[-1].limits == limits


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


def test_agent_chat_runner_forwards_tool_execution_events(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_tool_events",
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
    events: list[ToolExecutionEvent] = []
    runner = AgentChatRunner(current_session=state, provider=provider, tool_event_handler=events.append)

    response = runner.run_user_turn("读一下")

    assert response.content == "done"
    assert [event.kind for event in events] == ["started", "finished"]
    assert [event.tool_call.name for event in events] == ["echo_path", "echo_path"]
    assert events[1].result is not None
    assert events[1].result.content == "read README.md"


def test_display_lines_hide_internal_task_boundary_tool() -> None:
    messages = [
        AgentMessage(
            id="msg_assistant",
            session_id="sess_test",
            role="assistant",
            parts=[
                MessagePart(
                    id="part_call",
                    message_id="msg_assistant",
                    kind="tool_call",
                    content="",
                    metadata={
                        "tool_call_id": "call_boundary",
                        "tool_name": "task_boundary",
                        "arguments": {"decision": "new", "basis_message_id": "msg_user"},
                    },
                )
            ],
        ),
        AgentMessage(
            id="msg_tool",
            session_id="sess_test",
            role="tool",
            parts=[
                MessagePart(
                    id="part_result",
                    message_id="msg_tool",
                    kind="tool_result",
                    content="任务边界观察已记录，暂不触发压缩。",
                    metadata={
                        "tool_call_id": "call_boundary",
                        "tool_name": "task_boundary",
                        "ok": True,
                    },
                )
            ],
        ),
    ]

    assert _display_lines_from_messages(messages) == []


def test_agent_chat_runner_exposes_pending_user_input(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_pending",
        agents_md="",
        tools=[create_ask_user_tool()],
    )
    state = CurrentSessionState(session)
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_ask",
                        name="ask_user",
                        arguments={"question": "继续吗？", "options": ["继续", "取消"]},
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    runner = AgentChatRunner(current_session=state, provider=provider)

    response = runner.run_user_turn("先问我")

    assert response.finish_reason == AgentTurnStatus.WAITING_FOR_USER_INPUT.value
    assert response.content == "继续吗？"
    assert runner.last_pending_input is not None
    assert runner.last_pending_input.kind == "ask_user"
    assert runner.last_pending_input.question == "继续吗？"
    assert [option.label for option in runner.last_pending_input.options] == ["继续", "取消"]
    assert runner.last_display_lines == [
        'Tool call: ask_user {"options": ["继续", "取消"], "question": "继续吗？"}',
        "Tool result: ask_user success: 继续吗？ 1. 继续 2. 取消",
        "继续吗？",
    ]


def test_agent_chat_runner_can_resume_permission_confirmation(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_permission_runner",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    state = CurrentSessionState(session)
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
            ChatResponse(provider="fake", model="fake-model", content="写好了"),
        ]
    )
    runner = AgentChatRunner(current_session=state, provider=provider)

    waiting = runner.run_user_turn("写 README")
    assert waiting.finish_reason == AgentTurnStatus.WAITING_FOR_USER_INPUT.value
    assert runner.last_pending_input is not None
    response = runner.resume_with_user_input(runner.last_pending_input.id, "allow_once")

    assert response.content == "写好了"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert runner.last_pending_input is None
    assert runner.last_display_lines == [
        "Tool result: write success: 已写入文件：README.md",
        "写好了",
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
async def test_agent_chat_runner_streaming_forwards_reasoning_events(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_reasoning", agents_md="")
    state = CurrentSessionState(session)
    provider = FakeStreamingProvider(
        [
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
                content="answer",
                diagnostics=ProviderDiagnostics(reasoning="thinking"),
            )
        ]
    )
    seen: list[ChatStreamEvent] = []
    runner = AgentChatRunner(
        current_session=state,
        provider=provider,
        use_streaming=True,
        stream_event_handler=seen.append,
    )

    response = await runner.arun_user_turn("你好")

    assert response.content == "answer"
    assert [event.kind for event in seen] == [
        "message_started",
        "reasoning_delta",
        "text_delta",
        "message_completed",
    ]
    assert [event.kind for event in runner.last_stream_events] == [event.kind for event in seen]
    assert [event.text for event in seen if event.kind == "reasoning_delta"] == ["thinking"]


@pytest.mark.anyio
async def test_agent_chat_runner_streaming_exposes_pending_user_input(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_stream_pending",
        agents_md="",
        tools=[create_ask_user_tool()],
    )
    state = CurrentSessionState(session)
    provider = FakeStreamingProvider(
        [
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_stream_ask",
                        name="ask_user",
                        arguments={"question": "流式继续吗？", "options": ["继续", "暂停"]},
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )
    runner = AgentChatRunner(current_session=state, provider=provider, use_streaming=True)

    response = await runner.arun_user_turn("先流式问我")

    assert response.finish_reason == AgentTurnStatus.WAITING_FOR_USER_INPUT.value
    assert response.content == "流式继续吗？"
    assert runner.last_pending_input is not None
    assert runner.last_pending_input.id == "call_stream_ask"
    assert [option.label for option in runner.last_pending_input.options] == ["继续", "暂停"]
    assert [event.kind for event in runner.last_stream_events] == [
        "message_started",
        "message_completed",
    ]
    assert runner.last_display_lines == [
        'Tool call: ask_user {"options": ["继续", "暂停"], "question": "流式继续吗？"}',
        "Tool result: ask_user success: 流式继续吗？ 1. 继续 2. 暂停",
        "流式继续吗？",
    ]


@pytest.mark.anyio
async def test_agent_chat_runner_streaming_resume_permission_uses_streaming(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_stream_permission",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    state = CurrentSessionState(session)
    provider = FakeStreamingProvider(
        [
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
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
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="写好了"),
        ]
    )
    runner = AgentChatRunner(current_session=state, provider=provider, use_streaming=True)

    waiting = await runner.arun_user_turn("写 README")
    assert waiting.finish_reason == AgentTurnStatus.WAITING_FOR_USER_INPUT.value
    assert runner.last_pending_input is not None
    response = await runner.aresume_with_user_input(runner.last_pending_input.id, "allow_once")

    assert response.content == "写好了"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert len(provider.requests) == 2
    assert [event.kind for event in runner.last_stream_events] == [
        "message_started",
        "text_delta",
        "message_completed",
    ]


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
