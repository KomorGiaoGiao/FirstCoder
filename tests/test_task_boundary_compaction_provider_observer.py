from __future__ import annotations

from dataclasses import dataclass, field

from benchmark.task_boundary_compaction.provider_observer import RecordingProvider, usage_totals
from firstcoder.agent.task_boundary_classifier import CLASSIFICATION_MAX_TOKENS
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ProviderCapabilities,
    TokenUsage,
)


@dataclass
class UsageProvider(ChatProvider):
    responses: list[ChatResponse]
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    requests: list[ChatRequest] = field(default_factory=list)

    @property
    def name(self) -> str:
        return "wrapped"

    @property
    def model(self) -> str:
        return "wrapped-model"

    def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        return self.responses.pop(0)


def _response(*, usage: TokenUsage | None) -> ChatResponse:
    return ChatResponse(provider="wrapped", model="wrapped-model", content="ok", usage=usage)


def _main_request() -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="user", content="修复 parser")],
        tools=[],
        tool_choice="auto",
        max_tokens=1024,
    )


def _classifier_request() -> ChatRequest:
    return ChatRequest(
        messages=[ChatMessage(role="system", content="classify")],
        tools=[],
        tool_choice="none",
        max_tokens=CLASSIFICATION_MAX_TOKENS,
    )


def _l4_request() -> ChatRequest:
    return ChatRequest(
        messages=[
            ChatMessage(
                role="system",
                content="你是 FirstCoder 的上下文压缩器。输出简洁的 coding handoff；",
            ),
            ChatMessage(role="user", content="摘要这段上下文"),
        ],
        tools=[],
        tool_choice="none",
        max_tokens=1200,
    )


def test_recording_provider_classifies_all_visible_and_hidden_calls() -> None:
    raw = UsageProvider(
        responses=[
            _response(usage=TokenUsage(input_tokens=10, output_tokens=4, total_tokens=14)),
            _response(usage=TokenUsage(input_tokens=8, output_tokens=2, total_tokens=10)),
            _response(usage=TokenUsage(input_tokens=30, output_tokens=12, total_tokens=42)),
        ]
    )
    provider = RecordingProvider(raw)

    provider.complete(_main_request())
    provider.complete(_classifier_request())
    provider.complete(_l4_request())

    assert provider.name == "wrapped"
    assert provider.model == "wrapped-model"
    assert provider.capabilities is raw.capabilities
    assert [metric.kind for metric in provider.metrics] == ["main", "classifier", "l4"]
    assert [metric.total_tokens for metric in provider.metrics] == [14, 10, 42]
    assert all(metric.elapsed_seconds >= 0 for metric in provider.metrics)

    totals = usage_totals(provider.metrics)
    assert totals["main"].input_tokens == 10
    assert totals["classifier"].total_tokens == 10
    assert totals["l4"].output_tokens == 12
    assert totals["all"].total_tokens == 66


def test_recording_provider_preserves_missing_usage_as_incomplete() -> None:
    provider = RecordingProvider(UsageProvider(responses=[_response(usage=None)]))

    provider.complete(_main_request())

    metric = provider.metrics[0]
    totals = usage_totals(provider.metrics)
    assert metric.input_tokens is None
    assert metric.output_tokens is None
    assert metric.total_tokens is None
    assert totals["main"].input_tokens is None
    assert totals["all"].total_tokens is None
