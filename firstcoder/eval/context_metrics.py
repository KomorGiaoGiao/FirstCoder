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
        "compacted_parts": sum(1 for part in parts if part.metadata.get("compaction_state")),
        "l4_events": len(l4_events),
        "task_boundary_events": len(boundary_events),
        "task_switch_triggers": sum(1 for event in boundary_events if event.payload.get("should_trigger_compaction")),
    }
