from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.loop_limits import AgentLoopLimits
from firstcoder.agent.provider_retry import ProviderRetryPolicy
from firstcoder.agent.session import AgentSession
from firstcoder.agent.telemetry import AgentTurnTelemetry
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatRequest, ChatResponse, ToolCall
from firstcoder.runtime.cancellation import CancellationToken
from firstcoder.runtime.cancellation import AgentCancelledError
from firstcoder.tools.ask_user import create_ask_user_tool
from firstcoder.tools.types import ToolResult


@dataclass
class RecordingProvider(ChatProvider):
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
        response = self.responses.pop(0)
        if isinstance(response, ProviderError):
            raise response
        return response


def _telemetry_events(store: JsonlSessionStore, session_id: str):
    return [
        event
        for event in store.list_events(session_id)
        if event.type == "agent_turn_telemetry"
    ]


def test_turn_telemetry_aggregates_retries_repeated_calls_mutation_and_validation() -> None:
    telemetry = AgentTurnTelemetry()
    telemetry.begin(turn_number=3, started_at=10.0)
    telemetry.observe_provider_call()
    telemetry.observe_provider_call()
    telemetry.observe_provider_retry(ProviderErrorKind.NETWORK_ERROR)
    telemetry.observe_provider_retry(ProviderErrorKind.NETWORK_ERROR)

    telemetry.observe_tool_result(
        ToolCall(id="write", name="write", arguments={"path": "README.md", "content": "ok"}),
        ToolResult(name="write", ok=True, content="written"),
        elapsed_seconds=2.5,
    )
    failed_validation = ToolResult(
        name="shell",
        ok=False,
        content="1 failed",
        error="exit 1",
    )
    validation_call = ToolCall(
        id="test-1",
        name="shell",
        arguments={"command": "pytest -q"},
    )
    telemetry.observe_tool_result(validation_call, failed_validation, elapsed_seconds=4.0)
    telemetry.observe_tool_result(validation_call, failed_validation, elapsed_seconds=5.0)
    telemetry.observe_completion_gate(reason_count=2)

    payload = telemetry.snapshot(
        status="limited",
        stop_reason="turn_timeout",
        elapsed_seconds=9.25,
        finalize=True,
    )

    assert payload is not None
    assert payload["turn_number"] == 3
    assert payload["provider_calls"] == 2
    assert payload["provider_retries"] == 2
    assert payload["provider_retry_categories"] == {"network_error": 2}
    assert payload["tool_calls"] == 3
    assert payload["tool_failures"] == 2
    assert payload["repeated_tool_calls"] == 1
    assert payload["max_identical_tool_calls"] == 2
    assert payload["first_mutation_sequence"] == 1
    assert payload["first_mutation_elapsed_seconds"] == 2.5
    assert payload["validation_count"] == 2
    assert payload["latest_validation_ok"] is False
    assert payload["completion_gate_used"] is True
    assert payload["completion_gate_reason_count"] == 2
    assert telemetry.active is False


def test_agent_loop_persists_completed_telemetry_without_projecting_it_to_provider(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_telemetry_completed")
    provider = RecordingProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    first = loop._run_user_turn_sync("first")
    session.writer.append_agent_turn_telemetry({"sentinel": "TELEMETRY_MUST_STAY_OUT"})
    projected = loop._prepare_main_provider_request()

    assert first.content == "done"
    payload = _telemetry_events(store, session.session_id)[0].payload
    assert payload["status"] == "completed"
    assert payload["stop_reason"] == "completed"
    assert payload["provider_calls"] == 1
    assert payload["turn_number"] == 1
    assert [message.role for message in store.rebuild_session_view(session.session_id).messages] == ["user", "assistant"]
    assert all(
        "TELEMETRY_MUST_STAY_OUT" not in message.content
        for message in projected.request.messages
    )


def test_agent_loop_persists_paused_telemetry(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_telemetry_paused",
        tools=[create_ask_user_tool()],
    )
    provider = RecordingProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="ask-1",
                        name="ask_user",
                        arguments={"question": "Which target?"},
                    )
                ],
                finish_reason="tool_calls",
            )
        ]
    )

    result = AgentLoop(session=session, provider=provider)._run_user_turn_sync("ambiguous")

    assert result.pending_input is not None
    payload = _telemetry_events(store, session.session_id)[0].payload
    assert payload["status"] == "paused"
    assert payload["stop_reason"] == "ask_user"
    assert payload["tool_calls"] == 1
    assert payload["snapshot_index"] == 1


def test_agent_loop_persists_provider_failure_category_and_retry_count(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(store=store, session_id="sess_telemetry_provider_error")
    provider = RecordingProvider(
        [
            ProviderError(ProviderErrorKind.NETWORK_ERROR, "connection reset"),
            ProviderError(ProviderErrorKind.AUTH_ERROR, "bad key"),
        ]
    )
    loop = AgentLoop(
        session=session,
        provider=provider,
        provider_retry_policy=ProviderRetryPolicy(max_retries=2, initial_delay_seconds=0),
        provider_retry_sleeper=lambda _delay: None,
    )

    with pytest.raises(ProviderError) as exc_info:
        loop._run_user_turn_sync("fail")

    assert exc_info.value.kind == ProviderErrorKind.AUTH_ERROR
    payload = _telemetry_events(store, session.session_id)[0].payload
    assert payload["status"] == "errored"
    assert payload["stop_reason"] == "auth_error"
    assert payload["provider_failure_category"] == "auth_error"
    assert payload["provider_calls"] == 2
    assert payload["provider_retries"] == 1
    assert payload["provider_retry_categories"] == {"network_error": 1}


def test_agent_loop_persists_limit_and_interrupted_turns(tmp_path) -> None:
    limited_store = JsonlSessionStore(tmp_path / "limited")
    limited_session = AgentSession.create(
        store=limited_store,
        session_id="sess_telemetry_limited",
    )
    limited_provider = RecordingProvider([])

    limited = AgentLoop(
        session=limited_session,
        provider=limited_provider,
        limits=AgentLoopLimits(max_provider_calls=0),
    )._run_user_turn_sync("limited")

    assert limited.finish_reason == "provider_call_limit"
    limited_payload = _telemetry_events(limited_store, limited_session.session_id)[0].payload
    assert limited_payload["status"] == "limited"
    assert limited_payload["stop_reason"] == "provider_call_limit"
    assert limited_payload["provider_calls"] == 0

    interrupted_store = JsonlSessionStore(tmp_path / "interrupted")
    interrupted_session = AgentSession.create(
        store=interrupted_store,
        session_id="sess_telemetry_interrupted",
    )
    token = CancellationToken()
    token.cancel()

    with pytest.raises(AgentCancelledError):
        AgentLoop(
            session=interrupted_session,
            provider=RecordingProvider([]),
            cancellation_token=token,
        )._run_user_turn_sync("cancelled")

    interrupted_payload = _telemetry_events(
        interrupted_store,
        interrupted_session.session_id,
    )[0].payload
    assert interrupted_payload["status"] == "interrupted"
    assert interrupted_payload["stop_reason"] == "interrupted"
