"""Generic coding benchmark task models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class CodingTask:
    """A repository-level coding task for a benchmark adapter."""

    instance_id: str
    repo_path: Path
    problem_statement: str
    base_commit: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CodingTaskResult:
    """The patch and trace produced for one benchmark task."""

    instance_id: str
    model_name_or_path: str
    model_patch: str
    transcript_path: Path | None = None
    raw_response: str = ""
    context_metrics: dict[str, Any] = field(default_factory=dict)

    def to_prediction_dict(self) -> dict[str, str]:
        return {
            "instance_id": self.instance_id,
            "model_name_or_path": self.model_name_or_path,
            "model_patch": self.model_patch,
        }
