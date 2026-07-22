"""Session-scoped retrieval for archived tool-result originals.

The compaction pipeline stores an immutable original alongside a compact
placeholder.  This tool is deliberately created per session so an agent can
only retrieve archives belonging to its current session.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from firstcoder.context.archive import ArchiveIntegrityError, ToolResultArchive
from firstcoder.providers.types import ToolDefinition
from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result

_MAX_CHARS_LIMIT = 12_000
_DEFAULT_MAX_CHARS = 6_000
_OMITTED = "[... omitted ...]"


def _bounded_max_chars(value: int) -> int | None:
    """Normalize the public character budget without allowing unbounded output."""
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_CHARS_LIMIT:
        return None
    return value


def _clip(text: str, max_chars: int) -> tuple[str, bool]:
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars], True


def _query_projection(raw: str, query: str, max_chars: int) -> tuple[str, int, bool]:
    """Return matching numbered line windows, joined within the total budget."""
    lines = raw.splitlines()
    needle = query.casefold()
    matching_indexes = [index for index, line in enumerate(lines) if needle in line.casefold()]
    if not matching_indexes:
        return "", 0, False

    windows: list[tuple[int, int]] = []
    for index in matching_indexes:
        start = max(0, index - 2)
        end = min(len(lines), index + 3)
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], end))
        else:
            windows.append((start, end))

    blocks: list[str] = []
    previous_end = 0
    for start, end in windows:
        if blocks and start > previous_end:
            blocks.append("[... omitted ...]")
        blocks.extend(f"{line_number + 1}: {lines[line_number]}" for line_number in range(start, end))
        previous_end = end
    projection = "\n".join(blocks)
    if len(projection) <= max_chars:
        return projection, len(matching_indexes), False
    # A blind prefix makes it unclear whether further matches exist.  Reserve a
    # stable omission marker whenever the caller's budget can accommodate it.
    if max_chars >= len(_OMITTED):
        return projection[: max_chars - len(_OMITTED)] + _OMITTED, len(matching_indexes), True
    return projection[:max_chars], len(matching_indexes), True


def _diagnostic(record, raw: str, max_chars: int) -> tuple[str, bool]:
    """Build a bounded browse hint with metadata and both raw-content ends.

    A retrieval with no query must be useful without accidentally becoming a
    second, unbounded archive-read API.  Very small limits retain as much of
    the instruction as fits; larger limits include stable metadata and evenly
    allocated head/tail excerpts.
    """
    instruction = "Use a query or full=true to retrieve the original content."
    if max_chars < len(instruction):
        return _clip(instruction, max_chars)

    metadata = f"Archive metadata: archive_id={record.archive_id}; " f"original_tokens={record.original_tokens}; original_chars={record.original_chars}."
    base = f"{metadata}\n{instruction}"
    if len(base) > max_chars:
        return instruction, True

    remaining = max_chars - len(base)
    excerpt_overhead = len("\nHead:\n\nTail:\n")
    if remaining < excerpt_overhead + 2 or not raw:
        return base, bool(raw)

    excerpt_budget = remaining - excerpt_overhead
    head_chars = excerpt_budget // 2
    tail_chars = excerpt_budget - head_chars
    if len(raw) <= excerpt_budget:
        # Keep the same labels so callers do not have to infer which form was
        # returned, even when the whole original happens to fit the excerpt.
        head = raw[:head_chars]
        tail = raw[head_chars:]
        truncated = False
    else:
        head = raw[:head_chars]
        tail = raw[-tail_chars:]
        truncated = True
    rendered = f"{base}\nHead:\n{head}\nTail:\n{tail}"
    content, overflow = _clip(rendered, max_chars)
    return content, truncated or overflow


def _error(message: str) -> ToolResult:
    """Return a safe, user-actionable error without leaking local paths."""
    return make_error_result("retrieve_archive", message)


def create_retrieve_archive_tool(
    *,
    archive_root: str | Path,
    session_id: str,
    current_turn: Callable[[], int],
) -> Tool:
    """Create the session-bound ``retrieve_archive`` tool.

    ``current_turn`` is intentionally a callback: the session writer advances
    after tool construction, and each successful retrieval must protect the
    result until the turn in which it was actually retrieved.
    """

    def retrieve_archive(
        *,
        archive_id: str,
        query: str | None = None,
        max_chars: int = _DEFAULT_MAX_CHARS,
        full: bool = False,
        **kwargs: Any,
    ) -> ToolResult:
        if kwargs:
            names = ", ".join(sorted(kwargs))
            return _error(f"Unexpected argument(s): {names}.")
        budget = _bounded_max_chars(max_chars)
        if not isinstance(archive_id, str) or not archive_id:
            return _error("archive_id is required.")
        if budget is None:
            return _error("max_chars must be an integer between 1 and 12000.")
        if query is not None and not isinstance(query, str):
            return _error("query must be a string when provided.")
        if not isinstance(full, bool):
            return _error("full must be a boolean.")

        try:
            record, raw = ToolResultArchive(archive_root).read(session_id, archive_id)
        except (ArchiveIntegrityError, FileNotFoundError, OSError, ValueError):
            return _error("The requested archive is unavailable or failed integrity validation.")

        normalized_query = query.strip() if query is not None else ""
        if full:
            content, truncated = _clip(raw, budget)
            match_count = 0
        elif normalized_query:
            content, match_count, truncated = _query_projection(raw, normalized_query, budget)
            if match_count == 0:
                content, truncated = _clip("No matching lines found.", budget)
        else:
            content, truncated = _diagnostic(record, raw, budget)
            match_count = 0

        data = {
            "archive_retrieval": True,
            "compaction_protected_until_turn": current_turn(),
            "archive_id": archive_id,
            "query": normalized_query or None,
            "full": bool(full),
            "match_count": match_count,
            "returned_chars": len(content),
            "truncated": truncated,
            "original_tokens": record.original_tokens,
            "content_sha256": record.content_sha256,
        }
        return make_text_result("retrieve_archive", content, **data)

    return Tool(
        definition=ToolDefinition(
            name="retrieve_archive",
            description=("Retrieve the original content of a compacted tool result from this " "session. Use query for matching line windows or full=true for raw text."),
            parameters={
                "type": "object",
                "properties": {
                    "archive_id": {"type": "string", "description": "Archive identifier."},
                    "query": {"type": "string", "description": "Case-insensitive literal search."},
                    "max_chars": {
                        "type": "integer",
                        "description": "Maximum returned characters (capped at 12000).",
                        "default": _DEFAULT_MAX_CHARS,
                        "minimum": 1,
                        "maximum": _MAX_CHARS_LIMIT,
                    },
                    "full": {"type": "boolean", "description": "Return original content.", "default": False},
                },
                "required": ["archive_id"],
                "additionalProperties": False,
            },
        ),
        executor=retrieve_archive,
    )
