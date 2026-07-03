"""只读工具行为测试。"""

from __future__ import annotations

from firstcoder.utils import git as git_utils
from firstcoder.utils.subprocess import CommandResult
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.tools import grep as grep_module
from firstcoder.tools import create_builtin_registry


def _completed(args, returncode=0, stdout="", stderr=""):
    return git_utils.subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def test_tree_shows_directory_structure(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme", encoding="utf-8")
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("tree", {"max_depth": 2})

    assert result.ok is True
    assert "README.md" in result.content
    assert "src/" in result.content
    assert "  src/app.py" in result.content


def test_tree_rejects_non_positive_limits(tmp_path):
    registry = create_builtin_registry(tmp_path)

    depth_result = registry.execute("tree", {"max_depth": 0})
    entries_result = registry.execute("tree", {"max_entries": 0})

    assert depth_result.ok is False
    assert depth_result.error == "max_depth 必须大于 0"
    assert entries_result.ok is False
    assert entries_result.error == "max_entries 必须大于 0"


def test_tree_rejects_paths_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("tree", {"path": ".."})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_ls_returns_sorted_entries_and_respects_max_entries(tmp_path):
    (tmp_path / "zeta.txt").write_text("z", encoding="utf-8")
    (tmp_path / "alpha.txt").write_text("a", encoding="utf-8")
    (tmp_path / "src").mkdir()
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("ls", {"max_entries": 2})

    assert result.ok is True
    assert [entry["path"] for entry in result.data["entries"]] == ["alpha.txt", "src"]
    assert result.data["truncated"] is True


def test_ls_rejects_non_positive_max_entries(tmp_path):
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("ls", {"max_entries": 0})

    assert result.ok is False
    assert result.error == "max_entries 必须大于 0"


def test_view_reads_utf8_text_inside_root(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("第一行\n第二行\n第三行\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("view", {"path": "README.md", "offset": 1, "limit": 1})

    assert result.ok is True
    assert result.content == "2: 第二行"
    assert result.data["path"] == "README.md"
    assert result.data["start_line"] == 2
    assert result.data["end_line"] == 2


def test_view_reports_empty_range_when_offset_is_past_end(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("第一行\n第二行\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("view", {"path": "README.md", "offset": 10, "limit": 5})

    assert result.ok is True
    assert result.content == "没有可显示内容。"
    assert result.data["start_line"] is None
    assert result.data["end_line"] is None
    assert result.data["truncated"] is False


def test_tools_reject_paths_outside_root(tmp_path):
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("view", {"path": "../outside.txt"})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_grep_finds_matching_lines(tmp_path):
    source = tmp_path / "firstcoder.py"
    source.write_text("alpha\nFirstCoder agent\nbeta\n", encoding="utf-8")
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("grep", {"pattern": "firstcoder", "include": "*.py"})

    assert result.ok is True
    assert result.data["results"][0]["path"] == "firstcoder.py"
    assert result.data["results"][0]["line"] == 2


def test_grep_with_rg_filters_sensitive_environment(tmp_path, monkeypatch):
    source = tmp_path / "firstcoder.py"
    source.write_text("FirstCoder agent\n", encoding="utf-8")
    captured = {}

    def fake_run_command(command, **kwargs):
        captured["env"] = kwargs.get("env")
        return CommandResult(
            exit_code=0,
            stdout=f"{source}:1:FirstCoder agent\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            ok=True,
        )

    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(grep_module, "run_command", fake_run_command)
    monkeypatch.setenv("SEARCH_API_KEY", "secret")
    monkeypatch.setenv("FIRSTCODER_VISIBLE_TEST_FLAG", "visible")
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("grep", {"pattern": "firstcoder", "include": "*.py"})

    assert result.ok is True
    assert "SEARCH_API_KEY" not in captured["env"]
    assert captured["env"]["FIRSTCODER_VISIBLE_TEST_FLAG"] == "visible"


def test_grep_falls_back_to_python_when_rg_is_missing(tmp_path, monkeypatch):
    (tmp_path / "src").mkdir()
    source = tmp_path / "src" / "firstcoder.py"
    source.write_text("alpha\nFirstCoder agent\nbeta\n", encoding="utf-8")
    monkeypatch.setattr(grep_module.shutil, "which", lambda _name: None)
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("grep", {"pattern": "firstcoder", "include": "*.py"})

    assert result.ok is True
    assert result.data["engine"] == "python"
    assert result.data["results"][0]["path"] == "src/firstcoder.py"
    assert result.data["results"][0]["line"] == 2


def test_grep_rejects_non_positive_max_results(tmp_path):
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("grep", {"pattern": "x", "max_results": 0})

    assert result.ok is False
    assert result.error == "max_results 必须大于 0"


def test_grep_parses_rg_output_for_windows_paths(tmp_path):
    source = tmp_path / "firstcoder.py"
    source.write_text("FirstCoder agent", encoding="utf-8")
    sandbox = PathSandbox(tmp_path)
    output = f"{source}:1:FirstCoder agent\n"

    results = grep_module._parse_rg_output(sandbox, output, max_results=10)

    assert results == [{"path": "firstcoder.py", "line": 1, "text": "FirstCoder agent"}]


def test_glob_finds_matching_paths(tmp_path):
    (tmp_path / "firstcoder").mkdir()
    (tmp_path / "firstcoder" / "app.py").write_text("print('hi')", encoding="utf-8")
    (tmp_path / "README.md").write_text("# readme", encoding="utf-8")
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("glob", {"pattern": "**/*.py"})

    assert result.ok is True
    assert result.data["matches"] == ["firstcoder/app.py"]


def test_glob_respects_max_results_after_sorting(tmp_path):
    (tmp_path / "c.py").write_text("c", encoding="utf-8")
    (tmp_path / "a.py").write_text("a", encoding="utf-8")
    (tmp_path / "b.py").write_text("b", encoding="utf-8")
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("glob", {"pattern": "*.py", "max_results": 2})

    assert result.ok is True
    assert result.data["matches"] == ["a.py", "b.py"]
    assert result.data["truncated"] is True


def test_glob_rejects_non_positive_max_results(tmp_path):
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("glob", {"pattern": "*.py", "max_results": 0})

    assert result.ok is False
    assert result.error == "max_results 必须大于 0"


def test_git_status_reports_worktree_status(tmp_path, monkeypatch):
    def fake_run_git(_sandbox, args):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return _completed(args, stdout="true\n")
        if args == ["status", "--short"]:
            return _completed(args, stdout="?? README.md\n")
        return _completed(args, returncode=1, stderr="unexpected")

    monkeypatch.setattr(git_utils, "run_git", fake_run_git)
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("git_status")

    assert result.ok is True
    assert "?? README.md" in result.content
    assert result.data["clean"] is False


def test_git_status_returns_error_outside_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(
        git_utils,
        "run_git",
        lambda _sandbox, args: _completed(args, returncode=1, stderr="not a git repo"),
    )
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("git_status")

    assert result.ok is False
    assert "不是 git 仓库" in result.error


def test_git_diff_reports_changes(tmp_path, monkeypatch):
    (tmp_path / "README.md").write_text("new\n", encoding="utf-8")

    def fake_run_git(_sandbox, args):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return _completed(args, stdout="true\n")
        if args == ["diff", "--", "README.md"]:
            return _completed(args, stdout="-old\n+new\n")
        return _completed(args, returncode=1, stderr="unexpected")

    monkeypatch.setattr(git_utils, "run_git", fake_run_git)
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("git_diff", {"path": "README.md"})

    assert result.ok is True
    assert "-old" in result.content
    assert "+new" in result.content
    assert result.data["path"] == "README.md"


def test_git_diff_returns_error_outside_git_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(
        git_utils,
        "run_git",
        lambda _sandbox, args: _completed(args, returncode=1, stderr="not a git repo"),
    )
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("git_diff")

    assert result.ok is False
    assert "不是 git 仓库" in result.error


def test_git_diff_truncates_large_diff(tmp_path, monkeypatch):
    def fake_run_git(_sandbox, args):
        if args == ["rev-parse", "--is-inside-work-tree"]:
            return _completed(args, stdout="true\n")
        if args == ["diff", "--", "."]:
            return _completed(args, stdout="abcdef")
        return _completed(args, returncode=1, stderr="unexpected")

    monkeypatch.setattr(git_utils, "run_git", fake_run_git)
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("git_diff", {"max_chars": 3})

    assert result.ok is True
    assert result.content == "abc\n\n[diff 已截断]"
    assert result.data["truncated"] is True


def test_git_diff_rejects_path_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("git_diff", {"path": "../outside.txt"})

    assert result.ok is False
    assert "超出项目目录" in result.error
