from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
import threading
import time

import pytest

from firstcoder.agent.loop import AgentLoop, ToolExecutionEvent
from firstcoder.agent import loop as agent_loop
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.user_input import AgentTurnStatus
from firstcoder.agent.session import AgentSession
from firstcoder.context.manager import ContextCompactResult, ContextWindowTrigger
from firstcoder.context.runtime_replay import replay_runtime_state
from firstcoder.context.store import JsonlSessionStore
from firstcoder.input.attachments import attach_path
from firstcoder.runtime.cancellation import CancellationToken
from firstcoder.permissions.types import PermissionMode
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ProviderDiagnostics,
    ProviderCapabilities,
    ToolCall,
    ToolChoiceFunction,
    ToolDefinition,
)
from firstcoder.tools.task_boundary import create_task_boundary_tool
from firstcoder.tools.ask_user import create_ask_user_tool
from firstcoder.tools.todo import create_todo_tool
from firstcoder.tools.write import create_write_tool
from firstcoder.tools.edit import create_edit_tool
from firstcoder.tools.shell import create_shell_tool
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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(
                provider="fake",
                model="fake-model",
                content='{"decision":"uncertain","basis_message_id":"' + _extract_basis_message_id(request) + '"}',
            )
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response


@dataclass
class CancellingProvider(FakeProvider):
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)

    def complete(self, request: ChatRequest) -> ChatResponse:
        response = super().complete(request)
        if len(self.requests) == 2:
            self.cancellation_token.cancel()
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
class JsonBoundaryProvider(ChatProvider):
    responses: list[str]
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        content = self.responses.pop(0)
        if content in {"<boundary>", "<same>"}:
            decision = "new" if content == "<boundary>" else "same"
            content = '{"decision":"' + decision + '","basis_message_id":"' + _extract_basis_message_id(request) + '"}'
        return ChatResponse(provider="fake", model="fake-model", content=content)


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


class FakeClock:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    def __call__(self) -> float:
        if not self.values:
            return 999.0
        return self.values.pop(0)


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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(
                provider=self.name,
                model=self.model,
                content='{"decision":"uncertain","basis_message_id":"' + _extract_basis_message_id(request) + '"}',
            )
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
class StreamingProviderWithClassification(ChatProvider):
    classification_responses: list[str]
    stream_responses: list[ChatResponse]
    complete_requests: list[ChatRequest] = field(default_factory=list)
    stream_requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake-stream"

    @property
    def model(self) -> str:
        return "fake-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.complete_requests.append(request)
        content = self.classification_responses.pop(0)
        if content == "<boundary>":
            content = '{"decision":"new","basis_message_id":"' + _extract_basis_message_id(request) + '"}'
        return ChatResponse(provider=self.name, model=self.model, content=content)

    async def astream(self, request: ChatRequest):
        self.stream_requests.append(request)
        response = self.stream_responses.pop(0)
        yield ChatStreamEvent(kind="message_started")
        for text in response.content:
            yield ChatStreamEvent(kind="text_delta", text=text)
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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(provider=self.name, model=self.model, content='{"decision":"uncertain","basis_message_id":"' + _extract_basis_message_id(request) + '"}')
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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(provider=self.name, model=self.model, content='{"decision":"uncertain","basis_message_id":"' + _extract_basis_message_id(request) + '"}')
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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(provider=self.name, model=self.model, content='{"decision":"uncertain","basis_message_id":"' + _extract_basis_message_id(request) + '"}')
        raise AssertionError("streaming test should not call complete")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        yield ChatStreamEvent(kind="message_started")
        yield ChatStreamEvent(kind="text_delta", text="partial")
        raise self.error


@dataclass
class FallbackStreamingProvider(ChatProvider):
    stream_errors: list[ProviderError]
    complete_response: ChatResponse
    stream_requests: list[ChatRequest] = field(default_factory=list)
    complete_requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fallback-stream"

    @property
    def model(self) -> str:
        return "fallback-stream-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.complete_requests.append(request)
        return self.complete_response

    async def astream(self, request: ChatRequest):
        self.stream_requests.append(request)
        yield ChatStreamEvent(kind="message_started")
        yield ChatStreamEvent(kind="text_delta", text="partial")
        raise self.stream_errors.pop(0)


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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(provider=self.name, model=self.model, content='{"decision":"uncertain","basis_message_id":"' + _extract_basis_message_id(request) + '"}')
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
        if request.tools == [] and request.tool_choice == "none" and request.max_tokens == 512:
            return ChatResponse(provider=self.name, model=self.model, content='{"decision":"uncertain","basis_message_id":"' + _extract_basis_message_id(request) + '"}')
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


def _slow_named_tool(name: str, *, delay: float) -> Tool:
    def execute(text: str) -> ToolResult:
        time.sleep(delay)
        return ToolResult(name=name, ok=True, content=f"{name}:{text}")

    return Tool(
        definition=ToolDefinition(
            name=name,
            description=f"slow {name}",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        executor=execute,
    )


def _success_tool() -> Tool:
    definition = ToolDefinition(
        name="shell",
        description="fake shell",
        parameters={"type": "object", "properties": {}},
    )

    def execute(**kwargs):
        return ToolResult(
            name="shell",
            ok=True,
            content="3 passed",
            data={"command": "pytest -q", "exit_code": 0, "stdout": "3 passed", "stderr": ""},
        )

    return Tool(definition=definition, executor=execute)


def _failed_test_tool() -> Tool:
    definition = ToolDefinition(
        name="shell",
        description="fake shell",
        parameters={"type": "object", "properties": {}},
    )

    def execute(**kwargs):
        return ToolResult(
            name="shell",
            ok=False,
            content="1 failed",
            data={"command": "pytest -q", "exit_code": 1, "stdout": "", "stderr": "1 failed"},
            error="命令退出码为 1",
        )

    return Tool(definition=definition, executor=execute)


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


def test_agent_loop_projects_image_attachment_into_provider_request(tmp_path) -> None:
    image = tmp_path / "image.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    )
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.create(store=store, session_id="sess_image_request")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="收到图片")])

    AgentLoop(session=session, provider=provider).run_user_turn(
        "描述图片",
        attachments=[attach_path(image)],
    )

    request = provider.requests[0]
    user_message = next(message for message in request.messages if message.role == "user")
    assert user_message.content_parts is not None
    image_part = next(part for part in user_message.content_parts if part.type == "image")
    assert image_part.media_type == "image/png"
    assert image_part.data_base64


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


