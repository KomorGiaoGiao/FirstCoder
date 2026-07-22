"""只读 transcript 构造。

transcript 从完整 event log 派生，默认不展开 archive 原文，也不导出 system prompt。
它是分享和预览的中间结构，不是可 resume snapshot。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from firstcoder.context.events import SessionEvent
from firstcoder.context.store import JsonlSessionStore
from firstcoder.session.catalog import SessionCatalog
from firstcoder.session.errors import SessionCorruptError, SessionEmptyError
from firstcoder.session.models import RedactionOptions, ShareOptions, Transcript, TranscriptEntry
from firstcoder.session.redaction import redact_text
from firstcoder.utils.text import ellipsis_truncate, optional_str


@dataclass(slots=True)
class TranscriptBuilder:
    store: JsonlSessionStore
    catalog: SessionCatalog | None = None

    def build(self, session_id: str, options: ShareOptions | None = None) -> Transcript:
        resolved = options or ShareOptions()
        catalog = self.catalog or SessionCatalog(self.store.root)
        record = catalog.get_session(session_id)
        if record.status == "corrupt":
            raise SessionCorruptError(record.error or f"session is corrupt: {session_id}")
        if record.status == "empty":
            raise SessionEmptyError(f"session is empty: {session_id}")
        redaction = RedactionOptions(
            redact_paths=resolved.redact_paths,
            redact_secrets=resolved.redact_secrets,
        )
        entries: list[TranscriptEntry] = []
        for event in self.store.list_events(session_id):
            entries.extend(_entries_from_event(event, options=resolved, redaction=redaction))
        return Transcript(session=record, entries=entries)


def _entries_from_event(
    event: SessionEvent,
    *,
    options: ShareOptions,
    redaction: RedactionOptions,
) -> list[TranscriptEntry]:
    if event.type in {"user_message", "assistant_message", "tool_result"}:
        return _message_entries(event, options=options, redaction=redaction)
    if event.type == "checkpoint_created":
        return [_checkpoint_entry(event, redaction=redaction)]
    if event.type == "compaction_completed" and options.include_compaction_metadata:
        return [_compaction_entry(event)]
    return []


def _message_entries(
    event: SessionEvent,
    *,
    options: ShareOptions,
    redaction: RedactionOptions,
) -> list[TranscriptEntry]:
    payload = event.payload
    message_id = str(payload.get("message_id") or "")
    entries: list[TranscriptEntry] = []
    for part in _payload_parts(payload):
        kind = str(part.get("kind") or "")
        if kind == "text":
            entries.append(
                TranscriptEntry(
                    role=_role_for_event(event.type),
                    title=_title_for_event(event.type),
                    content=redact_text(str(part.get("content") or ""), redaction),
                    message_id=message_id,
                    metadata=_entry_metadata(event, part),
                )
            )
        elif kind == "tool_call" and options.include_tool_calls:
            entries.append(_tool_call_entry(event, part, message_id=message_id, redaction=redaction))
        elif kind == "tool_result":
            entries.append(_tool_result_entry(event, part, message_id=message_id, options=options, redaction=redaction))
    return entries


def _payload_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    parts = payload.get("parts")
    if not isinstance(parts, list):
        return []
    return [part for part in parts if isinstance(part, dict)]


def _tool_call_entry(
    event: SessionEvent,
    part: dict[str, Any],
    *,
    message_id: str,
    redaction: RedactionOptions,
) -> TranscriptEntry:
    metadata = _part_metadata(part)
    tool_name = str(metadata.get("tool_name") or "tool")
    arguments = json.dumps(metadata.get("arguments") or {}, ensure_ascii=False, sort_keys=True)
    return TranscriptEntry(
        role="tool_call",
        title=f"Tool Call: {tool_name}",
        content=redact_text(arguments, redaction),
        message_id=message_id,
        metadata=_entry_metadata(event, part),
    )


def _tool_result_entry(
    event: SessionEvent,
    part: dict[str, Any],
    *,
    message_id: str,
    options: ShareOptions,
    redaction: RedactionOptions,
) -> TranscriptEntry:
    metadata = _part_metadata(part)
    tool_name = str(metadata.get("tool_name") or "tool")
    content = _tool_result_content(part, metadata=metadata, options=options, redaction=redaction)
    return TranscriptEntry(
        role="tool",
        title=f"Tool: {tool_name}",
        content=content,
        message_id=message_id,
        metadata=_entry_metadata(event, part),
    )


def _tool_result_content(
    part: dict[str, Any],
    *,
    metadata: dict[str, Any],
    options: ShareOptions,
    redaction: RedactionOptions,
) -> str:
    status = "success" if metadata.get("ok", True) else "failed"
    archive_id = optional_str(metadata.get("archive_id"))
    if archive_id:
        lines = [
            f"Status: {status}",
            f"Archive: {archive_id}",
        ]
        summary = optional_str(metadata.get("summary"))
        preview = optional_str(metadata.get("preview"))
        if summary:
            lines.append(f"Summary: {redact_text(summary, redaction)}")
        if preview and options.archive_mode == "preview_only":
            lines.append(f"Preview: {redact_text(ellipsis_truncate(preview, options.max_tool_result_chars), redaction)}")
        elif preview:
            lines.append(f"Preview: {redact_text(preview, redaction)}")
        return "\n".join(lines)

    if not options.include_tool_results:
        return f"Status: {status}\nSummary: tool result omitted for sharing"

    content = redact_text(str(part.get("content") or ""), redaction)
    return f"Status: {status}\n{ellipsis_truncate(content, options.max_tool_result_chars)}"


def _checkpoint_entry(event: SessionEvent, *, redaction: RedactionOptions) -> TranscriptEntry:
    checkpoint_id = str(event.payload.get("id") or "checkpoint")
    return TranscriptEntry(
        role="checkpoint",
        title=f"Checkpoint: {checkpoint_id}",
        content=redact_text(str(event.payload.get("summary") or ""), redaction),
        message_id=None,
        metadata={"event_id": event.id, "created_at": event.created_at},
    )


def _compaction_entry(event: SessionEvent) -> TranscriptEntry:
    payload = event.payload
    before = payload.get("before_tokens")
    after = payload.get("after_tokens")
    return TranscriptEntry(
        role="compaction",
        title="Compaction",
        content=(f"Trigger: {payload.get('trigger')}\n" f"Status: {payload.get('status')}\n" f"Reason: {payload.get('reason')}\n" f"Tokens: {before} -> {after}"),
        metadata={"event_id": event.id, "created_at": event.created_at},
    )


def _entry_metadata(event: SessionEvent, part: dict[str, Any]) -> dict[str, Any]:
    metadata = {
        "event_id": event.id,
        "created_at": event.created_at,
        "part_id": part.get("id"),
        "part_kind": part.get("kind"),
    }
    part_metadata = _part_metadata(part)
    if part_metadata.get("tool_call_id"):
        metadata["tool_call_id"] = part_metadata["tool_call_id"]
    if part_metadata.get("archive_id"):
        metadata["archive_id"] = part_metadata["archive_id"]
    return metadata


def _part_metadata(part: dict[str, Any]) -> dict[str, Any]:
    metadata = part.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    return {}


def _role_for_event(event_type: str) -> str:
    if event_type == "assistant_message":
        return "assistant"
    if event_type == "tool_result":
        return "tool"
    return "user"


def _title_for_event(event_type: str) -> str:
    if event_type == "assistant_message":
        return "Assistant"
    if event_type == "tool_result":
        return "Tool"
    return "User"
