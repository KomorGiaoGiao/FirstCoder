"""工具结果进入会话历史前的标准化处理。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from firstcoder.context.identity import content_fingerprint
from firstcoder.context.models import MessagePart
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.tools.types import ToolResult


CompactionState = Literal[
    "raw",
    "archived",
    "micro_compacted",
    "route_compacted",
    "checkpointed",
    "pinned",
]


@dataclass(slots=True)
class NormalizedToolResult:
    """压缩和恢复层可理解的工具结果格式。"""

    tool_name: str
    tool_call_id: str
    ok: bool
    content: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    token_estimate: int = 0
    content_fingerprint: str = ""
    display_preview: str = ""
    archive_id: str | None = None
    task_hash: str | None = None
    compaction_state: CompactionState = "raw"
    created_turn: int | None = None

    def to_part(self, *, id: str, message_id: str) -> MessagePart:
        """转换成可写入 `AgentMessage.parts` 的 tool_result part。"""

        return MessagePart(
            id=id,
            message_id=message_id,
            kind="tool_result",
            content=self.content,
            metadata={
                "tool_name": self.tool_name,
                "tool_call_id": self.tool_call_id,
                "ok": self.ok,
                "data": self.data,
                "error": self.error,
                "token_estimate": self.token_estimate,
                "content_fingerprint": self.content_fingerprint,
                "display_preview": self.display_preview,
                "archive_id": self.archive_id,
                "task_hash": self.task_hash,
                "compaction_state": self.compaction_state,
                "created_turn": self.created_turn,
            },
        )


def normalize_tool_result(
    result: ToolResult,
    *,
    tool_call_id: str,
    task_hash: str | None = None,
    created_turn: int | None = None,
) -> NormalizedToolResult:
    """把工具层结果补齐 context 层需要的追踪字段。"""

    preview = result.content[:500]
    return NormalizedToolResult(
        tool_name=result.name,
        tool_call_id=tool_call_id,
        ok=result.ok,
        content=result.content,
        data=dict(result.data),
        error=result.error,
        token_estimate=estimate_text_tokens(result.content),
        content_fingerprint=content_fingerprint(result.content),
        display_preview=preview,
        task_hash=task_hash,
        created_turn=created_turn,
    )