@pytest.mark.parametrize("streaming", [False, True])
def test_sync_and_streaming_tool_loops_persist_equivalent_terminal_state(tmp_path, streaming) -> None:
    store = JsonlSessionStore(tmp_path / str(streaming))
    session = AgentSession.create(store=store, session_id="sess_parity", agents_md="")
    responses = [
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="",
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "abc"})],
            finish_reason="tool_calls",
        ),
        ChatResponse(provider="fake", model="fake-model", content="完成"),
    ]
    provider = StreamingProvider(responses) if streaming else FakeProvider(responses)
    loop = AgentLoop(session=session, provider=provider, tools=[_echo_tool()])

    response = (
        loop.run_user_turn_streaming_sync("调用工具")
        if streaming
        else loop.run_user_turn("调用工具")
    )

    view = session.rebuild_view()
    assert response.content == "完成"
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    assert view.messages[1].parts[0].metadata["tool_call_id"] == "call_1"
    assert session.pending_permission_execution is None
    assert view.messages[2].parts[0].metadata["tool_call_id"] == "call_1"
    assert view.messages[0].parts[0].metadata["created_turn"] == 1
    assert view.messages[1].parts[0].metadata["created_turn"] == 1
    assert view.messages[2].parts[0].metadata["created_turn"] == 1


def test_agent_loop_runs_readonly_tool_calls_in_parallel_and_appends_results_in_order(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_parallel_readonly", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(id="call_slow_first", name="view", arguments={"text": "first"}),
                    ToolCall(id="call_slow_second", name="grep", arguments={"text": "second"}),
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
        ]
    )
    tool_events: list[ToolExecutionEvent] = []
    loop = AgentLoop(
        session=session,
        provider=provider,
        tools=[_slow_named_tool("view", delay=0.2), _slow_named_tool("grep", delay=0.2)],
        tool_event_handler=tool_events.append,
    )

    started_at = time.perf_counter()
    result = loop.run_user_turn("并发读")
    elapsed = time.perf_counter() - started_at

    assert result.content == "完成"
    assert elapsed < 0.35
    assert [event.kind for event in tool_events] == ["started", "started", "finished", "finished"]
    view = store.rebuild_session_view("sess_parallel_readonly")
    tool_messages = [message for message in view.messages if message.role == "tool"]
    assert [message.parts[0].metadata["tool_call_id"] for message in tool_messages] == [
        "call_slow_first",
        "call_slow_second",
    ]
    assert [message.parts[0].content for message in tool_messages] == ["view:first", "grep:second"]


def test_agent_loop_runs_bypass_allowed_tool_calls_in_parallel(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_parallel_bypass",
        project_root=tmp_path,
        tools=[_slow_named_tool("shell", delay=0.2), _slow_named_tool("python_exec", delay=0.2)],
    )
    session.set_permission_mode(PermissionMode.BYPASS)
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(id="call_shell", name="shell", arguments={"text": "first"}),
                    ToolCall(id="call_python", name="python_exec", arguments={"text": "second"}),
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
        ]
    )
    tool_events: list[ToolExecutionEvent] = []
    loop = AgentLoop(
        session=session,
        provider=provider,
        tool_event_handler=tool_events.append,
    )

    started_at = time.perf_counter()
    result = loop.run_user_turn("bypass 并发")
    elapsed = time.perf_counter() - started_at

    assert result.content == "完成"
    assert elapsed < 0.35
    assert [event.kind for event in tool_events] == ["started", "started", "finished", "finished"]
    view = store.rebuild_session_view("sess_parallel_bypass")
    tool_messages = [message for message in view.messages if message.role == "tool"]
    assert [message.parts[0].metadata["tool_call_id"] for message in tool_messages] == [
        "call_shell",
        "call_python",
    ]
    assert [message.parts[0].content for message in tool_messages] == ["shell:first", "python_exec:second"]


