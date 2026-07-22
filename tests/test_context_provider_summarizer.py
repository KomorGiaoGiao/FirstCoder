from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from firstcoder.context.llm_compact import (
    CODING_HANDOFF_HEADINGS,
    LlmCompactSummary,
    NoSummaryError,
    PromptTooLongError,
    normalize_coding_handoff,
)
from firstcoder.context.models import AgentMessage, MessagePart
from firstcoder.context.provider_summarizer import ProviderLlmCompactSummarizer
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatRequest, ChatResponse

EXPECTED_CODING_HANDOFF_HEADINGS = (
    "## 当前目标",
    "## 已知事实与硬约束",
    "## 已确认的决定及理由",
    "## 相关文件与当前实现状态",
    "## 已运行命令及有效结果",
    "## 当前错误与未解决事项",
    "## 下一步（可立即执行）",
)


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
    assert "## 当前目标\n摘要" in summary.summary
    assert summary.summary.count("## ") == 7
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


def test_provider_summarizer_prompt_and_normalizer_enforce_exact_handoff_headings() -> None:
    assert CODING_HANDOFF_HEADINGS == EXPECTED_CODING_HANDOFF_HEADINGS
    model_output = "\n".join(
        [
            "## 当前目标",
            "Implement L1.",
            "## 当前目标",
            "Keep the latest user message.",
            "## 当前错误与未解决事项",
            "A failing test remains.",
            "## Extra model heading",
            "Keep this as body text.",
        ]
    )
    provider = FakeProvider(ChatResponse(provider="fake", model="fake-model", content=model_output))

    summary = ProviderLlmCompactSummarizer(provider).summarize([_message("msg_1", "user", "目标"), _message("msg_2", "assistant", "进展")])

    assert all(summary.summary.count(heading) == 1 for heading in CODING_HANDOFF_HEADINGS)
    assert "Implement L1.\nKeep the latest user message." in summary.summary
    assert "A failing test remains.\nExtra model heading\nKeep this as body text." in summary.summary
    assert "## 已知事实与硬约束\n无" in summary.summary
    prompt = provider.requests[0].messages[1].content
    assert all(prompt.count(heading) == 1 for heading in CODING_HANDOFF_HEADINGS)
    assert "恰好出现一次" in prompt


def test_normalize_coding_handoff_preserves_body_under_matching_heading() -> None:
    normalized = normalize_coding_handoff("## 下一步（可立即执行）\nRun focused tests.")

    assert normalized.endswith("## 下一步（可立即执行）\nRun focused tests.")
    assert all(normalized.count(heading) == 1 for heading in CODING_HANDOFF_HEADINGS)


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
