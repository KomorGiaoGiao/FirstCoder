"""执行类工具行为测试。"""

from __future__ import annotations

from firstcoder.utils import git as git_utils
from firstcoder.tools import diagnostics as diagnostics_module
from firstcoder.tools import python_exec as python_exec_module
from firstcoder.tools import shell as shell_module
from firstcoder.tools.diagnostics import create_diagnostics_tool
from firstcoder.tools.python_exec import create_python_exec_tool
from firstcoder.tools.shell import create_shell_tool
from firstcoder.tools import create_builtin_registry


def _completed(args, returncode=0, stdout="", stderr=""):
    return git_utils.subprocess.CompletedProcess(["git", *args], returncode, stdout, stderr)


def test_shell_executes_command_inside_root(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return shell_module.subprocess.CompletedProcess(command, 0, "hello\n", "")

    monkeypatch.setattr(shell_module.subprocess, "run", fake_run)
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "echo hello"})

    assert result.ok is True
    assert result.content == "hello"
    assert result.data["exit_code"] == 0
    assert result.data["cwd"] == "."


def test_shell_returns_error_for_nonzero_exit(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return shell_module.subprocess.CompletedProcess(command, 2, "", "bad command\n")

    monkeypatch.setattr(shell_module.subprocess, "run", fake_run)
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "bad"})

    assert result.ok is False
    assert result.error == "命令退出码为 2"
    assert result.data["stderr"] == "bad command\n"


def test_shell_rejects_cwd_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "echo hi", "cwd": ".."})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_shell_handles_timeout(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        raise shell_module.subprocess.TimeoutExpired(command, timeout=1)

    monkeypatch.setattr(shell_module.subprocess, "run", fake_run)
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "sleep", "timeout_seconds": 1})

    assert result.ok is False
    assert result.error == "命令执行超时"


def test_shell_rejects_non_positive_limits(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    timeout_result = registry.execute("shell", {"command": "x", "timeout_seconds": 0})
    output_result = registry.execute("shell", {"command": "x", "max_output_chars": 0})

    assert timeout_result.ok is False
    assert timeout_result.error == "timeout_seconds 必须大于 0"
    assert output_result.ok is False
    assert output_result.error == "max_output_chars 必须大于 0"


def test_shell_truncates_large_stdout(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return shell_module.subprocess.CompletedProcess(command, 0, "abcdef", "")

    monkeypatch.setattr(shell_module.subprocess, "run", fake_run)
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "echo", "max_output_chars": 3})

    assert result.ok is True
    assert result.data["stdout"] == "abc\n\n[输出已截断]"
    assert result.data["stdout_truncated"] is True


def test_python_exec_executes_code_inside_root(tmp_path, monkeypatch):
    def fake_run(command, **kwargs):
        return python_exec_module.subprocess.CompletedProcess(command, 0, "42\n", "")

    monkeypatch.setattr(python_exec_module.subprocess, "run", fake_run)
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("python_exec", {"code": "print(42)"})

    assert result.ok is True
    assert result.content == "42"
    assert result.data["exit_code"] == 0


def test_python_exec_rejects_cwd_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("python_exec", {"code": "print(1)", "cwd": ".."})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_python_exec_filters_sensitive_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("FIRSTCODER_VISIBLE_TEST_FLAG", "visible")
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute(
        "python_exec",
        {
            "code": (
                "import os; "
                "print(os.environ.get('OPENAI_API_KEY', '<missing>')); "
                "print(os.environ.get('FIRSTCODER_VISIBLE_TEST_FLAG', '<missing>'))"
            ),
        },
    )

    assert result.ok is True
    assert result.data["stdout"] == "<missing>\nvisible\n"


def test_diagnostics_runs_pytest(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return diagnostics_module.subprocess.CompletedProcess(command, 0, "ok\n", "")

    monkeypatch.setattr(diagnostics_module.subprocess, "run", fake_run)
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("diagnostics")

    assert result.ok is True
    assert result.content == "ok"
    assert result.data["command"] == "python -m pytest -q"