def test_agent_loop_streaming_runs_readonly_tool_calls_in_parallel(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_parallel_readonly", agents_md="")
    provider = StreamingProvider(
        [
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
                content="",
                tool_calls=[
                    ToolCall(id="call_slow_first", name="view", arguments={"text": "first"}),
                    ToolCall(id="call_slow_second", name="grep", arguments={"text": "second"}),
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="完成"),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        tools=[_slow_named_tool("view", delay=0.2), _slow_named_tool("grep", delay=0.2)],
    )

    started_at = time.perf_counter()
    result = loop.run_user_turn_streaming_sync("并发读")
    elapsed = time.perf_counter() - started_at

    assert result.content == "完成"
    assert elapsed < 0.35
    view = store.rebuild_session_view("sess_stream_parallel_readonly")
    tool_messages = [message for message in view.messages if message.role == "tool"]
    assert [message.parts[0].metadata["tool_call_id"] for message in tool_messages] == [
        "call_slow_first",
        "call_slow_second",
    ]


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


@pytest.mark.anyio
@pytest.mark.parametrize("anyio_backend", ["asyncio"])
async def test_agent_loop_streaming_tool_execution_does_not_block_event_loop(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_tool_thread", agents_md="")
    provider = StreamingProvider(
        [
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
                content="",
                tool_calls=[ToolCall(id="call_wait", name="wait_tool", arguments={})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="完成"),
        ]
    )
    started = threading.Event()
    release = threading.Event()

    def execute() -> ToolResult:
        started.set()
        release.wait(timeout=0.2)
        return ToolResult(name="wait_tool", ok=True, content="waited")

    tool = Tool(
        definition=ToolDefinition(
            name="wait_tool",
            description="blocks until released",
            parameters={"type": "object", "properties": {}},
        ),
        executor=execute,
    )
    events: list[ToolExecutionEvent] = []
    loop = AgentLoop(session=session, provider=provider, tools=[tool], tool_event_handler=events.append)
    task = asyncio.create_task(loop.run_user_turn_streaming("调用慢工具"))

    while not started.is_set():
        await asyncio.sleep(0.01)

    assert [event.kind for event in events] == ["started"]
    assert events[0].tool_call.name == "wait_tool"

    ticks = 0
    for _ in range(5):
        await asyncio.sleep(0)
        ticks += 1

    assert not task.done()
    release.set()
    result = await task

    assert ticks == 5
    assert result.content == "完成"
    assert [event.kind for event in events] == ["started", "finished"]
    assert events[1].result is not None
    assert events[1].result.content == "waited"


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


def test_agent_loop_streaming_retries_retryable_network_error_once(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_network_retry", agents_md="")
    provider = StreamingProvider(
        [
            ProviderError(ProviderErrorKind.NETWORK_ERROR, "connection closed"),
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="ok"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider, context_manager=RecordingContextManager())

    result = loop.run_user_turn_streaming_sync("问题")

    assert result.content == "ok"
    assert len(provider.requests) == 2
    assert [event.kind for event in loop.last_stream_events] == [
        "message_started",
        "text_delta",
        "text_delta",
        "message_completed",
    ]
    assert [event.text for event in loop.last_stream_events if event.kind == "text_delta"] == ["o", "k"]
    assert [message.role for message in store.rebuild_session_view("sess_stream_network_retry").messages] == [
        "user",
        "assistant",
    ]


def test_agent_loop_streaming_falls_back_to_non_streaming_after_retryable_stream_failures(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_fallback", agents_md="")
    provider = FallbackStreamingProvider(
        stream_errors=[
            ProviderError(ProviderErrorKind.NETWORK_ERROR, "connection closed"),
            ProviderError(ProviderErrorKind.NETWORK_ERROR, "connection closed again"),
        ],
        complete_response=ChatResponse(provider="fallback-stream", model="fallback-stream-model", content="complete ok"),
    )
    loop = AgentLoop(session=session, provider=provider, context_manager=RecordingContextManager())

    result = loop.run_user_turn_streaming_sync("问题")

    assert result.content == "complete ok"
    assert len(provider.stream_requests) == 2
    assert len(provider.complete_requests) == 1
    assert loop.last_stream_events == []
    view = store.rebuild_session_view("sess_stream_fallback")
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[-1].parts[0].content == "complete ok"


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
    assert "task_boundary" not in [tool.name for tool in tools]
    result = session.tool_registry.execute(
        "task_boundary",
        {"decision": "new", "basis_message_id": user_message_id},
    )
    assert result.ok
    assert result.data["candidate_hash"].startswith("task_")


def test_agent_loop_sends_tool_schema_only_via_request_tools(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_tool_schema", agents_md="", tools=[_echo_tool()])
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("调用 echo")

    request = provider.requests[0]
    echo = next(tool for tool in request.tools if tool.name == "echo")
    system_message = request.messages[0].content

    assert echo.description == "回显文本"
    assert echo.parameters["properties"]["text"] == {"type": "string"}
    assert "Available tools" not in system_message
    assert "echo" not in system_message
    assert "回显文本" not in system_message
    assert '"text": {"type": "string"}' not in system_message


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
    assert "Available tools" not in system_message


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


def test_agent_loop_initializes_active_task_hash_when_model_skips_task_boundary(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("读取 README")

    events = store.list_events("sess_test")
    boundary_event = next(event for event in events if event.type == "task_boundary_observed")
    replayed = replay_runtime_state(store, "sess_test")
    user_message = next(message for message in store.rebuild_session_view("sess_test").messages if message.role == "user")

    assert boundary_event.payload["confirmation_reason"] == "implicit_initial_task"
    assert boundary_event.payload["basis_message_id"] == user_message.id
    assert boundary_event.payload["active_task_hash"] == session.runtime_state.active_task_hash
    assert session.runtime_state.active_task_hash is not None
    assert replayed.active_task_hash == session.runtime_state.active_task_hash


def test_agent_loop_tags_initial_user_message_with_implicit_task_hash(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])

    AgentLoop(session=session, provider=provider).run_user_turn("第一任务：" + "旧任务内容" * 80)

    view = store.rebuild_session_view("sess_test")
    user_part = next(message for message in view.messages if message.role == "user").parts[0]
    assert user_part.metadata["task_hash"] == session.runtime_state.active_task_hash


def test_agent_loop_rejects_hidden_task_boundary_calls_from_main_model(tmp_path) -> None:
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
    assert "内部控制面工具" in tool_result.content
    assert event_types.count("task_boundary_observed") == 1
    assert session.runtime_state.active_task_hash is not None
    assert replayed.active_task_hash == session.runtime_state.active_task_hash


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


def test_agent_session_persists_successful_todo_result_as_native_state_event(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_todo_state", tools=[create_todo_tool()])
    session.runtime_state.active_task_hash = "task_current"
    tool_call = ToolCall(
        id="call_todo",
        name="todo",
        arguments={
            "todos": [
                {"content": "读代码", "status": "in_progress"},
            ]
        },
    )

    result = session.execute_tool_call(tool_call)
    session.append_tool_result(tool_call=tool_call, result=result)

    events = store.list_events("sess_todo_state")
    todo_event = next(event for event in events if event.type == "todo_updated")
    assert todo_event.payload == {
        "todos": [{"content": "读代码", "status": "in_progress"}],
        "task_hash": "task_current",
    }
    assert session.rebuild_view().todos == todo_event.payload["todos"]


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


def test_agent_loop_passes_tool_choice_none_for_final_only_completion(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_tool_choice", agents_md="")
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="final")])
    loop = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits.default(),
    )

    response = loop._complete_once(tool_choice="none")

    assert response.content == "final"
    assert provider.requests[0].tool_choice == "none"


def test_agent_loop_skips_classification_for_initial_task(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_forced_boundary", agents_md="")
    provider = JsonBoundaryProvider(["ok"])

    response = AgentLoop(session=session, provider=provider).run_user_turn("读取 README")

    assert response.content == "ok"
    assert len(provider.requests) == 1
    assert provider.requests[0].tool_choice == "auto"
    observations = [event for event in store.list_events("sess_forced_boundary") if event.type == "task_boundary_observed"]
    assert len(observations) == 1
    assert observations[0].payload["confirmation_reason"] == "implicit_initial_task"


def test_task_boundary_classification_prompt_defines_same_and_uncertain() -> None:
    prompt = agent_loop._TASK_BOUNDARY_CLASSIFICATION_PROMPT

    assert "continuation or follow-up of the active task" in prompt
    assert "Use \"uncertain\" only when the conversation does not provide enough information" in prompt


def test_agent_loop_retries_invalid_boundary_json_then_records_valid_observation(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_unforced_boundary", agents_md="")
    provider = JsonBoundaryProvider(["初始化完成", "not json", "<boundary>", "ok"])

    loop = AgentLoop(session=session, provider=provider)
    loop.run_user_turn("初始化任务")
    loop.run_user_turn("读取 README")

    user_message_id = store.rebuild_session_view("sess_unforced_boundary").messages[2].id
    assert len(provider.requests) == 4
    assert provider.requests[1].tools == []
    assert provider.requests[1].max_tokens == 512
    assert provider.requests[2].tools == []
    assert any("previous classification was invalid" in message.content for message in provider.requests[2].messages)
    assert any(user_message_id in message.content for message in provider.requests[2].messages)


def test_hidden_boundary_classification_counts_toward_provider_call_limit(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_boundary_budget", agents_md="")
    provider = JsonBoundaryProvider(["初始化完成", "<same>"])
    loop = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=1, max_turn_seconds=None),
    )
    loop.run_user_turn("初始化任务")

    response = loop.run_user_turn("继续")

    assert response.finish_reason == "provider_call_limit"
    assert len(provider.requests) == 2


def test_boundary_retry_cannot_exceed_provider_call_limit_before_main_request(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_boundary_retry_budget", agents_md="")
    provider = JsonBoundaryProvider(["初始化完成", "not json"])
    loop = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=1, max_turn_seconds=None),
    )
    loop.run_user_turn("初始化任务")

    response = loop.run_user_turn("继续")

    assert response.finish_reason == "provider_call_limit"
    assert len(provider.requests) == 2


def test_agent_loop_falls_back_to_uncertain_after_invalid_boundary_json(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_boundary_fallback", agents_md="")
    provider = JsonBoundaryProvider(["初始化完成", "not json", "still not json", "invalid again", "ok"])

    loop = AgentLoop(session=session, provider=provider)
    loop.run_user_turn("初始化任务")
    loop.run_user_turn("读取 README")

    observations = [event for event in store.list_events("sess_boundary_fallback") if event.type == "task_boundary_observed"]
    assert observations[-1].payload["decision"] == "uncertain"
    assert len(provider.requests) == 5


def test_agent_loop_streaming_classifies_boundary_without_emitting_stream_events(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_stream_forced_boundary", agents_md="")
    provider = StreamingProviderWithClassification(
        classification_responses=["<boundary>"],
        stream_responses=[
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="初始"),
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="ok"),
        ],
    )

    loop = AgentLoop(session=session, provider=provider)
    loop.run_user_turn_streaming_sync("初始化任务")
    response = loop.run_user_turn_streaming_sync("读取 README")

    assert response.content == "ok"
    assert provider.complete_requests[0].tools == []
    assert provider.stream_requests[1].tool_choice == "auto"


def test_agent_loop_runs_todo_self_check_before_final_answer(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_self_check",
        agents_md="",
        tools=[create_todo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={
                            "todos": [
                                {"content": "读代码", "status": "completed", "priority": "medium"},
                                {"content": "跑测试", "status": "pending", "priority": "medium"},
                            ],
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
            ChatResponse(provider="fake", model="fake-model", content="还需要跑测试，我继续。"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=5, max_turn_seconds=None),
    ).run_user_turn("做一个多步骤任务")

    assert response.content == "还需要跑测试，我继续。"
    assert len(provider.requests) == 3
    reconciliation = provider.requests[2].messages
    assert any(
        message.role == "system" and "unfinished Todo" in message.content
        for message in reconciliation
    )
    assert all(
        "unfinished Todo" not in part.content
        for message in store.rebuild_session_view("sess_todo_self_check").messages
        for part in message.parts
    )


def test_runtime_instruction_is_ephemeral_and_only_applies_to_one_request(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_runtime_instruction", agents_md="")
    session.append_user_message("真实用户请求")
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="first"),
            ChatResponse(provider="fake", model="fake-model", content="second"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    loop._complete_once(runtime_instruction="Reconcile Todo state")
    loop._complete_once()

    assert any(
        message.role == "system" and message.content == "Reconcile Todo state"
        for message in provider.requests[0].messages
    )
    assert all(message.content != "Reconcile Todo state" for message in provider.requests[1].messages)
    assert all(
        "Reconcile Todo state" not in part.content
        for message in session.rebuild_view().messages
        for part in message.parts
    )


def test_runtime_instruction_survives_prompt_too_long_retry(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_runtime_retry", agents_md="")
    session.append_user_message("真实用户请求")
    provider = FakeProvider(
        [
            ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long"),
            ChatResponse(provider="fake", model="fake-model", content="ok"),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        context_manager=PromptTooLongSuccessContextManager(),
    )

    response = loop._complete_once_with_recovery(runtime_instruction="Reconcile Todo state")

    assert response.content == "ok"
    assert len(provider.requests) == 2
    assert all(
        any(message.role == "system" and message.content == "Reconcile Todo state" for message in request.messages)
        for request in provider.requests
    )


def test_agent_loop_skips_todo_self_check_when_all_todos_done(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_self_check_done",
        agents_md="",
        tools=[create_todo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={
                            "todos": [
                                {"content": "读代码", "status": "completed", "priority": "medium"},
                                {"content": "跑测试", "status": "completed", "priority": "medium"},
                            ],
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=5, max_turn_seconds=None),
    ).run_user_turn("做一个多步骤任务")

    assert response.content == "我完成了"
    assert len(provider.requests) == 2


def test_agent_loop_skips_todo_self_check_when_all_todos_completed(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_self_check_completed",
        agents_md="",
        tools=[create_todo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={
                            "todos": [
                                {"content": "读代码", "status": "completed", "priority": "medium"},
                                {"content": "跑测试", "status": "completed", "priority": "medium"},
                            ],
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=5, max_turn_seconds=None),
    ).run_user_turn("做一个多步骤任务")

    assert response.content == "我完成了"
    assert len(provider.requests) == 2


def test_agent_loop_executes_tool_calls_after_todo_self_check(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_self_check_tools",
        agents_md="",
        tools=[create_todo_tool(), _echo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={
                            "todos": [{"content": "跑测试", "status": "pending", "priority": "medium"}],
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={"text": "run tests"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_done",
                        name="todo",
                        arguments={
                            "todos": [{"content": "跑测试", "status": "completed", "priority": "medium"}]
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="现在完成了"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=10, max_provider_calls=10, max_turn_seconds=None),
    ).run_user_turn("做一个多步骤任务")

    assert response.content == "现在完成了"
    view = store.rebuild_session_view("sess_todo_self_check_tools")
    tool_result_ids = [
        part.metadata["tool_call_id"]
        for message in view.messages
        for part in message.parts
        if part.kind == "tool_result"
    ]
    assert tool_result_ids == ["call_todo_set", "call_echo", "call_todo_done"]


def test_agent_loop_runs_todo_reconciliation_at_most_once_per_user_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_reconciliation_once",
        agents_md="",
        tools=[create_todo_tool(), _echo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={"todos": [{"content": "跑测试", "status": "pending"}]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={"text": "check"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="仍有未完成项"),
        ]
    )

    response = AgentLoop(session=session, provider=provider).run_user_turn("执行任务")

    assert response.content == "仍有未完成项"
    reconciliation_requests = [
        request
        for request in provider.requests
        if any(message.role == "system" and "unfinished Todo" in message.content for message in request.messages)
    ]
    assert len(reconciliation_requests) == 1


def test_agent_loop_does_not_reconcile_todo_after_tool_round_limit(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_tool_limit",
        agents_md="",
        tools=[create_todo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={"todos": [{"content": "跑测试", "status": "pending"}]},
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=1, max_provider_calls=5, max_turn_seconds=None),
    ).run_user_turn("执行任务")

    assert response.finish_reason == "tool_round_limit"
    assert len(provider.requests) == 1


def test_agent_loop_converts_provider_limit_during_todo_reconciliation_to_stop_response(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_provider_limit",
        agents_md="",
        tools=[create_todo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={"todos": [{"content": "跑测试", "status": "pending"}]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=2, max_turn_seconds=None),
    ).run_user_turn("执行任务")

    assert response.finish_reason == "provider_call_limit"
    assert len(provider.requests) == 2


def test_agent_loop_converts_timeout_during_todo_reconciliation_to_stop_response(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_timeout",
        agents_md="",
        tools=[create_todo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={"todos": [{"content": "跑测试", "status": "pending"}]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=5, max_turn_seconds=5),
        clock=FakeClock([0.0, 0.0, 0.0, 6.0]),
    ).run_user_turn("执行任务")

    assert response.finish_reason == "turn_timeout"
    assert len(provider.requests) == 2


def test_agent_loop_converts_cancellation_during_todo_reconciliation_to_interrupted_response(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_todo_cancelled",
        agents_md="",
        tools=[create_todo_tool()],
    )
    token = CancellationToken()
    provider = CancellingProvider(
        responses=[
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={"todos": [{"content": "跑测试", "status": "pending"}]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
        ],
        cancellation_token=token,
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        cancellation_token=token,
    ).run_user_turn("执行任务")

    assert response.finish_reason == "interrupted"
    assert len(provider.requests) == 2


def test_agent_loop_propagates_prewrite_review_from_todo_self_check_without_duplicate_call(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_todo_review_pause",
        project_root=tmp_path,
        tools=[create_todo_tool(), create_write_tool(tmp_path)],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={
                            "todos": [{"content": "写文件", "status": "pending", "priority": "medium"}],
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_write_from_self_check",
                        name="write",
                        arguments={"path": "README.md", "content": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_done_after_write",
                        name="todo",
                        arguments={
                            "todos": [{"content": "写文件", "status": "completed", "priority": "medium"}]
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="写入完成"),
        ]
    )

    loop = AgentLoop(session=session, provider=provider)
    result = loop.run_user_turn_interactive("完成任务")

    assert result.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    assert result.pending_input is not None
    assert result.pending_input.payload["pending_tool_call"]["id"] == "call_write_from_self_check"
    call_ids = [
        part.metadata["tool_call_id"]
        for message in store.rebuild_session_view("sess_todo_review_pause").messages
        for part in message.parts
        if part.kind == "tool_call"
    ]
    assert call_ids.count("call_write_from_self_check") == 1

    resumed = loop.resume_with_user_input(result.pending_input.id, "allow_once")

    assert resumed.status == AgentTurnStatus.COMPLETED
    assert resumed.response is not None
    assert resumed.response.content == "写入完成"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert [message.role for message in store.rebuild_session_view("sess_todo_review_pause").messages][-3:] == [
        "assistant",
        "tool",
        "assistant",
    ]


def test_permission_resume_does_not_repeat_todo_reconciliation_for_same_user_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_todo_review_once",
        project_root=tmp_path,
        tools=[create_todo_tool(), create_write_tool(tmp_path)],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={"todos": [{"content": "写文件", "status": "pending"}]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="我完成了"),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_write_from_reconciliation",
                        name="write",
                        arguments={"path": "README.md", "content": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="写入完成，但 Todo 尚未更新"),
            ChatResponse(provider="fake", model="fake-model", content="不应再次对账"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    paused = loop.run_user_turn_interactive("完成任务")
    assert paused.pending_input is not None
    resumed = loop.resume_with_user_input(paused.pending_input.id, "allow_once")

    assert resumed.response is not None
    assert resumed.response.content == "写入完成，但 Todo 尚未更新"
    reconciliation_requests = [
        request
        for request in provider.requests
        if any(message.role == "system" and "unfinished Todo" in message.content for message in request.messages)
    ]
    assert len(reconciliation_requests) == 1


def test_agent_loop_streaming_propagates_prewrite_review_from_todo_self_check_without_duplicate_call(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_stream_todo_review_pause",
        project_root=tmp_path,
        tools=[create_todo_tool(), create_write_tool(tmp_path)],
    )
    provider = StreamingProvider(
        [
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_set",
                        name="todo",
                        arguments={
                            "todos": [{"content": "写文件", "status": "pending", "priority": "medium"}],
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake-stream", model="fake-stream-model", content="我完成了"),
            ChatResponse(
                provider="fake-stream",
                model="fake-stream-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_stream_write_from_self_check",
                        name="write",
                        arguments={"path": "README.md", "content": "hello"},
                    )
                ],
                finish_reason="tool_calls",
            ),
        ]
    )

    response = AgentLoop(session=session, provider=provider).run_user_turn_streaming_sync("完成任务")

    assert response.finish_reason == AgentTurnStatus.WAITING_FOR_USER_INPUT.value
    pending = response.raw["pending_input"]
    assert pending.payload["pending_tool_call"]["id"] == "call_stream_write_from_self_check"
    call_ids = [
        part.metadata["tool_call_id"]
        for message in store.rebuild_session_view("sess_stream_todo_review_pause").messages
        for part in message.parts
        if part.kind == "tool_call"
    ]
    assert call_ids.count("call_stream_write_from_self_check") == 1


def test_agent_loop_injects_guidance_before_next_provider_call(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_guidance", agents_md="", tools=[_echo_tool()])
    guidance = ["先别总结，继续检查测试。"]
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={"text": "ok"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="收到，我继续检查测试。"),
        ]
    )
    guidance_calls = 0

    def drain_guidance() -> list[str]:
        nonlocal guidance_calls
        guidance_calls += 1
        if guidance_calls == 1:
            return []
        return guidance.pop(0).splitlines() if guidance else []

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        guidance_provider=drain_guidance,
    ).run_user_turn("开始检查")

    assert response.content == "收到，我继续检查测试。"
    assert len(provider.requests) == 2
    assert provider.requests[1].messages[-1].role == "user"
    assert "先别总结，继续检查测试。" in provider.requests[1].messages[-1].content
    assert guidance == []


def test_agent_loop_does_not_inject_periodic_todo_user_messages(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_no_todo_reminders",
        agents_md="",
        tools=[create_todo_tool(), _echo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo",
                        name="todo",
                        arguments={
                            "todos": [
                                {"content": "检查实现", "status": "in_progress"},
                                {"content": "运行测试", "status": "pending"},
                            ]
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            *[
                ChatResponse(
                    provider="fake",
                    model="fake-model",
                    content="",
                    tool_calls=[ToolCall(id=f"call_echo_{index}", name="echo", arguments={"text": str(index)})],
                    finish_reason="tool_calls",
                )
                for index in range(1, 4)
            ],
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo_done",
                        name="todo",
                        arguments={
                            "todos": [
                                {"content": "检查实现", "status": "completed"},
                                {"content": "运行测试", "status": "completed"},
                            ]
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="完成"),
        ]
    )

    AgentLoop(session=session, provider=provider).run_user_turn("完成多步骤任务")

    projected_user_messages = [
        message.content
        for request in provider.requests
        for message in request.messages
        if message.role == "user"
    ]
    assert all("Todo planning reminder" not in text for text in projected_user_messages)
    assert all("Todo progress reminder" not in text for text in projected_user_messages)
    assert [message.role for message in session.rebuild_view().messages].count("user") == 1


def test_agent_loop_resets_provider_call_count_for_each_user_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_provider_count_reset", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="first"),
            ChatResponse(provider="fake", model="fake-model", content="second"),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=1, max_provider_calls=2, max_turn_seconds=None),
    )

    first = loop.run_user_turn("第一轮")
    second = loop.run_user_turn("第二轮")

    assert first.content == "first"
    assert second.content == "second"


def test_agent_loop_allows_unlimited_tool_rounds_when_limit_is_none(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_unlimited_tools", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo_1", name="echo", arguments={"text": "one"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo_2", name="echo", arguments={"text": "two"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        limits=AgentLoopLimits(
            max_tool_rounds=None,
            max_provider_calls=10,
            max_turn_seconds=None,
        ),
    ).run_user_turn("调用两轮工具")

    assert response.content == "done"
    assert response.finish_reason != "tool_round_limit"


def test_agent_loop_allows_public_max_tool_rounds_none_override(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_public_unlimited_tools", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id=f"call_echo_{index}", name="echo", arguments={"text": str(index)})],
                finish_reason="tool_calls",
            )
            for index in range(21)
        ]
        + [ChatResponse(provider="fake", model="fake-model", content="done")]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        max_tool_rounds=None,
        limits=AgentLoopLimits(max_tool_rounds=20, max_provider_calls=30, max_turn_seconds=None),
    ).run_user_turn("调用很多轮工具")

    assert response.content == "done"
    assert response.finish_reason != "tool_round_limit"


def test_agent_loop_continues_after_successful_verification(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_verify_stop", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_test", name="shell", arguments={})],
                finish_reason="tool_calls",
            ),
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_diff", name="echo", arguments={"text": "diff"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="Tests pass after diff review."),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_success_tool(), _echo_tool()],
        limits=AgentLoopLimits.default(),
    ).run_user_turn("修测试")

    assert response.content == "Tests pass after diff review."
    assert len(provider.requests) == 3
    assert provider.requests[0].tool_choice == "auto"
    assert provider.requests[1].tool_choice == "auto"
    assert [message.role for message in store.rebuild_session_view("sess_verify_stop").messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]


def test_agent_loop_does_not_force_final_answer_after_failed_verification(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_verify_fail", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_test", name="shell", arguments={})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="继续修复"),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_failed_test_tool()],
        limits=AgentLoopLimits.default(),
    ).run_user_turn("修测试")

    assert response.content == "继续修复"
    assert provider.requests[1].tool_choice == "auto"


def test_agent_loop_stops_when_provider_call_limit_is_reached(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_provider_limit", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={"text": "one"})],
                finish_reason="tool_calls",
            ),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        limits=AgentLoopLimits(
            max_tool_rounds=None,
            max_provider_calls=1,
            max_turn_seconds=None,
        ),
    ).run_user_turn("调用工具")

    assert response.finish_reason == "provider_call_limit"
    assert "provider 调用次数达到上限" in response.content


def test_agent_loop_stops_when_turn_timeout_is_reached(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_turn_timeout", agents_md="")
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_echo", name="echo", arguments={"text": "one"})],
                finish_reason="tool_calls",
            ),
        ]
    )

    response = AgentLoop(
        session=session,
        provider=provider,
        tools=[_echo_tool()],
        limits=AgentLoopLimits(
            max_tool_rounds=None,
            max_provider_calls=None,
            max_turn_seconds=5,
        ),
        clock=FakeClock([0.0, 0.0, 6.0]),
    ).run_user_turn("调用工具")

    assert response.finish_reason == "turn_timeout"
    assert "本轮任务耗时达到上限" in response.content


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
    session.runtime_state.active_task_hash = "task_previous"
    provider = JsonBoundaryProvider(["<boundary>", "ok", "<same>", "ok"])
    context_manager = FakeContextManager()

    AgentLoop(
        session=session,
        provider=provider,
        context_manager=context_manager,
    ).run_user_turn("换一个任务")
    AgentLoop(
        session=session,
        provider=provider,
        context_manager=context_manager,
    ).run_user_turn("继续处理")

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


def test_agent_loop_interactive_pauses_on_ask_user(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_ask",
        agents_md="",
        tools=[create_ask_user_tool()],
    )
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
                        arguments={"question": "请选择环境", "options": ["dev", "prod"]},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="不应继续"),
        ]
    )

    result = AgentLoop(session=session, provider=provider).run_user_turn_interactive("部署")

    assert result.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    assert result.response is None
    assert result.pending_input is not None
    assert result.pending_input.kind == "ask_user"
    assert result.pending_input.id == "call_ask"
    assert result.pending_input.question == "请选择环境"
    assert [(option.id, option.label) for option in result.pending_input.options] == [
        ("1", "dev"),
        ("2", "prod"),
    ]
    assert len(provider.requests) == 1
    assert [message.role for message in store.rebuild_session_view("sess_ask").messages] == [
        "user",
        "assistant",
        "tool",
    ]


def test_agent_loop_skips_remaining_parallel_tools_when_waiting_for_user(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_parallel_ask",
        agents_md="",
        tools=[create_ask_user_tool(), _echo_tool()],
    )
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
                        arguments={"question": "确认？"},
                    ),
                    ToolCall(id="call_echo", name="echo", arguments={"text": "should skip"}),
                ],
                finish_reason="tool_calls",
            )
        ]
    )

    result = AgentLoop(session=session, provider=provider).run_user_turn_interactive("需要确认")

    assert result.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    view = store.rebuild_session_view("sess_parallel_ask")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "tool"]
    assert view.messages[2].parts[0].metadata["tool_call_id"] == "call_ask"
    skipped = view.messages[3].parts[0]
    assert skipped.metadata["tool_call_id"] == "call_echo"
    assert skipped.metadata["ok"] is False
    assert skipped.metadata["data"]["skipped_due_to_user_input"] is True


