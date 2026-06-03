from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from firstcoder.context.llm_compact import LlmCompactSummary, NoSummaryError, PromptTooLongError
from firstcoder.context.models import AgentMessage, MessagePart
from firstcoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatRequest, ChatResponse


@dataclass
class FakeProvider(ChatProvider):
    response: ChatResponse | ProviderError
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "fake"

    @property
    def model(self) -> str:
        return "fake-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if isinstance(self.response, ProviderError):
            raise self.response
        return self.response


def test_provider_summarizer_requests_plain_summary_without_tools() -> None:
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content="摘要"))

    summary = ProviderLlmCompactSummarizer(provider).summarize(
        [
            _message("msg_1", "user", "目标"),
            _message("msg_2", "assistant", "进展"),
        ]
    )

    assert isinstance(summary, LlmCompactSummary)
    assert summary.summary == "摘要"
    assert summary.covered_until_message_id == "msg_1"
    assert summary.tail_start_message_id == "msg_2"
    assert provider.requests[0].tools == []
    assert provider.requests[0].tool_choice == "none"


def test_provider_summarizer_keeps_tool_call_sequence_in_tail() -> None:
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content="摘要"))

    summary = ProviderLlmCompactSummarizer(provider).summarize(
        [
            _message("msg_1", "user", "目标"),
            _assistant_tool_call("msg_2", "call_1"),
            _tool_result("msg_3", "call_1"),
        ]
    )

    assert summary.covered_until_message_id == "msg_1"
    assert summary.tail_start_message_id == "msg_2"


def test_provider_summarizer_maps_prompt_too_long_provider_error() -> None:
    provider = FakeProvider(ProviderError(ProviderErrorKind.PROMPT_TOO_LONG, "too long"))

    with pytest.raises(PromptTooLongError):
        ProviderLlmCompactSummarizer(provider).summarize(
            [
                _message("msg_1", "user", "目标"),
                _message("msg_2", "assistant", "进展"),
            ]
        )


def test_provider_summarizer_rejects_too_short_history() -> None:
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content="摘要"))

    with pytest.raises(NoSummaryError):
        ProviderLlmCompactSummarizer(provider).summarize([_message("msg_1", "user", "目标")])


def _message(message_id: str, role: str, content: str) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role=role,
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="text",
                content=content,
            )
        ],
    )


def _assistant_tool_call(message_id: str, tool_call_id: str) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="assistant",
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="tool_call",
                content="{}",
                metadata={"tool_call_id": tool_call_id, "tool_name": "grep"},
            )
        ],
    )


def _tool_result(message_id: str, tool_call_id: str) -> AgentMessage:
    return AgentMessage(
        id=message_id,
        session_id="sess_test",
        role="tool",
        parts=[
            MessagePart(
                id=f"part_{message_id}",
                message_id=message_id,
                kind="tool_result",
                content="结果",
                metadata={"tool_call_id": tool_call_id, "tool_name": "grep"},
            )
        ],
    )
