"""Agent 用户回合遥测。

遥测只保存控制循环的结构化计数，不保存提示词、工具参数、工具输出或密钥。事件通过
append-only session JSONL 持久化，但不会被 session view 投影成 provider 消息。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from firstcoder.agent.execution_evidence import is_mutation_result, is_validation_call
from firstcoder.context.identity import stable_json_hash
from firstcoder.providers.errors import ProviderErrorKind
from firstcoder.providers.types import ToolCall
from firstcoder.tools.types import ToolResult


TELEMETRY_SCHEMA_VERSION = 1


@dataclass(slots=True)
class AgentTurnTelemetry:
    """累计一个用户回合的可观察执行指标。"""

    turn_number: int = 0
    started_at: float | None = None
    active: bool = False
    snapshot_index: int = 0
    provider_calls: int = 0
    provider_retries: int = 0
    provider_retry_categories: dict[str, int] = field(default_factory=dict)
    provider_failure_category: str | None = None
    tool_calls: int = 0
    tool_failures: int = 0
    tool_call_counts: dict[str, int] = field(default_factory=dict)
    _tool_call_fingerprints: dict[str, int] = field(default_factory=dict, repr=False)
    repeated_tool_calls: int = 0
    max_identical_tool_calls: int = 0
    first_mutation_sequence: int | None = None
    first_mutation_elapsed_seconds: float | None = None
    validation_count: int = 0
    latest_validation_ok: bool | None = None
    latest_validation_tool: str | None = None
    completion_gate_used: bool = False
    completion_gate_reason_count: int = 0

    def begin(self, *, turn_number: int, started_at: float) -> None:
        self.turn_number = turn_number
        self.started_at = started_at
        self.active = True
        self.snapshot_index = 0
        self.provider_calls = 0
        self.provider_retries = 0
        self.provider_retry_categories.clear()
        self.provider_failure_category = None
        self.tool_calls = 0
        self.tool_failures = 0
        self.tool_call_counts.clear()
        self._tool_call_fingerprints.clear()
        self.repeated_tool_calls = 0
        self.max_identical_tool_calls = 0
        self.first_mutation_sequence = None
        self.first_mutation_elapsed_seconds = None
        self.validation_count = 0
        self.latest_validation_ok = None
        self.latest_validation_tool = None
        self.completion_gate_used = False
        self.completion_gate_reason_count = 0

    def observe_provider_call(self) -> None:
        if self.active:
            self.provider_calls += 1

    def observe_provider_retry(self, kind: ProviderErrorKind) -> None:
        if not self.active:
            return
        category = kind.value
        self.provider_retries += 1
        self.provider_retry_categories[category] = self.provider_retry_categories.get(category, 0) + 1

    def observe_tool_result(self, tool_call: ToolCall, result: ToolResult, *, elapsed_seconds: float) -> None:
        if not self.active:
            return
        self.tool_calls += 1
        self.tool_call_counts[tool_call.name] = self.tool_call_counts.get(tool_call.name, 0) + 1
        if not result.ok:
            self.tool_failures += 1

        fingerprint = stable_json_hash(
            {"name": tool_call.name, "arguments": tool_call.arguments},
            length=24,
        )
        identical_count = self._tool_call_fingerprints.get(fingerprint, 0) + 1
        self._tool_call_fingerprints[fingerprint] = identical_count
        self.max_identical_tool_calls = max(self.max_identical_tool_calls, identical_count)
        if identical_count > 1:
            self.repeated_tool_calls += 1

        if self.first_mutation_sequence is None and is_mutation_result(tool_call, result):
            self.first_mutation_sequence = self.tool_calls
            self.first_mutation_elapsed_seconds = max(0.0, elapsed_seconds)
        if is_validation_call(tool_call):
            self.validation_count += 1
            self.latest_validation_ok = result.ok
            self.latest_validation_tool = tool_call.name

    def observe_completion_gate(self, *, reason_count: int) -> None:
        if not self.active:
            return
        self.completion_gate_used = True
        self.completion_gate_reason_count = max(self.completion_gate_reason_count, reason_count)

    def snapshot(
        self,
        *,
        status: str,
        stop_reason: str,
        elapsed_seconds: float,
        provider_failure_category: str | None = None,
        finalize: bool,
    ) -> dict[str, Any] | None:
        if not self.active:
            return None
        self.snapshot_index += 1
        if provider_failure_category is not None:
            self.provider_failure_category = provider_failure_category
        payload: dict[str, Any] = {
            "schema_version": TELEMETRY_SCHEMA_VERSION,
            "turn_number": self.turn_number,
            "snapshot_index": self.snapshot_index,
            "status": status,
            "stop_reason": stop_reason,
            "elapsed_seconds": round(max(0.0, elapsed_seconds), 6),
            "provider_calls": self.provider_calls,
            "provider_retries": self.provider_retries,
            "provider_retry_categories": dict(sorted(self.provider_retry_categories.items())),
            "provider_failure_category": self.provider_failure_category,
            "tool_calls": self.tool_calls,
            "tool_failures": self.tool_failures,
            "tool_call_counts": dict(sorted(self.tool_call_counts.items())),
            "repeated_tool_calls": self.repeated_tool_calls,
            "max_identical_tool_calls": self.max_identical_tool_calls,
            "first_mutation_sequence": self.first_mutation_sequence,
            "first_mutation_elapsed_seconds": (
                round(self.first_mutation_elapsed_seconds, 6)
                if self.first_mutation_elapsed_seconds is not None
                else None
            ),
            "validation_count": self.validation_count,
            "latest_validation_ok": self.latest_validation_ok,
            "latest_validation_tool": self.latest_validation_tool,
            "completion_gate_used": self.completion_gate_used,
            "completion_gate_reason_count": self.completion_gate_reason_count,
        }
        if finalize:
            self.active = False
        return payload