def test_agent_loop_permission_pause_does_not_append_confirmation_tool_result(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_pause",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
            )
        ]
    )

    result = AgentLoop(session=session, provider=provider).run_user_turn_interactive("写 README")

    assert result.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    assert result.pending_input is not None
    assert result.pending_input.kind == "permission_confirmation"
    review = result.pending_input.payload["prewrite_review"]
    assert review["summary"]["created_files"] == 1
    assert review["summary"]["added_lines"] == 1
    assert review["files"][0]["path"] == "README.md"
    assert review["files"][0]["operation"] == "create"
    assert "+hello" in review["files"][0]["diff"]
    assert session.pending_permission_execution is not None
    assert session.pending_permission_execution.prewrite_review is not None
    assert session.pending_permission_execution.tool_call.id == "call_write"
    assert not (tmp_path / "README.md").exists()
    view = store.rebuild_session_view("sess_perm_pause")
    assert [message.role for message in view.messages] == ["user", "assistant"]
    assert view.messages[1].parts[0].metadata["tool_call_id"] == "call_write"


def test_agent_loop_bypass_mode_emits_prewrite_review_and_executes_without_confirmation(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_review_bypass",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    session.set_permission_mode(PermissionMode.BYPASS)
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

    events: list[ToolExecutionEvent] = []
    result = AgentLoop(
        session=session,
        provider=provider,
        tool_event_handler=events.append,
    ).run_user_turn_interactive("写 README")

    assert result.status == AgentTurnStatus.COMPLETED
    assert result.pending_input is None
    assert result.response is not None
    assert result.response.content == "写好了"
    assert session.pending_permission_execution is None
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert [event.kind for event in events] == ["prewrite_review", "started", "finished"]
    assert events[0].prewrite_review is not None
    assert events[0].prewrite_review["files"][0]["path"] == "README.md"
    assert "+hello" in events[0].prewrite_review["files"][0]["diff"]


def test_agent_loop_streaming_bypass_mode_emits_prewrite_review_without_confirmation(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_stream_review_bypass",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
    session.set_permission_mode(PermissionMode.BYPASS)
    provider = StreamingProvider(
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

    events: list[ToolExecutionEvent] = []
    response = AgentLoop(
        session=session,
        provider=provider,
        tool_event_handler=events.append,
    ).run_user_turn_streaming_sync("写 README")

    assert response.content == "写好了"
    assert session.pending_permission_execution is None
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert [event.kind for event in events] == ["prewrite_review", "started", "finished"]
    assert events[0].prewrite_review is not None
    assert "+hello" in events[0].prewrite_review["files"][0]["diff"]


def test_agent_loop_bypass_mode_blocks_mutation_when_prewrite_review_fails(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("old\nold\n", encoding="utf-8")
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_review_failed_bypass",
        project_root=tmp_path,
        tools=[create_edit_tool(tmp_path)],
    )
    session.set_permission_mode(PermissionMode.BYPASS)
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_edit",
                        name="edit",
                        arguments={"path": "app.py", "old": "old", "new": "new"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="无法安全预览"),
        ]
    )

    result = AgentLoop(session=session, provider=provider).run_user_turn_interactive("修改 app.py")

    assert result.status == AgentTurnStatus.COMPLETED
    assert result.pending_input is None
    assert target.read_text(encoding="utf-8") == "old\nold\n"
    tool_part = store.rebuild_session_view("sess_review_failed_bypass").messages[2].parts[0]
    assert tool_part.metadata["ok"] is False
    assert tool_part.metadata["data"]["request_type"] == "prewrite_review_failed"


def test_agent_loop_shell_permission_does_not_claim_precomputed_diff(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_shell_no_review",
        project_root=tmp_path,
        tools=[create_shell_tool(tmp_path)],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(id="call_shell", name="shell", arguments={"command": "echo hello"})
                ],
                finish_reason="tool_calls",
            )
        ]
    )

    result = AgentLoop(session=session, provider=provider).run_user_turn_interactive("运行命令")

    assert result.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    assert result.pending_input is not None
    assert "prewrite_review" not in result.pending_input.payload


