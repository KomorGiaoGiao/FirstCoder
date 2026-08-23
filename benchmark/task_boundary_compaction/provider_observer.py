"""Provider decorator that records benchmark cost without retaining model content."""

from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Literal, Sequence

from benchmark.task_boundary_compaction.models import ProviderCallMetric
from firstcoder.agent.task_boundary_classifier import CLASSIFICATION_MAX_TOKENS
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import ChatRequest, ChatResponse


CallBucket = Literal["main", "classifier", "l4", "all"]
_L4_SYSTEM_PREFIX = "你是 FirstCoder 的上下文压缩器。"


@dataclass(frozen=True, slots=True)
class TokenTotals:
    """One bucket's token subtotal, preserving unknown provider fields as ``None``."""

    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None


class RecordingProvider(ChatProvider):
    """Delegate non-streaming calls and retain only safe benchmark metrics."""

    def __init__(self, wrapped: ChatProvider) -> None:
        self._wrapped = wrapped
        self.metrics: list[ProviderCallMetric] = []

    @property
    def name(self) -> str:
        return self._wrapped.name

    @property
    def model(self) -> str:
        return self._wrapped.model

    @property
    def capabilities(self):
        return self._wrapped.capabilities

    def complete(self, request: ChatRequest) -> ChatResponse:
        started_at = time.perf_counter()
        kind = _classify_request(request)
        try:
            response = self._wrapped.complete(request)
        except BaseException:
            self._record(kind=kind, elapsed_seconds=time.perf_counter() - started_at, response=None)
            raise
        self._record(kind=kind, elapsed_seconds=time.perf_counter() - started_at, response=response)
        return response

    def _record(
        self,
        *,
        kind: Literal["main", "classifier", "l4"],
        elapsed_seconds: float,
        response: ChatResponse | None,
    ) -> None:
        usage = response.usage if response is not None else None
        self.metrics.append(
            ProviderCallMetric(
                kind=kind,
                input_tokens=usage.input_tokens if usage is not None else None,
                output_tokens=usage.output_tokens if usage is not None else None,
                total_tokens=usage.total_tokens if usage is not None else None,
                elapsed_seconds=elapsed_seconds,
            )
        )


def usage_totals(metrics: Sequence[ProviderCallMetric]) -> dict[CallBucket, TokenTotals]:
    """Return per-call-kind and all-call token totals without treating unknown as zero."""

    buckets: dict[CallBucket, tuple[ProviderCallMetric, ...]] = {
        "main": tuple(metric for metric in metrics if metric.kind == "main"),
        "classifier": tuple(metric for metric in metrics if metric.kind == "classifier"),
        "l4": tuple(metric for metric in metrics if metric.kind == "l4"),
        "all": tuple(metrics),
    }
    return {
        name: TokenTotals(
            input_tokens=_sum_or_unknown(bucket, "input_tokens"),
            output_tokens=_sum_or_unknown(bucket, "output_tokens"),
            total_tokens=_sum_or_unknown(bucket, "total_tokens"),
        )
        for name, bucket in buckets.items()
    }


def _classify_request(request: ChatRequest) -> Literal["main", "classifier", "l4"]:
    if (
        request.tools == []
        and request.tool_choice == "none"
        and request.max_tokens == 1200
        and bool(request.messages)
        and request.messages[0].role == "system"
        and request.messages[0].content.startswith(_L4_SYSTEM_PREFIX)
    ):
        return "l4"
    if (
        request.tools == []
        and request.tool_choice == "none"
        and request.max_tokens == CLASSIFICATION_MAX_TOKENS
    ):
        return "classifier"
    return "main"


def _sum_or_unknown(
    metrics: Sequence[ProviderCallMetric],
    field_name: Literal["input_tokens", "output_tokens", "total_tokens"],
) -> int | None:
    values = [getattr(metric, field_name) for metric in metrics]
    if any(value is None for value in values):
        return None
    return sum(values)
