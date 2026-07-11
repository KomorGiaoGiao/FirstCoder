"""Context-management metrics for benchmark session logs."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from firstcoder.context.store import JsonlSessionStore
from firstcoder.context.token_budget import estimate_text_tokens


def collect_context_metrics(transcript_path: str | Path | None) -> dict[str, Any]:
    """Summarize context compaction behavior from a FirstCoder JSONL transcript."""

    if transcript_path is None:
        return {}
    path = Path(transcript_path)
    if not path.exists():
        return {"transcript_path": str(path), "transcript_exists": False}

    session_id = path.stem
    store = JsonlSessionStore(path.parents[1])
    events = store.list_events(session_id)
    view = store.rebuild_session_view(session_id)
    parts = [part for message in view.messages for part in message.parts]
    compactions = [event for event in events if event.type == "compaction_completed"]
    l4_events = [event for event in events if event.type == "llm_compaction_completed"]
    boundary_events = [event for event in events if event.type == "task_boundary_observed"]
    tool_result_parts = list(_tool_result_parts(events))

    compaction_triggers = Counter(str(event.payload.get("trigger") or "") for event in compactions)
    compaction_changed_parts = sum(
        int((event.payload.get("event") or {}).get("changed_parts") or 0)
        for event in compactions
        if isinstance(event.payload.get("event"), dict)
    )
    max_before_tokens = max(
        [int(event.payload.get("before_tokens") or 0) for event in compactions]
        + [sum(estimate_text_tokens(part.content) for part in parts)]
    )
    compaction_before_tokens, compaction_after_tokens, level_savings = _compaction_token_metrics(
        compactions
    )
    archive_count, archive_bytes = _archive_metrics(path.parents[1], session_id)
    tool_names = _tool_result_tool_name_counts(tool_result_parts)
    content_types = _effective_tool_result_content_types(view)

    return {
        "transcript_path": str(path),
        "transcript_exists": True,
        "events": len(events),
        "messages": len(view.messages),
        "parts": len(parts),
        "estimated_tokens": sum(estimate_text_tokens(part.content) for part in parts),
        "max_compaction_before_tokens": max_before_tokens,
        "compaction_events": len(compactions),
        "compaction_triggers": dict(compaction_triggers),
        "compaction_changed_parts": compaction_changed_parts,
        "compaction_before_tokens_total": compaction_before_tokens,
        "compaction_after_tokens_total": compaction_after_tokens,
        "compaction_token_savings_total": max(0, compaction_before_tokens - compaction_after_tokens),
        "compaction_level_token_savings": dict(level_savings),
        "compacted_parts": sum(1 for part in parts if part.metadata.get("compaction_state")),
        "l4_events": len(l4_events),
        "l4_completion_count": sum(1 for event in l4_events if _l4_completed(event.payload)),
        "archive_count": archive_count,
        "archive_bytes": archive_bytes,
        "retrieve_archive_success_count": sum(
            1
            for part in tool_result_parts
            if part[0] == "retrieve_archive" and part[1].get("ok") is True
        ),
        "tool_result_tool_names": dict(tool_names),
        "tool_result_content_types": dict(content_types),
        "source_reread_count": _source_reread_count(tool_result_parts),
        "task_boundary_events": len(boundary_events),
        "task_switch_triggers": sum(1 for event in boundary_events if event.payload.get("should_trigger_compaction")),
    }


def _compaction_token_metrics(
    compactions: list[Any],
) -> tuple[int, int, Counter[str]]:
    """Read both legacy top-level and v2 nested compaction token fields."""

    before_total = 0
    after_total = 0
    level_savings: Counter[str] = Counter()
    for event in compactions:
        payload = event.payload
        nested = payload.get("event")
        nested_event = nested if isinstance(nested, dict) else {}
        before = _nonnegative_int(payload.get("before_tokens"))
        after = _nonnegative_int(payload.get("after_tokens"))
        if before is None:
            before = _nonnegative_int(nested_event.get("before_tokens"))
        if after is None:
            after = _nonnegative_int(nested_event.get("after_tokens"))
        before_total += before or 0
        after_total += after or 0

        metrics = nested_event.get("level_metrics")
        if not isinstance(metrics, dict):
            continue
        for level, metric in metrics.items():
            if not isinstance(level, str) or not level or not isinstance(metric, dict):
                continue
            saved = _nonnegative_int(metric.get("saved_tokens"))
            if saved is None:
                level_before = _nonnegative_int(metric.get("before_tokens"))
                level_after = _nonnegative_int(metric.get("after_tokens"))
                if level_before is None or level_after is None:
                    continue
                saved = max(0, level_before - level_after)
            level_savings[level] += saved
    return before_total, after_total, level_savings


def _archive_metrics(root: Path, session_id: str) -> tuple[int, int]:
    """Count the immutable text payloads under this session's archive folder."""

    directory = root / "archives" / session_id
    if not directory.is_dir():
        return 0, 0
    count = 0
    size = 0
    for path in directory.glob("*.txt"):
        try:
            if not path.is_file():
                continue
            count += 1
            size += path.stat().st_size
        except OSError:
            # Metrics must not make a partially removed archive break eval.
            continue
    return count, size


