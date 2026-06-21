"""Git helpers for benchmark patch generation."""

from __future__ import annotations

import os
import shutil
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
    if include_untracked:
        return _collect_diff_with_untracked(repo)
    staged = _git(["diff", "--cached", "--binary"], repo).stdout
    unstaged = _git(["diff", "--binary"], repo).stdout
    return staged + unstaged


def _collect_diff_with_untracked(repo: Path) -> str:
    with tempfile.NamedTemporaryFile(prefix="firstcoder-index-") as index:
        env = {"GIT_INDEX_FILE": index.name}
        real_index = Path(_git(["rev-parse", "--git-path", "index"], repo).stdout.strip())
        if not real_index.is_absolute():
            real_index = repo / real_index
        shutil.copyfile(real_index, index.name)
        untracked = _git(["ls-files", "--others", "--exclude-standard", "-z"], repo).stdout
        if untracked:
            paths = [path for path in untracked.split("\0") if path]
            _git(["add", "--", *paths], repo, env=env)
        staged_and_untracked = _git(["diff", "--cached", "--binary"], repo, env=env).stdout
        unstaged = _git(["diff", "--binary"], repo).stdout
        return staged_and_untracked + unstaged


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
