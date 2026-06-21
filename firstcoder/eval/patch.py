"""Git helpers for benchmark patch generation."""

from __future__ import annotations

import subprocess
from pathlib import Path


def ensure_clean_repo(repo_path: str | Path) -> None:
    repo = Path(repo_path)
    result = _git(["status", "--porcelain"], repo)
    if result.stdout.strip():
        raise RuntimeError(f"Repository is dirty before benchmark run: {repo}")


def collect_git_diff(repo_path: str | Path) -> str:
    repo = Path(repo_path)
    staged = _git(["diff", "--cached", "--binary"], repo).stdout
    unstaged = _git(["diff", "--binary"], repo).stdout
    return staged + unstaged


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        text=True,
        capture_output=True,
    )
