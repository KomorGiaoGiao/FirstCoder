"""Private capture and replay primitives for real task-A benchmark context."""

from __future__ import annotations

from pathlib import Path
import shutil

from firstcoder.context.store import JsonlSessionStore


def clone_recorded_session(
    *,
    source_data_root: str | Path,
    destination_data_root: str | Path,
    session_id: str,
) -> JsonlSessionStore:
    """Copy one captured session store for an isolated task-B trial.

    The caller owns both directories and must remove them after the trial.  Only
    session data is copied: a task-A project is deliberately never accepted or
    transferred by this function.
    """

    source = Path(source_data_root)
    destination = Path(destination_data_root)
    session_path = source / "sessions" / f"{session_id}.jsonl"
    if not session_path.is_file():
        raise FileNotFoundError(f"recorded session does not exist: {session_path}")
    if destination.exists():
        raise FileExistsError(f"recorded session destination already exists: {destination}")
    shutil.copytree(source, destination)
    return JsonlSessionStore(destination)
