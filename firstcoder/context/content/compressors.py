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
    """Return the L1 representation for an old-task dialogue part.

    L1 is deliberate forgetting, rather than a tiny natural-language summary.
    The original event remains in JSONL, but the effective view has no visible
    text for this part.  ``ContextBuilder`` emits one aggregate marker for a
    tail containing any such part.
    """

    metadata = _compacted_metadata(
        part,
        state="trimmed",
        compacted_by="l1_old_task_dialogue",
    )
    return MessagePart(
        id=part.id,
        message_id=part.message_id,
        kind=part.kind,
        content="",
        metadata=metadata,
    )


class PlainTextRouteCompressor:
    """派生工具输出的确定性 fallback compressor。

    专用 search、diff、build、json、code、html compressor 不适用时，保留首尾和
    明确的 token 元数据。它只由 L2 的 tool-result 路由调用，不能用于普通对话或
    fresh source read。
    """

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        preview = part.content[: context.preview_chars]
        tail_preview = part.content[-context.preview_chars :] if len(part.content) > context.preview_chars else ""
        preview_tokens = estimate_text_tokens(preview)
        tail_preview_tokens = estimate_text_tokens(tail_preview) if tail_preview else 0
        content = "\n".join(
            [
                "[Derived tool result compacted]",
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
            compacted_by="l2_current_task_cold",
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
