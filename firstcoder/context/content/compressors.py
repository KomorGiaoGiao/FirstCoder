"""确定性内容压缩器。

这一层只做可预测的轻量替换，不调用 LLM。目标是先降低 token 占用，并用 metadata
记录压缩状态，避免后续 pipeline 对同一 part 反复处理。
"""

from __future__ import annotations

from typing import Any

from firstcoder.context.content.router import (
    RouteCompactResult,
    RouteContentType,
    RouteContext,
)
from firstcoder.context.identity import content_fingerprint
from firstcoder.context.models import MessagePart, utc_now_iso
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.context.versions import COMPACTION_STRATEGY_VERSION


def compact_old_task_part(part: MessagePart) -> MessagePart:
    metadata = _compacted_metadata(part, state="micro_compacted", compacted_by="l1_old_task")
    content = "\n".join(
        [
            "[Old task content compacted]",
            f"part_id={part.id}",
            f"original_tokens={metadata['original_tokens']}",
            f"task_hash={part.metadata.get('task_hash')}",
        ]
    )
    return MessagePart(
        id=part.id,
        message_id=part.message_id,
        kind=part.kind,
        content=content,
        metadata=metadata,
    )


def compact_cold_text_part(part: MessagePart, *, preview_chars: int = 160) -> MessagePart:
    preview = part.content[:preview_chars]
    metadata = _compacted_metadata(part, state="route_compacted", compacted_by="l3_current_task_cold")
    metadata["preview"] = preview
    metadata["preview_tokens"] = estimate_text_tokens(preview)
    content = "\n".join(
        [
            "[Current task cold content compacted]",
            f"part_id={part.id}",
            f"original_tokens={metadata['original_tokens']}",
            f"preview_tokens={metadata['preview_tokens']}",
            f"preview={preview}",
        ]
    )
    return MessagePart(
        id=part.id,
        message_id=part.message_id,
        kind=part.kind,
        content=content,
        metadata=metadata,
    )


class PlainTextRouteCompressor:
    """L3 路由框架的兼容压缩器。

    第 14 步会逐个补齐 search、diff、build、json、code、html 等专用压缩器。
    plain_text 先保留旧版 L3 的 preview 语义，确保 pipeline 接入 router 后行为不漂移。
    """

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        preview = part.content[: context.preview_chars]
        tail_preview = part.content[-context.preview_chars :] if len(part.content) > context.preview_chars else ""
        preview_tokens = estimate_text_tokens(preview)
        tail_preview_tokens = estimate_text_tokens(tail_preview) if tail_preview else 0
        content = "\n".join(
            [
                "[Current task cold content compacted]",
                f"part_id={part.id}",
                f"original_tokens={estimate_text_tokens(part.content)}",
                f"preview_tokens={preview_tokens}",
                f"preview={preview}",
                f"tail_preview_tokens={tail_preview_tokens}",
                f"tail_preview={tail_preview}",
            ]
        )
        return RouteCompactResult(
            content=content,
            content_type=RouteContentType.PLAIN_TEXT,
            compacted_by="l3_current_task_cold",
            metadata={
                "preview": preview,
                "preview_tokens": preview_tokens,
                "tail_preview": tail_preview,
                "tail_preview_tokens": tail_preview_tokens,
            },
        )


def _compacted_metadata(part: MessagePart, *, state: str, compacted_by: str) -> dict[str, Any]:
    metadata = dict(part.metadata)
    metadata.update(
        {
            "original_tokens": estimate_text_tokens(part.content),
            "content_fingerprint": content_fingerprint(part.content),
            "compaction_state": state,
            "compacted_by": compacted_by,
            "compacted_at": utc_now_iso(),
            "compaction_strategy_version": COMPACTION_STRATEGY_VERSION,
        }
    )
    return metadata
