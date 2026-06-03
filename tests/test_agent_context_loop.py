from __future__ import annotations

from dataclasses import dataclass, field
import re

import pytest

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.context.manager import ContextCompactResult, ContextWindowTrigger
from firstcoder.context.runtime_replay import replay_runtime_state
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderDiagnostics,
    ProviderCapabilities,
    ToolCall,
    ToolDefinition,
)
from firstcoder.tools.task_boundary import create_task_boundary_tool
from firstcoder.tools.types import Tool, ToolResult


@dataclass
class FakeProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response


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


@dataclass
class FakeContextManager:
    results: list[ContextCompactResult] = field(default_factory=list)
    calls: list[object] = field(default_factory=list)

    def compact_if_needed(self, request):
        self.calls.append(request)
        if self.results:
            return self.results.pop(0)
        return ContextCompactResult(
            status="skipped",
            reason="under_threshold",
            view=request.view,
            before_tokens=0,
            after_tokens=0,
        )


@dataclass
class RecordingContextManager:
    calls: list[object] = field(default_factory=list)
    status: str = "skipped"
    reason: str = "under_threshold"

    def compact_if_needed(self, request):
        self.calls.append(request)
        return ContextCompactResult(
            status=self.status,
            reason=self.reason,
            view=request.view,
            before_tokens=0,
            after_tokens=0,
        )


@dataclass
class PromptTooLongSuccessContextManager(RecordingContextManager):
    def compact_if_needed(self, request):
        self.calls.append(request)
        status = "success" if request.trigger == ContextWindowTrigger.PROMPT_TOO_LONG else "skipped"
        reason = request.trigger.value if request.trigger == ContextWindowTrigger.PROMPT_TOO_LONG else "under_threshold"
        return ContextCompactResult(
            status=status,
            reason=reason,
            view=request.view,
            before_tokens=100,
            after_tokens=10,
        )


@dataclass
class StreamingProvider(ChatProvider):
    responses: list[ChatResponse | ProviderError]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake-stream"

    @property
    def model(self) -> str:
        return "fake-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming test should not call complete")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        yield ChatStreamEvent(kind="message_started")
        if response.content:
            for text in response.content:
                yield ChatStreamEvent(kind="text_delta", text=text)
        for tool_call in response.tool_calls:
            yield ChatStreamEvent(kind="tool_call_started", tool_call_id=tool_call.id, tool_name=tool_call.name)
            yield ChatStreamEvent(kind="tool_call_delta", tool_call_id=tool_call.id, tool_name=tool_call.name)
            yield ChatStreamEvent(kind="tool_call_completed", tool_call=tool_call)
        yield ChatStreamEvent(kind="message_completed", response=response)


@dataclass
class ObservingStreamingProvider(ChatProvider):
    response: ChatResponse
    session: AgentSession
    tool_results_before_message_completed: int | None = None
    calls: int = 0

    @property
    def name(self) -> str:
        return "observing-stream"

    @property
    def model(self) -> str:
        return "observing-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming test should not call complete")

    async def astream(self, request: ChatRequest):
        self.calls += 1
        if self.calls > 1:
            final_response = ChatResponse(provider=self.name, model=self.model, content="完成")
            yield ChatStreamEvent(kind="message_started")
            yield ChatStreamEvent(kind="message_completed", response=final_response)
            return

        yield ChatStreamEvent(kind="message_started")
        tool_call = self.response.tool_calls[0]
        yield ChatStreamEvent(kind="tool_call_started", tool_call_id=tool_call.id, tool_name=tool_call.name)
        yield ChatStreamEvent(kind="tool_call_completed", tool_call=tool_call)
        self.tool_results_before_message_completed = len(
            [message for message in self.session.rebuild_view().messages if message.role == "tool"]
        )
        yield ChatStreamEvent(kind="message_completed", response=self.response)


@dataclass
class IncompleteStreamingProvider(ChatProvider):
    @property
    def name(self) -> str:
        return "incomplete-stream"

    @property
    def model(self) -> str:
        return "incomplete-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming test should not call complete")

    async def astream(self, request: ChatRequest):
        yield ChatStreamEvent(kind="message_started")
        yield ChatStreamEvent(kind="text_delta", text="partial")