def _tool_result_parts(events: list[Any]):
    """Yield transcript-order ``(tool_name, metadata)`` pairs fail-open."""

    for event in events:
        if event.type != "tool_result":
            continue
        raw_parts = event.payload.get("parts")
        if not isinstance(raw_parts, list):
            continue
        for raw_part in raw_parts:
            if not isinstance(raw_part, dict) or raw_part.get("kind") != "tool_result":
                continue
            metadata = raw_part.get("metadata")
            if not isinstance(metadata, dict):
                metadata = {}
            tool_name = metadata.get("tool_name")
            yield (str(tool_name) if isinstance(tool_name, str) and tool_name else "unknown", metadata)


def _tool_result_tool_name_counts(tool_result_parts: list[tuple[str, dict[str, Any]]]) -> Counter[str]:
    tool_names: Counter[str] = Counter()
    for tool_name, metadata in tool_result_parts:
        tool_names[tool_name] += 1
    return tool_names


def _effective_tool_result_content_types(view) -> Counter[str]:
    """Count content types from replacement-aware effective tool-result parts.

    Tool-name volume describes every raw execution in the transcript.  Route
    content types, however, are written by L2 replacement metadata, so they
    must be read from the replayed ``SessionView`` rather than raw events.
    """

    content_types: Counter[str] = Counter()
    for message in view.messages:
        for part in message.parts:
            if part.kind != "tool_result":
                continue
            content_type = part.metadata.get("content_type")
            if isinstance(content_type, str) and content_type:
                content_types[content_type] += 1
    return content_types


def _source_reread_count(tool_result_parts: list[tuple[str, dict[str, Any]]]) -> int:
    """Count successful built-in source targets after their first read.

    This intentionally uses only result metadata, rather than attempting to
    infer paths from tool-call text or shell output.  A ``view`` contributes
    its structured ``data.path``; ``read_multi`` contributes every structured
    ``data.files[*].path`` in transcript order.
    """

    seen_targets: set[str] = set()
    rereads = 0
    for tool_name, metadata in tool_result_parts:
        if metadata.get("ok") is not True:
            continue
        for target in _source_targets(tool_name, metadata.get("data")):
            if target in seen_targets:
                rereads += 1
            else:
                seen_targets.add(target)
    return rereads


def _source_targets(tool_name: str, data: object) -> tuple[str, ...]:
    if not isinstance(data, dict):
        return ()
    if tool_name == "view":
        path = _source_path(data.get("path"))
        return (path,) if path is not None else ()
    if tool_name != "read_multi":
        return ()
    files = data.get("files")
    if not isinstance(files, list):
        return ()
    paths: list[str] = []
    for file_data in files:
        if not isinstance(file_data, dict):
            continue
        path = _source_path(file_data.get("path"))
        if path is not None and path not in paths:
            paths.append(path)
    return tuple(paths)


def _source_path(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized or None


def _l4_completed(payload: dict[str, Any]) -> bool:
    nested = payload.get("event")
    nested_event = nested if isinstance(nested, dict) else {}
    return (nested_event.get("status") or payload.get("status")) == "success"


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None
