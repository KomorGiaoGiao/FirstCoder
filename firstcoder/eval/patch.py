"""Git helpers for benchmark patch generation."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path


def ensure_clean_repo(repo_path: str | Path) -> None:
    repo = Path(repo_path)
    result = _git(["status", "--porcelain"], repo)
    if result.stdout.strip():
        raise RuntimeError(f"Repository is dirty before benchmark run: {repo}")


def collect_git_diff(repo_path: str | Path, *, include_untracked: bool = False) -> str:
    repo = Path(repo_path)
    if not _is_git_worktree(repo):
        return ""
    if include_untracked:
        return _collect_final_worktree_diff(repo)
    staged = _git(["diff", "--cached", "--binary"], repo).stdout
    unstaged = _git(["diff", "--binary"], repo).stdout
    return staged + unstaged


def _collect_final_worktree_diff(repo: Path) -> str:
    with tempfile.NamedTemporaryFile(prefix="firstcoder-index-") as index:
        env = {"GIT_INDEX_FILE": index.name}
        _git(["read-tree", "HEAD"], repo, env=env)
        _git(["add", "-A"], repo, env=env)
        return _git(["diff", "--cached", "--binary"], repo, env=env).stdout


def _is_git_worktree(repo: Path) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=repo,
            text=True,
            capture_output=True,
        )
    except FileNotFoundError:
        return False
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git(
    args: list[str],
    cwd: Path,
    *,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    command_env = os.environ.copy()
    if env:
        command_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=command_env,
        check=True,
        text=True,
        capture_output=True,
    )