@dataclass
class PartialThenErrorStreamingProvider(ChatProvider):
    error: ProviderError
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "partial-error-stream"

    @property
    def model(self) -> str:
        return "partial-error-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming test should not call complete")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        yield ChatStreamEvent(kind="message_started")
        yield ChatStreamEvent(kind="text_delta", text="partial")
        raise self.error


@dataclass
class PartialPromptTooLongThenSuccessStreamingProvider(ChatProvider):
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "partial-retry-stream"

    @property
    def model(self) -> str:
        return "partial-retry-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming test should not call complete")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        if len(self.requests) == 1:
            yield ChatStreamEvent(kind="message_started")
            yield ChatStreamEvent(kind="text_delta", text="partial")
            raise ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long")

        response = ChatResponse(provider=self.name, model=self.model, content="ok")
        yield ChatStreamEvent(kind="message_started")
        yield ChatStreamEvent(kind="text_delta", text="ok")
        yield ChatStreamEvent(kind="message_completed", response=response)


@dataclass
class PartialPromptTooLongThenPartialPromptTooLongStreamingProvider(ChatProvider):
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "partial-retry-fail-stream"

    @property
    def model(self) -> str:
        return "partial-retry-fail-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        raise AssertionError("streaming test should not call complete")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        yield ChatStreamEvent(kind="message_started")
        yield ChatStreamEvent(kind="text_delta", text=f"partial-{len(self.requests)}")
        raise ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long")


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


def test_agent_loop_persists_provider_diagnostics_metadata(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                finish_reason="tool_calls",
                diagnostics=ProviderDiagnostics(
                    raw_finish_reason="tool_calls",
                    warnings=["tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用"],
                ),
            )
        ]
    )

    AgentLoop(session=session, provider=provider).run_user_turn("读取 README")

    assistant = [message for message in store.rebuild_session_view("sess_test").messages if message.role == "assistant"][0]
    assert assistant.metadata["diagnostics"]["raw_finish_reason"] == "tool_calls"
    assert assistant.metadata["diagnostics"]["warnings"]


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
    assert view.messages[0].parts[0].metadata["created_turn"] == 1
    assert view.messages[0].parts[0].metadata["turn_id"] == 1
    assert view.messages[1].parts[0].metadata["created_turn"] == 1
    assert view.messages[1].parts[0].metadata["turn_id"] == 1


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


def test_agent_loop_system_prefix_uses_provider_model_and_default_permission_policy(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="AGENTS 规则")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("问题")

    system_prompt = provider.requests[0].messages[0].content
    assert '"model": "fake-model"' in system_prompt
    assert '"path_access": "project_root_only"' in system_prompt
    assert '"env_secrets": "redact"' in system_prompt


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
    assert view.messages[0].parts[0].metadata["created_turn"] == 1
    assert view.messages[1].parts[0].metadata["created_turn"] == 1
    assert view.messages[2].parts[0].metadata["created_turn"] == 1


def test_agent_loop_streaming_text_persists_final_assistant_message(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream", agents_md="")
    provider = StreamingProvider([ChatResponse(provider="fake-stream", model="fake-stream-model", content="你好")])
    loop = AgentLoop(session=session, provider=provider)

    result = loop.run_user_turn_streaming_sync("你好")

    assert result.content == "你好"
    assert [event.kind for event in loop.last_stream_events] == [
        "message_started",
        "text_delta",
        "text_delta",
        "message_completed",
    ]
    view = store.rebuild_session_view("sess_stream")
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[1].parts[0].content == "你好"


def test_agent_loop_streaming_tool_call_executes_after_message_completed(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_tool", agents_md="")
    provider = StreamingProvider(
        [
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "abc"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="完成"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider, tools=[_echo_tool()])

    result = loop.run_user_turn_streaming_sync("调用工具")

    assert result.content == "完成"
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-2].role == "assistant"
    assert provider.requests[1].messages[-1].role == "tool"
    view = store.rebuild_session_view("sess_stream_tool")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    assert view.messages[1].parts[0].metadata["tool_call_id"] == "call_1"
    assert view.messages[2].parts[0].metadata["tool_call_id"] == "call_1"


def test_agent_loop_streaming_does_not_execute_tool_before_message_completed(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_atomic", agents_md="")
    response = ChatResponse(
        provider="observing-stream",
        model="observing-stream-model",
        content="",
        tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "abc"})],
        finish_reason="tool_calls",
    )
    provider = ObservingStreamingProvider(response=response, session=session)

    AgentLoop(session=session, provider=provider, tools=[_echo_tool()]).run_user_turn_streaming_sync("调用工具")

    assert provider.tool_results_before_message_completed == 0
    view = store.rebuild_session_view("sess_stream_atomic")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]


