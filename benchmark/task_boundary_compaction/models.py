"""Immutable, JSON-safe data models for task-boundary benchmark trials."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Mapping


class Arm(StrEnum):
    """The fixed causal experiment arms."""

    AUTO_ONLY = "auto_only"
    CLASSIFIER_ONLY = "classifier_only"
    FULL = "full"


Decision = Literal["new", "same", "uncertain"]
CaseKind = Literal["controlled", "historical"]
CallKind = Literal["main", "classifier", "l4"]

_VALID_DECISIONS = frozenset({"new", "same", "uncertain"})
_VALID_CASE_KINDS = frozenset({"controlled", "historical"})
_VALID_CALL_KINDS = frozenset({"main", "classifier", "l4"})


@dataclass(frozen=True, slots=True)
class TurnSpec:
    """One task-B user turn and its expected hidden-classifier decision."""

    message: str
    expected_decision: Decision

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("message must not be blank")
        if self.expected_decision not in _VALID_DECISIONS:
            raise ValueError("expected_decision must be new, same, or uncertain")


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """A two-or-more-turn task-B benchmark case."""

    case_id: str
    kind: CaseKind
    turns: tuple[TurnSpec, ...]
    verify_command: tuple[str, ...]
    expected_boundary: bool

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must not be blank")
        if self.kind not in _VALID_CASE_KINDS:
            raise ValueError("kind must be controlled or historical")
        if len(self.turns) < 2:
            raise ValueError("turns must contain at least two task-B turns")
        if not self.verify_command or any(not part.strip() for part in self.verify_command):
            raise ValueError("verify_command must not be empty or contain blank parts")

        decisions = tuple(turn.expected_decision for turn in self.turns)
        if self.expected_boundary:
            if decisions[0] != "new" or decisions[-1] != "same":
                raise ValueError("turns for an expected boundary must begin with new and end with same")
        elif any(decision != "same" for decision in decisions):
            raise ValueError("turns for a non-boundary case must all be same")


@dataclass(frozen=True, slots=True)
class ProviderCallMetric:
    """Token and latency metadata for one provider call, without its content."""

    kind: CallKind
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    elapsed_seconds: float

    def __post_init__(self) -> None:
        if self.kind not in _VALID_CALL_KINDS:
            raise ValueError("kind must be main, classifier, or l4")
        for field_name in ("input_tokens", "output_tokens", "total_tokens"):
            value = getattr(self, field_name)
            if value is not None and value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be non-negative")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "elapsed_seconds": self.elapsed_seconds,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProviderCallMetric:
        return cls(
            kind=data["kind"],
            input_tokens=data.get("input_tokens"),
            output_tokens=data.get("output_tokens"),
            total_tokens=data.get("total_tokens"),
            elapsed_seconds=data["elapsed_seconds"],
        )


@dataclass(frozen=True, slots=True)
class CompactionMetric:
    """A compaction event observed in one trial's persisted JSONL."""

    trigger: str
    event_type: str
    completed: bool

    def __post_init__(self) -> None:
        if not self.trigger.strip():
            raise ValueError("trigger must not be blank")
        if not self.event_type.strip():
            raise ValueError("event_type must not be blank")

    def to_dict(self) -> dict[str, object]:
        return {
            "trigger": self.trigger,
            "event_type": self.event_type,
            "completed": self.completed,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> CompactionMetric:
        return cls(
            trigger=data["trigger"],
            event_type=data["event_type"],
            completed=data["completed"],
        )


@dataclass(frozen=True, slots=True)
class TrialResult:
    """The complete, content-free result of one isolated trial."""

    case_id: str
    arm: Arm
    model: str
    context_window: int
    status: str
    verifier_exit_code: int | None
    verifier_stdout_sha256: str | None
    verifier_stderr_sha256: str | None
    provider_calls: tuple[ProviderCallMetric, ...] = ()
    compactions: tuple[CompactionMetric, ...] = ()
    boundary_event_count: int = 0
    task_hash_changed_count: int = 0
    agent_turn_telemetry_count: int = 0
    usage_complete: bool = False
    elapsed_seconds: float = 0.0
    repetition: int = 1
    max_tool_rounds: int = 6
    max_provider_calls: int = 12
    max_turn_seconds: float = 240.0
    provider_timeout_seconds: float = 180.0
    budget_window_type: str = "simulated_budget_window"
    artifact_paths: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must not be blank")
        if not self.model.strip():
            raise ValueError("model must not be blank")
        if self.context_window <= 0:
            raise ValueError("context_window must be positive")
        if not self.status.strip():
            raise ValueError("status must not be blank")
        if self.boundary_event_count < 0:
            raise ValueError("boundary_event_count must be non-negative")
        if self.task_hash_changed_count < 0:
            raise ValueError("task_hash_changed_count must be non-negative")
        if self.agent_turn_telemetry_count < 0:
            raise ValueError("agent_turn_telemetry_count must be non-negative")
        if self.elapsed_seconds < 0:
            raise ValueError("elapsed_seconds must be non-negative")
        if self.repetition <= 0:
            raise ValueError("repetition must be positive")
        if self.max_tool_rounds <= 0:
            raise ValueError("max_tool_rounds must be positive")
        if self.max_provider_calls <= 0:
            raise ValueError("max_provider_calls must be positive")
        if self.max_turn_seconds <= 0:
            raise ValueError("max_turn_seconds must be positive")
        if self.provider_timeout_seconds <= 0:
            raise ValueError("provider_timeout_seconds must be positive")
        if self.budget_window_type != "simulated_budget_window":
            raise ValueError("budget_window_type must be simulated_budget_window")
        if any(not key.strip() or not value.strip() for key, value in self.artifact_paths.items()):
            raise ValueError("artifact_paths must contain non-blank keys and values")

    def to_dict(self) -> dict[str, object]:
        return {
            "case_id": self.case_id,
            "arm": self.arm.value,
            "model": self.model,
            "context_window": self.context_window,
            "status": self.status,
            "verifier_exit_code": self.verifier_exit_code,
            "verifier_stdout_sha256": self.verifier_stdout_sha256,
            "verifier_stderr_sha256": self.verifier_stderr_sha256,
            "provider_calls": [metric.to_dict() for metric in self.provider_calls],
            "compactions": [metric.to_dict() for metric in self.compactions],
            "boundary_event_count": self.boundary_event_count,
            "task_hash_changed_count": self.task_hash_changed_count,
            "agent_turn_telemetry_count": self.agent_turn_telemetry_count,
            "usage_complete": self.usage_complete,
            "elapsed_seconds": self.elapsed_seconds,
            "repetition": self.repetition,
            "max_tool_rounds": self.max_tool_rounds,
            "max_provider_calls": self.max_provider_calls,
            "max_turn_seconds": self.max_turn_seconds,
            "provider_timeout_seconds": self.provider_timeout_seconds,
            "budget_window_type": self.budget_window_type,
            "artifact_paths": dict(self.artifact_paths),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrialResult:
        return cls(
            case_id=data["case_id"],
            arm=Arm(data["arm"]),
            model=data["model"],
            context_window=data["context_window"],
            status=data["status"],
            verifier_exit_code=data.get("verifier_exit_code"),
            verifier_stdout_sha256=data.get("verifier_stdout_sha256"),
            verifier_stderr_sha256=data.get("verifier_stderr_sha256"),
            provider_calls=tuple(
                ProviderCallMetric.from_dict(metric) for metric in data.get("provider_calls", ())
            ),
            compactions=tuple(
                CompactionMetric.from_dict(metric) for metric in data.get("compactions", ())
            ),
            boundary_event_count=data.get("boundary_event_count", 0),
            task_hash_changed_count=data.get("task_hash_changed_count", 0),
            agent_turn_telemetry_count=data.get("agent_turn_telemetry_count", 0),
            usage_complete=data.get("usage_complete", False),
            elapsed_seconds=data.get("elapsed_seconds", 0.0),
            repetition=data.get("repetition", 1),
            max_tool_rounds=data.get("max_tool_rounds", 6),
            max_provider_calls=data.get("max_provider_calls", 12),
            max_turn_seconds=data.get("max_turn_seconds", 90.0),
            provider_timeout_seconds=data.get("provider_timeout_seconds", 120.0),
            budget_window_type=data.get("budget_window_type", "simulated_budget_window"),
            artifact_paths=data.get("artifact_paths", {}),
        )


@dataclass(frozen=True, slots=True)
class ComparisonRow:
    """One paired arm comparison for a case and metric."""

    case_id: str
    comparison: str
    metric: str
    left_value: float | None
    right_value: float | None
    delta: float | None

    def __post_init__(self) -> None:
        if not self.case_id.strip():
            raise ValueError("case_id must not be blank")
        if not self.comparison.strip():
            raise ValueError("comparison must not be blank")
        if not self.metric.strip():
            raise ValueError("metric must not be blank")
