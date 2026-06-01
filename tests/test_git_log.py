"""`git_log` 工具测试。"""

from __future__ import annotations

from firstcoder.tools.git_log import create_git_log_tool
from firstcoder.utils import git as git_utils


def _completed(args, returncode=0, stdout="", stderr=""):
    return git_utils.subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def test_git_log_returns_commit_history(tmp_path, monkeypatch):
    def fake_run_git(_sandbox, args):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return _completed(args, stdout="true\n")
        if args[:2] == ["log", "--oneline"]:
            return _completed(args, stdout="abc1234 修复 bug\ndef5678 添加功能\n")
        return _completed(args, returncode=1, stderr="unexpected")

    monkeypatch.setattr(git_utils, "run_git", fake_run_git)
    tool = create_git_log_tool(tmp_path)

    result = tool.executor()

    assert result.ok is True
    assert "abc1234 修复 bug" in result.content
    assert "def5678 添加功能" in result.content
    assert result.data["commits"] == 2


def test_git_log_returns_error_outside_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(
        git_utils,
        "run_git",
        lambda _sandbox, args: _completed(args, returncode=1, stderr="not a git repo"),
    )
    tool = create_git_log_tool(tmp_path)

    result = tool.executor()

    assert result.ok is False
    assert "不是 git 仓库" in result.error


def test_git_log_rejects_non_positive_max_entries(tmp_path, monkeypatch):
    monkeypatch.setattr(
        git_utils,
        "run_git",
        lambda _sandbox, args: _completed(args, stdout="true\n") if args == ["rev-parse", "--is-inside-work-tree"] else _completed(args),
    )
    tool = create_git_log_tool(tmp_path)

    result = tool.executor(max_entries=0)

    assert result.ok is False
    assert result.error == "max_entries 必须大于 0"


def test_git_log_rejects_path_outside_root(tmp_path):
    tool = create_git_log_tool(tmp_path)

    result = tool.executor(path="../outside.txt")

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_git_log_supports_path_filter(tmp_path, monkeypatch):
    captured_args = {}

    def fake_run_git(_sandbox, args):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return _completed(args, stdout="true\n")
        captured_args["args"] = args
        return _completed(args, stdout="abc1234 修改 src/main.py\n")

    monkeypatch.setattr(git_utils, "run_git", fake_run_git)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x", encoding="utf-8")
    tool = create_git_log_tool(tmp_path)

    result = tool.executor(path="src/main.py", max_entries=5)

    assert result.ok is True
    assert "src/main.py" in captured_args["args"]
    assert result.data["max_entries"] == 5


def test_git_log_definition_has_correct_schema():
    tool = create_git_log_tool(".")

    assert tool.name == "git_log"
    assert "path" in tool.definition.parameters["properties"]
    assert "max_entries" in tool.definition.parameters["properties"]
