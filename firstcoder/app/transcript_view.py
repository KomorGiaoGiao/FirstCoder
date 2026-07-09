"""Transcript entry classification and CSS helpers."""

from __future__ import annotations

from firstcoder.app.activity_view import tool_event_status
from firstcoder.app.tui_state import TuiEntryKind, TuiTranscriptEntry


def looks_like_markdown_response(line: str) -> bool:
    return not looks_like_tool_display_line(line)


def looks_like_tool_display_line(line: str) -> bool:
    return line.startswith(("Tool call:", "Tool result:"))


def normalize_stream_text(text: str) -> str:
    return text.strip()


def display_line_kind(line: str) -> TuiEntryKind:
    if line.startswith(("Tool call:", "Tool result:")):
        return TuiEntryKind.TOOL
    return TuiEntryKind.SYSTEM


def display_line_status(line: str) -> str | None:
    if line.startswith("Tool call:"):
        return "running"
    if line.startswith("Tool result:"):
        return "success"
    return None


def entry_classes(entry: TuiTranscriptEntry) -> str:
    base = "message"
    if entry.kind == TuiEntryKind.SYSTEM:
        return f"{base} system-message"
    if entry.kind == TuiEntryKind.COMMAND:
        return f"{base} command-message"
    if entry.kind == TuiEntryKind.USER:
        return f"{base} user-message"
    if entry.kind == TuiEntryKind.ASSISTANT:
        return f"{base} assistant-message"
    if entry.kind == TuiEntryKind.REASONING:
        return f"{base} reasoning-message"
    if entry.kind == TuiEntryKind.PERMISSION:
        if entry.status == "permission_requested":
            return f"{base} permission-message permission-requested"
        return f"{base} permission-message"
    if entry.kind == TuiEntryKind.ERROR:
        return f"{base} error-message"
    if entry.kind == TuiEntryKind.TOOL:
        if entry.status == "running":
            return f"{base} tool-message tool-running"
        if entry.status == "success":
            return f"{base} tool-message tool-done"
        if entry.status in {"error", "denied", "failed"}:
            return f"{base} tool-message tool-failed"
        if entry.status == "skipped":
            return f"{base} tool-message tool-skipped"
        return f"{base} tool-message"
    return f"{base} system-message"


def entry_plain_text(entry: TuiTranscriptEntry) -> str:
    if entry.kind in {TuiEntryKind.USER, TuiEntryKind.ASSISTANT, TuiEntryKind.TOOL, TuiEntryKind.REASONING}:
        return f"{entry.label}\n  {entry.body}"
    return entry.body


def entry_markdown_text(entry: TuiTranscriptEntry) -> str:
    return f"{entry.label}\n\n{entry.body}"


def display_line_classes(line: str) -> str:
    if line.startswith("Tool call:"):
        return "message tool-message tool-running"
    if line.startswith("Tool result:"):
        return "message tool-message tool-done"
    return "message system-message"


def tool_event_classes(event) -> str:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "started":
        return "message tool-message tool-running"
    if kind == "finished":
        result = getattr(event, "result", None)
        if getattr(result, "ok", False):
            return "message tool-message tool-done"
        return "message tool-message tool-failed"
    if kind == "permission_requested":
        return "message permission-message"
    if kind == "denied":
        return "message tool-message tool-failed"
    return "message tool-message"


def tool_event_entry_kind(event) -> TuiEntryKind:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "permission_requested":
        return TuiEntryKind.PERMISSION
    return TuiEntryKind.TOOL
