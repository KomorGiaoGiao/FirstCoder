"""checkpoint 与简单 resume 投影所需的数据结构。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from firstcoder.context.identity import new_checkpoint_id
from firstcoder.context.models import utc_now_iso
from firstcoder.context.versions import CHECKPOINT_STRATEGY_VERSION


@dataclass(slots=True)
class Checkpoint:
    """已经被摘要折叠的旧历史边界。

    checkpoint 只记录“旧历史摘要”和“recent tail 从哪里开始”。它不负责生成摘要，
    也不因为 task hash 切换自动移动边界；边界由后续 compaction pipeline 明确写入。
    """

    id: str
    session_id: str
    summary: str
    tail_start_message_id: str
    covered_until_message_id: str
    source_fingerprint: str
    created_at: str = ""
    sequence: int = 0
    strategy_version: str = CHECKPOINT_STRATEGY_VERSION
    metadata: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not self.id:
            self.id = new_checkpoint_id()
        if not self.created_at:
            self.created_at = utc_now_iso()
        if self.metadata is None:
            self.metadata = {}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Checkpoint":
        return cls(
            id=str(value["id"]),
            session_id=str(value["session_id"]),
            summary=str(value.get("summary", "")),
            tail_start_message_id=str(value["tail_start_message_id"]),
            covered_until_message_id=str(value["covered_until_message_id"]),
            source_fingerprint=str(value["source_fingerprint"]),
            created_at=str(value.get("created_at") or utc_now_iso()),
            sequence=int(value.get("sequence") or 0),
            strategy_version=str(value.get("strategy_version") or CHECKPOINT_STRATEGY_VERSION),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "summary": self.summary,
            "tail_start_message_id": self.tail_start_message_id,
            "covered_until_message_id": self.covered_until_message_id,
            "source_fingerprint": self.source_fingerprint,
            "created_at": self.created_at,
            "sequence": self.sequence,
            "strategy_version": self.strategy_version,
            "metadata": dict(self.metadata or {}),
        }


class CheckpointIndex:
    """从一组 checkpoint 中选择当前可用的 latest checkpoint。"""

    def __init__(self, checkpoints: list[Checkpoint]) -> None:
        self.checkpoints = list(checkpoints)

    def latest(self) -> Checkpoint | None:
        if not self.checkpoints:
            return None

        return max(
            self.checkpoints,
            key=lambda checkpoint: (checkpoint.sequence, checkpoint.created_at, checkpoint.id),
        )


def checkpoint_summary_content(checkpoint: Checkpoint) -> str:
    return "\n".join(["[Checkpoint summary]", checkpoint.summary])
