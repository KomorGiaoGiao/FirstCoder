import subprocess
from pathlib import Path

from firstcoder.eval.patch import collect_git_diff, ensure_clean_repo


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    run(["git", "config", "user.name", "Test User"], repo)
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    run(["git", "add", "module.py"], repo)
    run(["git", "commit", "-m", "init"], repo)
    return repo


def test_ensure_clean_repo_accepts_clean_repo(tmp_path: Path):
    repo = init_repo(tmp_path)

    ensure_clean_repo(repo)


def test_ensure_clean_repo_rejects_dirty_repo(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    try:
        ensure_clean_repo(repo)
    except RuntimeError as exc:
        assert "dirty" in str(exc)
    else:
        raise AssertionError("Expected dirty repo error")


def test_collect_git_diff_includes_tracked_modifications(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")

    diff = collect_git_diff(repo)

    assert "diff --git a/module.py b/module.py" in diff
    assert "-VALUE = 1" in diff
    assert "+VALUE = 2" in diff


def test_collect_git_diff_includes_staged_modifications(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    run(["git", "add", "module.py"], repo)

    diff = collect_git_diff(repo)

    assert "diff --git a/module.py b/module.py" in diff
    assert "-VALUE = 1" in diff
    assert "+VALUE = 2" in diff


def test_collect_git_diff_can_include_untracked_files(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "new_module.py").write_text("NEW_VALUE = 3\n", encoding="utf-8")

    diff = collect_git_diff(repo, include_untracked=True)

    assert "diff --git a/new_module.py b/new_module.py" in diff
    assert "+NEW_VALUE = 3" in diff


def test_collect_git_diff_with_untracked_preserves_staged_only_changes(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    run(["git", "add", "module.py"], repo)
    (repo / "module.py").write_text("VALUE = 1\n", encoding="utf-8")

    diff = collect_git_diff(repo, include_untracked=True)

    assert "diff --git a/module.py b/module.py" in diff
    assert "-VALUE = 1" in diff
    assert "+VALUE = 2" in diff


def test_collect_git_diff_with_untracked_does_not_mutate_real_index(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "new_module.py").write_text("NEW_VALUE = 3\n", encoding="utf-8")

    collect_git_diff(repo, include_untracked=True)

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        check=True,
        text=True,
        capture_output=True,
    ).stdout
    assert status == "?? new_module.py\n"


def test_collect_git_diff_with_untracked_preserves_unstaged_tracked_changes(tmp_path: Path):
    repo = init_repo(tmp_path)
    (repo / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "new_module.py").write_text("NEW_VALUE = 3\n", encoding="utf-8")

    diff = collect_git_diff(repo, include_untracked=True)

    assert "diff --git a/module.py b/module.py" in diff
    assert "+VALUE = 2" in diff
    assert "diff --git a/new_module.py b/new_module.py" in diff
    assert "+NEW_VALUE = 3" in diff