def test_agent_loop_blocks_mutation_when_prewrite_review_cannot_be_built(tmp_path) -> None:
    target = tmp_path / "app.py"
    target.write_text("old\nold\n", encoding="utf-8")
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_review_failed",
        project_root=tmp_path,
        tools=[create_edit_tool(tmp_path)],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_edit",
                        name="edit",
                        arguments={"path": "app.py", "old": "old", "new": "new"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="需要更精确的修改范围"),
        ]
    )

    result = AgentLoop(session=session, provider=provider).run_user_turn_interactive("修改 app.py")

    assert result.status == AgentTurnStatus.COMPLETED
    assert result.pending_input is None
    assert result.response is not None
    assert result.response.content == "需要更精确的修改范围"
    assert target.read_text(encoding="utf-8") == "old\nold\n"
    view = store.rebuild_session_view("sess_review_failed")
    tool_part = view.messages[2].parts[0]
    assert tool_part.metadata["ok"] is False
    assert tool_part.metadata["data"]["request_type"] == "prewrite_review_failed"
    assert "匹配内容出现 2 次" in tool_part.content


def test_agent_loop_permission_deny_appends_denied_result_and_continues(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_deny",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
            ChatResponse(provider="fake", model="fake-model", content="已取消写入"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    result = loop.resume_with_user_input(pending.id, "deny")

    assert result.status == AgentTurnStatus.COMPLETED
    assert result.response is not None
    assert result.response.content == "已取消写入"
    assert session.pending_permission_execution is None
    assert not (tmp_path / "README.md").exists()
    view = store.rebuild_session_view("sess_perm_deny")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    tool_part = view.messages[2].parts[0]
    assert tool_part.metadata["tool_call_id"] == "call_write"
    assert tool_part.metadata["ok"] is False
    assert tool_part.metadata["data"]["request_type"] == "permission_denied"
    assert provider.requests[1].messages[-1].tool_call_id == "call_write"


def test_permission_resume_preserves_provider_call_budget_for_same_user_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_provider_budget",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=1, max_turn_seconds=None),
    )

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    resumed = loop.resume_with_user_input(pending.id, "deny")

    assert resumed.response is not None
    assert resumed.response.finish_reason == "provider_call_limit"
    assert len(provider.requests) == 1


def test_permission_resume_preserves_turn_start_time_for_same_user_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_time_budget",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=5, max_provider_calls=5, max_turn_seconds=5),
        clock=FakeClock([0.0, 0.0, 6.0]),
    )

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    resumed = loop.resume_with_user_input(pending.id, "allow_once")

    assert resumed.response is not None
    assert resumed.response.finish_reason == "turn_timeout"
    assert len(provider.requests) == 1
    assert not (tmp_path / "README.md").exists()


