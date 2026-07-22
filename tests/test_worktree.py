"""Phase 4 git worktree 隔离的聚焦测试。

覆盖点（对应 docs/async-subagents-dag-plan.md Phase 4）：
- worktree 路径被约束在仓库 git 目录下的 fc-worktrees/ 内。
- 创建 worktree 不会改动父工作区（父仓库保持 dirty 也能创建）。
- diff 摘要能反映未跟踪/已修改文件。
- remove 默认拒绝丢弃未提交改动，force=True 才允许。
- 非 git 仓库不可用。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from firstcoder.agent.worktree import (
    WORKTREE_DIRNAME,
    Worktree,
    WorktreeError,
    WorktreeManager,
    is_git_repo,
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@t.co")
    _git(root, "config", "user.name", "t")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")


def test_is_git_repo_detects_repo(tmp_path) -> None:
    assert is_git_repo(tmp_path) is False
    _init_repo(tmp_path)
    assert is_git_repo(tmp_path) is True


def test_create_worktree_is_constrained_and_leaves_parent_untouched(tmp_path) -> None:
    _init_repo(tmp_path)
    # 让父工作区变脏，证明隔离创建不受影响、也不会清理它。
    (tmp_path / "seed.txt").write_text("seed dirty\n", encoding="utf-8")

    manager = WorktreeManager(tmp_path)
    assert manager.available() is True
    worktree = manager.create("sess_abc")

    # 路径必须落在 <git-common-dir>/fc-worktrees/ 之下。
    assert WORKTREE_DIRNAME in worktree.path.parts
    assert worktree.path.exists()
    assert worktree.branch == "fc/subagent/sess_abc"

    # 父工作区仍然是我们留下的 dirty 状态，没有被隔离流程改动。
    assert (tmp_path / "seed.txt").read_text(encoding="utf-8").strip() == "seed dirty"
    parent_status = subprocess.run(["git", "status", "--porcelain"], cwd=tmp_path, capture_output=True, text=True).stdout
    assert "seed.txt" in parent_status


def test_diff_summary_includes_new_and_modified_files(tmp_path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    worktree = manager.create("sess_diff")

    (worktree.path / "seed.txt").write_text("seed changed\n", encoding="utf-8")
    (worktree.path / "brand_new.py").write_text("print('x')\n", encoding="utf-8")

    diff = manager.diff(worktree)
    assert diff.has_changes is True
    assert "seed.txt" in diff.files_changed
    assert "brand_new.py" in diff.files_changed
    assert "brand_new.py" in diff.render()


def test_child_edits_do_not_touch_parent_working_tree(tmp_path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    worktree = manager.create("sess_isolated")

    (worktree.path / "brand_new.py").write_text("print('x')\n", encoding="utf-8")

    # 隔离目录里有新文件，但父工作区完全看不到它。
    assert (worktree.path / "brand_new.py").exists()
    assert not (tmp_path / "brand_new.py").exists()


def test_remove_refuses_dirty_worktree_without_force(tmp_path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    worktree = manager.create("sess_dirty")
    (worktree.path / "uncommitted.py").write_text("y=2\n", encoding="utf-8")

    assert manager.is_dirty(worktree) is True
    with pytest.raises(WorktreeError):
        manager.remove(worktree)
    # 拒绝之后目录仍在，未提交改动没有被悄悄丢弃。
    assert worktree.path.exists()

    manager.remove(worktree, force=True)
    assert not worktree.path.exists()


def test_remove_clean_worktree_succeeds(tmp_path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    worktree = manager.create("sess_clean")

    assert manager.is_dirty(worktree) is False
    manager.remove(worktree)
    assert not worktree.path.exists()


def test_create_requires_git_repo(tmp_path) -> None:
    manager = WorktreeManager(tmp_path)
    assert manager.available() is False
    with pytest.raises(WorktreeError):
        manager.create("sess_nogit")


def test_available_requires_resolvable_base_ref(tmp_path) -> None:
    _git(tmp_path, "init", "-q")
    manager = WorktreeManager(tmp_path)

    assert is_git_repo(tmp_path) is True
    assert manager.available() is False
    with pytest.raises(WorktreeError, match="基准提交"):
        manager.create("sess_empty")


def test_create_rejects_duplicate_path(tmp_path) -> None:
    _init_repo(tmp_path)
    manager = WorktreeManager(tmp_path)
    manager.create("sess_dup")
    with pytest.raises(WorktreeError):
        manager.create("sess_dup")
