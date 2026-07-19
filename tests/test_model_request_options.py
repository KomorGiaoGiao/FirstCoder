from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from firstcoder.agent.loop import AgentLoop
from firstcoder.agent.session import AgentSession
from firstcoder.agent.task_boundary_classifier import TaskBoundaryClassifier
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.store import JsonlSessionStore
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import (
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    MainRequestOptions,
)


@dataclass
class RecordingProvider(ChatProvider):
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "recording"

    @property
    def model(self) -> str:
        return "recording-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return ChatResponse(provider=self.name, model=self.model, content="ok")

    async def astream(self, request: ChatRequest):
        self.requests.append(request)
        yield ChatStreamEvent(kind="message_completed", response=ChatResponse(provider=self.name, model=self.model, content="ok"))


def _session(tmp_path) -> AgentSession:
    return AgentSession.create(store=JsonlSessionStore(tmp_path), session_id="sess_options", agents_md="")


def test_main_sync_request_inherits_selected_model_options(tmp_path) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    loop = AgentLoop(
        session=session,
        provider=provider,
        request_options=MainRequestOptions(
            temperature=0.2,
            max_tokens=8192,
            extra_body={"reasoning_effort": "high"},
        ),
    )
    session.append_user_message("检查 README")

    loop._complete_once()

    request = provider.requests[-1]
    assert request.temperature == 0.2
    assert request.max_tokens == 8192
    assert request.extra_body == {"reasoning_effort": "high"}


def test_main_stream_request_inherits_selected_model_options(tmp_path) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    loop = AgentLoop(
        session=session,
        provider=provider,
        request_options=MainRequestOptions(
            temperature=0.3,
            max_tokens=4096,
            extra_body={"reasoning_effort": "medium"},
        ),
    )
    session.append_user_message("检查 README")

    asyncio.run(loop._stream_once())

    request = provider.requests[-1]
    assert request.temperature == 0.3
    assert request.max_tokens == 4096
    assert request.extra_body == {"reasoning_effort": "medium"}


def test_task_boundary_classifier_keeps_its_fixed_request_options(tmp_path) -> None:
    session = _session(tmp_path)
    classifier = TaskBoundaryClassifier(
        session=session,
        provider=RecordingProvider(),
        context_builder=ContextBuilder(),
        compact_if_needed=lambda **_: None,
        check_cancelled=lambda: None,
        reserve_provider_call=lambda: None,
        check_turn_timeout=lambda: None,
        tag_task_boundary_messages=lambda *_: None,
    )

    request = classifier.build_request(attempt=0)

    assert request.max_tokens == 512
    assert request.temperature is None
    assert request.extra_body == {}


def test_main_request_options_copy_extra_body() -> None:
    options_body = {"nested": {"value": 1}}
    options = MainRequestOptions(extra_body=options_body)
    options_body["nested"]["value"] = 2
    kwargs = options.as_chat_request_kwargs()
    kwargs["extra_body"]["nested"]["value"] = 3

    assert options.extra_body == {"nested": {"value": 1}}
