"""Phase 4 git worktree isolation for mutation-capable background subagents.

The manager owns the git plumbing needed to run a mutation-capable subagent
(role ``coder``) without ever touching the parent working tree.  Each isolated
job gets its own git worktree plus a dedicated branch under the repository's
common git dir (``<git-common-dir>/fc-worktrees/<name>``).  Storing worktrees
under the git dir keeps them out of the parent's ``git status`` and out of the
sandbox path space that ordinary tools can see.

Safety rules encoded here (see docs/async-subagents-dag-plan.md, Phase 4):

- A worktree can only be created for a real git repository.
- The parent working tree is never modified by creation or diffing.
- Removal refuses to discard uncommitted work unless ``force=True``.
- Nothing here auto-merges or applies the isolated diff back to the parent.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from firstcoder.utils.execution_sandbox import ExecutionSandbox

WORKTREE_DIRNAME = "fc-worktrees"
_DIFF_STAT_LIMIT = 8000


class WorktreeError(RuntimeError):
    """Raised when a git worktree operation cannot be completed safely."""


@dataclass(slots=True)
class Worktree:
    """A single isolated worktree owned by the manager."""

    name: str
    path: Path
    branch: str
    base_ref: str


@dataclass(slots=True)
class WorktreeDiff:
    """Summary of the uncommitted changes inside an isolated worktree."""

    stat: str
    files_changed: list[str]
    has_changes: bool

    def render(self) -> str:
        if not self.has_changes:
            return "(worktree has no changes)"
        return self.stat


def _run_git(cwd: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a git command with a sanitized environment.

    Reuses ``ExecutionSandbox.build_env`` so secret-bearing environment
    variables never leak into subprocesses, matching the rest of the codebase.
    """

    env = ExecutionSandbox(cwd).build_env()
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(["git", *args], returncode=1, stdout="", stderr=str(exc))


def is_git_repo(path: str | Path) -> bool:
    """Return True when ``path`` lives inside a real git work tree."""

    root = Path(path)
    if not root.exists():
        return False
    result = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    return result.returncode == 0 and result.stdout.strip() == "true"


class WorktreeManager:
    """Create and tear down isolated git worktrees for background mutation jobs."""

    def __init__(self, project_root: str | Path) -> None:
        self.project_root = Path(project_root).resolve()

    # -- discovery --------------------------------------------------------

    def available(self, *, base_ref: str = "HEAD") -> bool:
        """Whether isolated worktrees can be created for this project root.

        A freshly initialized repository with no commits is technically a git
        work tree, but ``git worktree add ... HEAD`` cannot succeed until the
        requested base ref resolves to a commit.  Treat that as unavailable so
        background coder dispatch can reject up front instead of starting a job
        that will only fail later.
        """

        if not is_git_repo(self.project_root):
            return False
        result = _run_git(self.project_root, ["rev-parse", "--verify", f"{base_ref}^{{commit}}"])
        return result.returncode == 0

    def _common_git_dir(self) -> Path:
        result = _run_git(self.project_root, ["rev-parse", "--git-common-dir"])
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "无法定位 git 目录；当前不是 git 仓库。")
        raw = result.stdout.strip() or ".git"
        common = Path(raw)
        if not common.is_absolute():
            common = (self.project_root / common).resolve()
        return common

    def worktrees_root(self) -> Path:
        return self._common_git_dir() / WORKTREE_DIRNAME

    # -- lifecycle --------------------------------------------------------

    def create(self, name: str, *, base_ref: str = "HEAD") -> Worktree:
        """Create a fresh worktree + branch for the given job/session name.

        Raises ``WorktreeError`` when the project is not a git repo, when the
        target path already exists, or when git refuses the operation.  Creation
        never mutates the parent working tree, so a dirty parent is fine.
        """

        safe_name = _sanitize_name(name)
        if not safe_name:
            raise WorktreeError("worktree 名称不能为空。")
        if not is_git_repo(self.project_root):
            raise WorktreeError("当前项目不是 git 仓库，无法创建隔离 worktree。")
        if not self.available(base_ref=base_ref):
            raise WorktreeError(f"无法解析 worktree 基准提交：{base_ref}")

        target = self.worktrees_root() / safe_name
        if target.exists():
            raise WorktreeError(f"worktree 路径已存在：{target}")
        target.parent.mkdir(parents=True, exist_ok=True)

        branch = f"fc/subagent/{safe_name}"
        result = _run_git(
            self.project_root,
            ["worktree", "add", "-q", "-b", branch, str(target), base_ref],
        )
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git worktree add 失败。")
        return Worktree(name=safe_name, path=target.resolve(), branch=branch, base_ref=base_ref)

    def diff(self, worktree: Worktree) -> WorktreeDiff:
        """Summarize uncommitted changes (including untracked files).

        Uses ``git add -A -N`` so newly created files appear in the stat output,
        then reads ``--stat`` and ``--name-status`` against ``HEAD``.  This only
        touches the isolated worktree's index, never the parent.
        """

        add_result = _run_git(worktree.path, ["add", "-A", "-N"])
        if add_result.returncode != 0:
            raise WorktreeError(add_result.stderr.strip() or "git add -N 失败。")
        stat_result = _run_git(worktree.path, ["diff", "--stat", "HEAD"])
        names_result = _run_git(worktree.path, ["diff", "--name-status", "HEAD"])
        if stat_result.returncode != 0:
            raise WorktreeError(stat_result.stderr.strip() or "git diff --stat 失败。")
        if names_result.returncode != 0:
            raise WorktreeError(names_result.stderr.strip() or "git diff --name-status 失败。")
        stat = stat_result.stdout.strip()
        files = _parse_name_status(names_result.stdout)
        has_changes = bool(files) or bool(stat)
        if len(stat) > _DIFF_STAT_LIMIT:
            stat = stat[:_DIFF_STAT_LIMIT] + "\n…(diff stat truncated)"
        return WorktreeDiff(stat=stat, files_changed=files, has_changes=has_changes)

    def is_dirty(self, worktree: Worktree) -> bool:
        result = _run_git(worktree.path, ["status", "--porcelain"])
        return bool(result.stdout.strip())

    def remove(self, worktree: Worktree, *, force: bool = False) -> None:
        """Remove the worktree.

        Refuses to discard uncommitted work unless ``force=True``; this mirrors
        git's own default and prevents silent loss of an isolated coder's output.
        """

        if not force and self.is_dirty(worktree):
            raise WorktreeError("worktree 有未提交改动；请先审查/保存，或用 force=True 显式丢弃。")
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(worktree.path))
        result = _run_git(self.project_root, args)
        if result.returncode != 0:
            raise WorktreeError(result.stderr.strip() or "git worktree remove 失败。")
        _run_git(self.project_root, ["worktree", "prune"])


def _sanitize_name(name: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "._-" else "-" for ch in str(name).strip())
    return cleaned.strip("-")


def _parse_name_status(output: str) -> list[str]:
    files: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        # For renames/copies (R100 old new) the destination path is last.
        files.append(parts[-1])
    return files