def test_agent_loop_streaming_incomplete_message_does_not_persist_assistant(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_incomplete", agents_md="")

    with pytest.raises(ProviderError) as exc_info:
        AgentLoop(session=session, provider=IncompleteStreamingProvider()).run_user_turn_streaming_sync("你好")

    assert exc_info.value.kind == ProviderErrorKind.API_ERROR
    view = store.rebuild_session_view("sess_stream_incomplete")
    assert [message.role for message in view.messages] == ["user"]


def test_agent_loop_streaming_retries_once_after_prompt_too_long_compaction(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_retry", agents_md="")
    provider = StreamingProvider(
        [
            ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long"),
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="ok"),
        ]
    )
    context_manager = PromptTooLongSuccessContextManager()

    result = AgentLoop(session=session, provider=provider, context_manager=context_manager).run_user_turn_streaming_sync(
        "问题"
    )

    assert result.content == "ok"
    assert len(provider.requests) == 2
    assert [call.trigger for call in context_manager.calls] == [
        ContextWindowTrigger.AUTO,
        ContextWindowTrigger.PROMPT_TOO_LONG,
        ContextWindowTrigger.AUTO,
    ]


def test_agent_loop_streaming_prompt_too_long_retry_discards_failed_attempt_events(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_retry_events", agents_md="")
    provider = PartialPromptTooLongThenSuccessStreamingProvider()
    context_manager = PromptTooLongSuccessContextManager()
    loop = AgentLoop(session=session, provider=provider, context_manager=context_manager)

    result = loop.run_user_turn_streaming_sync("问题")

    assert result.content == "ok"
    assert len(provider.requests) == 2
    assert [event.kind for event in loop.last_stream_events] == [
        "message_started",
        "text_delta",
        "message_completed",
    ]
    assert [event.text for event in loop.last_stream_events if event.kind == "text_delta"] == ["ok"]


def test_agent_loop_streaming_ignores_returned_tool_calls_when_provider_without_tool_support(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_no_tool_exec", agents_md="")
    provider = StreamingProvider(
        [
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
                content="",
                tool_calls=[ToolCall(id="call_1", name="think", arguments={"thought": "x"})],
                finish_reason="tool_calls",
            )
        ],
        capabilities=ProviderCapabilities(supports_tools=False),
    )

    loop = AgentLoop(session=session, provider=provider)
    response = loop.run_user_turn_streaming_sync("问题")

    assert provider.requests[0].tools == []
    assert response.tool_calls == []
    assert response.finish_reason == "error"
    assert "tool calls were ignored" in response.diagnostics.warnings[0]
    assert [event.kind for event in loop.last_stream_events] == ["message_started", "message_completed"]
    assert [message.role for message in store.rebuild_session_view("sess_stream_no_tool_exec").messages] == [
        "user",
        "assistant",
    ]


def test_agent_loop_streaming_second_prompt_too_long_discards_retry_attempt_events(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_retry_events_fail", agents_md="")
    provider = PartialPromptTooLongThenPartialPromptTooLongStreamingProvider()
    context_manager = PromptTooLongSuccessContextManager()
    loop = AgentLoop(session=session, provider=provider, context_manager=context_manager)

    with pytest.raises(ProviderError) as exc_info:
        loop.run_user_turn_streaming_sync("问题")

    assert exc_info.value.kind == ProviderErrorKind.PROMPT_TOO_LONG
    assert len(provider.requests) == 2
    assert loop.last_stream_events == []


def test_agent_loop_streaming_prompt_too_long_does_not_retry_when_compaction_fails(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_retry_fail", agents_md="")
    provider = StreamingProvider([ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long")])
    context_manager = RecordingContextManager(status="failed", reason="l4_service_missing")

    with pytest.raises(ProviderError) as exc_info:
        AgentLoop(session=session, provider=provider, context_manager=context_manager).run_user_turn_streaming_sync(
            "问题"
        )

    assert exc_info.value.kind == ProviderErrorKind.PROMPT_TOO_LONG
    assert len(provider.requests) == 1
    assert [call.trigger for call in context_manager.calls] == [
        ContextWindowTrigger.AUTO,
        ContextWindowTrigger.PROMPT_TOO_LONG,
    ]
    assert [message.role for message in store.rebuild_session_view("sess_stream_retry_fail").messages] == ["user"]


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


def test_agent_loop_omits_tools_for_provider_without_tool_support(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_no_tools", agents_md="")
    provider = FakeProvider(
        [ChatResponse(provider="fake", model="fake-model", content="ok")],
        capabilities=ProviderCapabilities(supports_tools=False),
    )

    AgentLoop(session=session, provider=provider).run_user_turn("问题")

    assert provider.requests[0].tools == []
    system_message = provider.requests[0].messages[0].content
    assert '"tool_calling": false' in system_message
    assert "可用工具:\n无" in system_message


def test_agent_loop_ignores_returned_tool_calls_when_provider_without_tool_support(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_no_tool_exec", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_1", name="think", arguments={"thought": "x"})],
                finish_reason="tool_calls",
            )
        ],
        capabilities=ProviderCapabilities(supports_tools=False),
    )

    response = AgentLoop(session=session, provider=provider).run_user_turn("问题")

    assert response.tool_calls == []
    assert response.finish_reason == "error"
    assert "tool calls were ignored" in response.diagnostics.warnings[0]
    assert [message.role for message in store.rebuild_session_view("sess_no_tool_exec").messages] == [
        "user",
        "assistant",
    ]


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


def test_agent_loop_passes_current_turn_into_context_manager(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])
    context_manager = PromptTooLongSuccessContextManager()

    AgentLoop(session=session, provider=provider, context_manager=context_manager).run_user_turn("新任务")

    assert context_manager.calls
    assert context_manager.calls[0].current_turn == 1


def test_agent_loop_retries_once_after_prompt_too_long_compaction(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_retry", agents_md="")
    provider = FakeProvider(
        [
            ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long"),
            ChatResponse(provider="fake", model="fake-model", content="ok"),
        ]
    )
    context_manager = PromptTooLongSuccessContextManager()

    result = AgentLoop(session=session, provider=provider, context_manager=context_manager).run_user_turn("问题")

    assert result.content == "ok"
    assert len(provider.requests) == 2
    assert [call.trigger for call in context_manager.calls] == [
        ContextWindowTrigger.AUTO,
        ContextWindowTrigger.PROMPT_TOO_LONG,
        ContextWindowTrigger.AUTO,
    ]


def test_agent_loop_prompt_too_long_retries_only_once(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_retry_once", agents_md="")
    provider = FakeProvider(
        [
            ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long"),
            ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "still too long"),
        ]
    )
    context_manager = PromptTooLongSuccessContextManager()

    with pytest.raises(ProviderError) as exc_info:
        AgentLoop(session=session, provider=provider, context_manager=context_manager).run_user_turn("问题")

    assert exc_info.value.kind == ProviderErrorKind.PROMPT_TOO_LONG
    assert len(provider.requests) == 2
    assert [call.trigger for call in context_manager.calls] == [
        ContextWindowTrigger.AUTO,
        ContextWindowTrigger.PROMPT_TOO_LONG,
    ]
    assert [message.role for message in store.rebuild_session_view("sess_retry_once").messages] == ["user"]


def test_agent_loop_prompt_too_long_does_not_retry_when_compaction_fails(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_retry_fail", agents_md="")
    provider = FakeProvider([ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long")])
    context_manager = RecordingContextManager(status="failed", reason="l4_service_missing")

    with pytest.raises(ProviderError) as exc_info:
        AgentLoop(session=session, provider=provider, context_manager=context_manager).run_user_turn("问题")

    assert exc_info.value.kind == ProviderErrorKind.PROMPT_TOO_LONG
    assert len(provider.requests) == 1
    assert [call.trigger for call in context_manager.calls] == [
        ContextWindowTrigger.AUTO,
        ContextWindowTrigger.PROMPT_TOO_LONG,
    ]
    assert [message.role for message in store.rebuild_session_view("sess_retry_fail").messages] == ["user"]


def test_agent_loop_does_not_retry_non_compaction_provider_error(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_no_retry", agents_md="")
    provider = FakeProvider([ProviderError(ProviderErrorKind.AUTH_ERROR, "bad key")])
    context_manager = RecordingContextManager()

    with pytest.raises(ProviderError) as exc_info:
        AgentLoop(session=session, provider=provider, context_manager=context_manager).run_user_turn("问题")

    assert exc_info.value.kind == ProviderErrorKind.AUTH_ERROR
    assert len(provider.requests) == 1
    assert [call.trigger for call in context_manager.calls] == [ContextWindowTrigger.AUTO]


def test_agent_loop_streaming_does_not_retry_non_compaction_provider_error(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_no_retry", agents_md="")
    provider = PartialThenErrorStreamingProvider(ProviderError(ProviderErrorKind.AUTH_ERROR, "bad key"))
    context_manager = RecordingContextManager()
    loop = AgentLoop(session=session, provider=provider, context_manager=context_manager)

    with pytest.raises(ProviderError) as exc_info:
        loop.run_user_turn_streaming_sync("问题")

    assert exc_info.value.kind == ProviderErrorKind.AUTH_ERROR
    assert len(provider.requests) == 1
    assert loop.last_stream_events == []
    assert [call.trigger for call in context_manager.calls] == [ContextWindowTrigger.AUTO]
    assert [message.role for message in store.rebuild_session_view("sess_stream_no_retry").messages] == ["user"]


def test_agent_loop_resume_keeps_turn_counter_and_metadata(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    original = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    first_provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="第一轮")])
    AgentLoop(session=original, provider=first_provider).run_user_turn("第一轮问题")

    resumed = AgentSession.resume(store=store, session_id="sess_test", agents_md="")
    second_provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="第二轮")])
    AgentLoop(session=resumed, provider=second_provider).run_user_turn("第二轮问题")

    view = store.rebuild_session_view("sess_test")
    assert view.messages[0].parts[0].metadata["created_turn"] == 1
    assert view.messages[1].parts[0].metadata["created_turn"] == 1
    assert view.messages[2].parts[0].metadata["created_turn"] == 2
    assert view.messages[3].parts[0].metadata["created_turn"] == 2


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


def test_agent_session_resume_replays_runtime_state_and_known_message_ids(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    original = AgentSession.create(store=store, session_id="sess_test", agents_md="rules")
    message_id = original.append_user_message("历史消息")
    tool_call = ToolCall(
        id="call_boundary",
        name="task_boundary",
        arguments={"decision": "new", "basis_message_id": message_id},
    )
    first = original.execute_tool_call(tool_call)
    original.append_tool_result(tool_call=tool_call, result=first)
    second = original.execute_tool_call(tool_call)
    original.append_tool_result(tool_call=tool_call, result=second)

    resumed = AgentSession.resume(store=store, session_id="sess_test", agents_md="rules")
    result = resumed.tool_registry.execute(
        "task_boundary",
        {"decision": "same", "basis_message_id": message_id},
    )

    assert resumed.runtime_state.active_task_hash == original.runtime_state.active_task_hash
    assert message_id in resumed.known_message_ids
    assert result.ok is True


def test_agent_loop_runs_compact_when_task_boundary_confirms_change(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = BoundaryProvider()
    context_manager = FakeContextManager()

    AgentLoop(
        session=session,
        provider=provider,
        context_manager=context_manager,
    ).run_user_turn("换一个任务")

    triggers = [call.trigger for call in context_manager.calls]
    assert ContextWindowTrigger.TASK_HASH_CHANGED in triggers


def test_agent_loop_runs_auto_compact_after_large_tool_result(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "large"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
        ]
    )

    def large_echo(text: str) -> ToolResult:
        return ToolResult(name="echo", ok=True, content="large output\n" * 400)

    tool = Tool(
        definition=ToolDefinition(
            name="echo",
            description="大输出",
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        ),
        executor=large_echo,
    )
    context_manager = FakeContextManager()

    AgentLoop(
        session=session,
        provider=provider,
        tools=[tool],
        context_manager=context_manager,
    ).run_user_turn("调用大工具")

    triggers = [call.trigger for call in context_manager.calls]
    assert ContextWindowTrigger.AUTO in triggers
