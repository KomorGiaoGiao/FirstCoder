"""工具结果归档能力。

这一层只处理“大结果完整内容落盘，prompt 中保留占位符”。它不决定什么时候触发
压缩，也不移动 checkpoint 边界；这些编排留给后续 compaction pipeline。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from firstcoder.context.identity import content_fingerprint, new_archive_id
from firstcoder.context.models import MessagePart, utc_now_iso
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.context.versions import COMPACTION_STRATEGY_VERSION


@dataclass(slots=True)
class ToolResultArchive:
    """把工具结果原文落盘，并生成可投影给模型的 placeholder。"""

    root: str | Path
    preview_chars: int = 500

    def archive_part(
        self,
        *,
        session_id: str,
        part: MessagePart,
        summary: str | None = None,
        archive_id: str | None = None,
    ) -> MessagePart:
        """归档一个 `tool_result` part。

        已经归档过的 part 直接原样返回，避免 resume 或重复压缩时反复落盘。
        """

        if part.metadata.get("compaction_state") == "archived" and part.metadata.get("archive_id"):
            return part
        if part.kind != "tool_result":
            raise ValueError("ToolResultArchive only accepts tool_result parts")

        resolved_archive_id = archive_id or str(part.metadata.get("archive_id") or new_archive_id())
        archive_dir = self._archive_dir(session_id)
        archive_dir.mkdir(parents=True, exist_ok=True)

        text_path = archive_dir / f"{resolved_archive_id}.txt"
        metadata_path = archive_dir / f"{resolved_archive_id}.json"
        original_content = part.content
        original_tokens = estimate_text_tokens(original_content)
        preview = original_content[: self.preview_chars]
        preview_tokens = estimate_text_tokens(preview)
        resolved_summary = summary or _default_summary(part, original_tokens=original_tokens)

        if not text_path.exists():
            text_path.write_text(original_content, encoding="utf-8")

        archive_metadata = {
            "archive_id": resolved_archive_id,
            "session_id": session_id,
            "part_id": part.id,
            "message_id": part.message_id,
            "tool_name": part.metadata.get("tool_name"),
            "tool_call_id": part.metadata.get("tool_call_id"),
            "content_fingerprint": content_fingerprint(original_content),
            "original_tokens": original_tokens,
            "preview_tokens": preview_tokens,
            "summary": resolved_summary,
            "created_at": utc_now_iso(),
            "compaction_strategy_version": COMPACTION_STRATEGY_VERSION,
        }
        if not metadata_path.exists():
            metadata_path.write_text(
                json.dumps(archive_metadata, ensure_ascii=False, sort_keys=True, indent=2),
                encoding="utf-8",
            )

        metadata: dict[str, Any] = dict(part.metadata)
        metadata.update(
            {
                "archive_id": resolved_archive_id,
                "archive_path": str(text_path),
                "archive_metadata_path": str(metadata_path),
                "summary": resolved_summary,
                "preview": preview,
                "original_tokens": original_tokens,
                "preview_tokens": preview_tokens,
                "content_fingerprint": content_fingerprint(original_content),
                "compaction_state": "archived",
                "compacted_by": "archive",
                "compacted_at": archive_metadata["created_at"],
                "compaction_strategy_version": COMPACTION_STRATEGY_VERSION,
            }
        )
        return MessagePart(
            id=part.id,
            message_id=part.message_id,
            kind=part.kind,
            content=_placeholder_text(
                archive_id=resolved_archive_id,
                summary=resolved_summary,
                preview=preview,
                original_tokens=original_tokens,
                preview_tokens=preview_tokens,
            ),
            metadata=metadata,
        )

    def _archive_dir(self, session_id: str) -> Path:
        return Path(self.root) / ".firstcoder" / "archives" / session_id


def _default_summary(part: MessagePart, *, original_tokens: int) -> str:
    tool_name = str(part.metadata.get("tool_name") or "tool")
    return f"{tool_name} 输出过大，已归档。原始估算 {original_tokens} tokens。"


def _placeholder_text(
    *,
    archive_id: str,
    summary: str,
    preview: str,
    original_tokens: int,
    preview_tokens: int,
) -> str:
    return "\n".join(
        [
            "[Tool result archived]",
            f"archive_id={archive_id}",
            f"summary={summary}",
            f"original_tokens={original_tokens}",
            f"preview_tokens={preview_tokens}",
            f"preview={preview}",
        ]
    )
