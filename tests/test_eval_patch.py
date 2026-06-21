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