def test_permission_resume_preserves_tool_round_budget_for_same_user_turn(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_tool_budget",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        limits=AgentLoopLimits(max_tool_rounds=1, max_provider_calls=5, max_turn_seconds=None),
    )

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    resumed = loop.resume_with_user_input(pending.id, "deny")

    assert resumed.response is not None
    assert resumed.response.finish_reason == "tool_round_limit"
    assert len(provider.requests) == 1


def test_agent_loop_permission_reject_with_feedback_returns_feedback_to_model(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_feedback",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
            ChatResponse(provider="fake", model="fake-model", content="我会按反馈重新修改"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    result = loop.resume_with_user_input(pending.id, "reject_with_feedback: 请保留原标题")

    assert result.response is not None
    assert result.response.content == "我会按反馈重新修改"
    assert not (tmp_path / "README.md").exists()
    view = store.rebuild_session_view("sess_perm_feedback")
    tool_part = view.messages[2].parts[0]
    assert tool_part.metadata["data"]["permission_feedback"] == "请保留原标题"
    assert "请保留原标题" in provider.requests[1].messages[-1].content


def test_agent_loop_permission_allow_once_executes_without_grant(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_once",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
    loop = AgentLoop(session=session, provider=provider)

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    result = loop.resume_with_user_input(pending.id, "allow_once")

    assert result.response is not None
    assert result.response.content == "写好了"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert session.permission_manager is not None
    assert session.permission_manager.grants.list() == []
    view = store.rebuild_session_view("sess_perm_once")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    assert view.messages[2].parts[0].metadata["ok"] is True


def test_agent_loop_permission_allow_once_rejects_stale_prewrite_review(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_stale_review",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
                        arguments={"path": "README.md", "content": "approved preview"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="预览已过期，未写入"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    (tmp_path / "README.md").write_text("external change", encoding="utf-8")
    result = loop.resume_with_user_input(pending.id, "allow_once")

    assert result.response is not None
    assert result.response.content == "预览已过期，未写入"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "external change"
    view = store.rebuild_session_view("sess_perm_stale_review")
    tool_part = view.messages[2].parts[0]
    assert tool_part.metadata["ok"] is False
    assert tool_part.metadata["data"]["request_type"] == "prewrite_review_stale"


def test_agent_loop_permission_allow_always_adds_grant_and_executes(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_always",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
    loop = AgentLoop(session=session, provider=provider)

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    loop.resume_with_user_input(pending.id, "allow_always_same_scope")

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    assert session.permission_manager is not None
    grants = session.permission_manager.grants.list()
    assert len(grants) == 1
    assert grants[0].effect == "allow"
    assert grants[0].scope_value == str((tmp_path / "README.md").resolve())


def test_agent_loop_permission_resume_rejects_unknown_request_id(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_unknown",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
            )
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    loop.run_user_turn_interactive("写 README")
    result = loop.resume_with_user_input("perm_wrong", "allow_once")

    assert result.response is not None
    assert result.response.finish_reason == "error"
    assert "没有找到" in result.response.content
    assert session.pending_permission_execution is not None
    assert not (tmp_path / "README.md").exists()


def test_agent_loop_permission_pending_blocks_new_user_turn_until_resolved(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_blocks_turn",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
            )
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    first = loop.run_user_turn_interactive("写 README")
    second = loop.run_user_turn_interactive("先别管权限，我有新问题")

    assert first.pending_input is not None
    assert second.status == AgentTurnStatus.WAITING_FOR_USER_INPUT
    assert second.pending_input is not None
    assert second.pending_input.id == first.pending_input.id
    assert len(provider.requests) == 1
    assert [message.role for message in store.rebuild_session_view("sess_perm_blocks_turn").messages] == [
        "user",
        "assistant",
    ]


def test_agent_loop_permission_resume_skips_remaining_parallel_tools(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_parallel",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path), _echo_tool()],
    )
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
                    ),
                    ToolCall(id="call_echo", name="echo", arguments={"text": "should skip"}),
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="写好了"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    pending = loop.run_user_turn_interactive("写 README，然后 echo").pending_input
    assert pending is not None
    loop.resume_with_user_input(pending.id, "allow_once")

    view = store.rebuild_session_view("sess_perm_parallel")
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "tool", "assistant"]
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "hello"
    skipped = view.messages[3].parts[0]
    assert skipped.metadata["tool_call_id"] == "call_echo"
    assert skipped.metadata["ok"] is False
    assert skipped.metadata["data"]["skipped_due_to_user_input"] is True
    assert provider.requests[1].messages[-2].tool_call_id == "call_write"
    assert provider.requests[1].messages[-1].tool_call_id == "call_echo"


def test_agent_loop_permission_resume_uses_local_pending_not_ui_payload(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_payload",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
                        arguments={"path": "README.md", "content": "safe"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="写好了"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    pending.payload["pending_tool_call"] = {
        "id": "call_fake",
        "name": "write",
        "arguments": {"path": "pwned.txt", "content": "tampered"},
    }
    loop.resume_with_user_input(pending.id, "allow_once")

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "safe"
    assert not (tmp_path / "pwned.txt").exists()
    view = store.rebuild_session_view("sess_perm_payload")
    tool_part = view.messages[2].parts[0]
    assert tool_part.metadata["tool_call_id"] == "call_write"


def test_agent_loop_permission_resume_ignores_nested_ui_argument_tampering(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / ".firstcoder")
    session = AgentSession.from_project(
        store=store,
        session_id="sess_perm_nested_payload",
        project_root=tmp_path,
        tools=[create_write_tool(tmp_path)],
    )
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
                        arguments={"path": "README.md", "content": "safe"},
                    )
                ],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="写好了"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    pending = loop.run_user_turn_interactive("写 README").pending_input
    assert pending is not None
    pending.payload["pending_tool_call"]["arguments"]["path"] = "pwned.txt"
    pending.payload["pending_tool_call"]["arguments"]["content"] = "tampered"
    loop.resume_with_user_input(pending.id, "allow_once")

    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "safe"
    assert not (tmp_path / "pwned.txt").exists()
