"""执行类工具行为测试。"""

from __future__ import annotations

import subprocess
import sys

from firstcoder.agent.processes import ProcessManager
from firstcoder.agent.session import create_project_permission_manager
from firstcoder.permissions.types import PermissionMode
from firstcoder.tools.diagnostics import create_diagnostics_tool
from firstcoder.tools.python_exec import create_python_exec_tool
from firstcoder.tools.shell import create_shell_tool
from firstcoder.tools import create_builtin_registry
from firstcoder.tools.permission_registry import PermissionAwareToolRegistry
from firstcoder.utils.subprocess import CommandResult


def test_shell_executes_command_inside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "echo hello"})

    assert result.ok is True
    assert result.content == "hello"
    assert result.data["exit_code"] == 0
    assert result.data["cwd"] == "."


def test_shell_returns_error_for_nonzero_exit(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "exit 2"})

    assert result.ok is False
    assert result.error == "命令退出码为 2"
    assert result.data["stderr"] == ""


def test_shell_nonzero_exit_returns_stdout_and_stderr_to_model(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return CommandResult(
            exit_code=2,
            stdout="partial stdout\n",
            stderr="compiler error\n",
            stdout_truncated=False,
            stderr_truncated=False,
            ok=False,
        )

    monkeypatch.setattr("firstcoder.utils.execution_sandbox.run_command", fake_run)
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "compile"})

    assert result.ok is False
    assert result.error == "命令退出码为 2"
    assert "partial stdout" in result.content
    assert "compiler error" in result.content


def test_shell_rejects_cwd_outside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "echo hi", "cwd": ".."})

    assert result.ok is False
    assert "超出项目目录" in result.error


def test_shell_handles_timeout(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)
    command = subprocess.list2cmdline([sys.executable, "-c", "import time; time.sleep(999)"])

    result = registry.execute("shell", {"command": command, "timeout_seconds": 1})

    assert result.ok is False
    assert result.error == "命令执行超时"


def test_shell_timeout_returns_partial_output_to_model(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return CommandResult(
            exit_code=-1,
            stdout="partial stdout\n",
            stderr="partial stderr\n",
            stdout_truncated=False,
            stderr_truncated=False,
            ok=False,
            error="命令执行超时",
        )

    monkeypatch.setattr("firstcoder.utils.execution_sandbox.run_command", fake_run)
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("shell", {"command": "slow"})

    assert result.ok is False
    assert result.error == "命令执行超时"
    assert "命令执行超时" in result.content
    assert "partial stdout" in result.content
    assert "partial stderr" in result.content


def test_shell_rejects_non_positive_limits(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    timeout_result = registry.execute("shell", {"command": "x", "timeout_seconds": 0})
    output_result = registry.execute("shell", {"command": "x", "max_output_chars": 0})

    assert timeout_result.ok is False
    assert timeout_result.error == "timeout_seconds 必须大于 0"
    assert output_result.ok is False
    assert output_result.error == "max_output_chars 必须大于 0"


def test_shell_truncates_large_stdout(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)
    command = subprocess.list2cmdline([sys.executable, "-c", "print('abcdef', end='')"])

    result = registry.execute("shell", {"command": command, "max_output_chars": 3})

    assert result.ok is True
    assert result.data["stdout"].startswith("ab")
    assert result.data["stdout"].endswith("f")
    assert "中间省略 3 个字符" in result.data["stdout"]
    assert result.data["stdout_truncated"] is True


def test_python_exec_executes_code_inside_root(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("python_exec", {"code": "print(42)"})

    assert result.ok is True
    assert result.content == "42"
    assert result.data["exit_code"] == 0


def test_python_exec_nonzero_exit_returns_stderr_to_model(tmp_path, monkeypatch):
    def fake_run(*args, **kwargs):
        return CommandResult(
            exit_code=1,
            stdout="",
            stderr="Traceback: boom\n",
            stdout_truncated=False,
            stderr_truncated=False,
            ok=False,
        )

    monkeypatch.setattr("firstcoder.utils.execution_sandbox.run_command", fake_run)
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    result = registry.execute("python_exec", {"code": "raise RuntimeError('boom')"})

    assert result.ok is False
    assert result.error == "Python 退出码为 1"
    assert "Traceback: boom" in result.content


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
            "code": ("import os; " "print(os.environ.get('OPENAI_API_KEY', '<missing>')); " "print(os.environ.get('FIRSTCODER_VISIBLE_TEST_FLAG', '<missing>'))"),
        },
    )

    assert result.ok is True
    assert result.data["stdout"] == "<missing>\nvisible\n"


def test_execution_tools_accept_explicit_non_sensitive_environment(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    shell_command = subprocess.list2cmdline(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['FIRSTCODER_TEST_FLAG'])",
        ]
    )
    shell_result = registry.execute(
        "shell",
        {
            "command": shell_command,
            "env": {"FIRSTCODER_TEST_FLAG": "shell-visible"},
        },
    )
    python_result = registry.execute(
        "python_exec",
        {
            "code": "import os; print(os.environ['FIRSTCODER_TEST_FLAG'])",
            "env": {"FIRSTCODER_TEST_FLAG": "python-visible"},
        },
    )

    assert shell_result.ok is True
    assert shell_result.data["stdout"].strip() == "shell-visible"
    assert shell_result.data["env_keys"] == ["FIRSTCODER_TEST_FLAG"]
    assert python_result.ok is True
    assert python_result.data["stdout"].strip() == "python-visible"
    assert python_result.data["env_keys"] == ["FIRSTCODER_TEST_FLAG"]


def test_execution_tools_reject_explicit_sensitive_environment(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    for tool_name, arguments in (
        ("shell", {"command": "echo no", "env": {"OPENAI_API_KEY": "secret"}}),
        ("python_exec", {"code": "print('no')", "env": {"ACCESS_TOKEN": "secret"}}),
        ("diagnostics", {"env": {"DATABASE_PASSWORD": "secret"}}),
    ):
        result = registry.execute(tool_name, arguments)

        assert result.ok is False
        assert "拒绝传入敏感环境变量" in result.error
        assert result.data["rejected_env_keys"]


def test_process_tools_start_wait_for_readiness_read_logs_and_stop(tmp_path):
    manager = ProcessManager(log_root=tmp_path / ".firstcoder" / "processes")
    registry = create_builtin_registry(
        tmp_path,
        include_execution_tools=True,
        process_manager=manager,
    )
    script = (
        "import sys,time; "
        "print('booting', flush=True); "
        "print('READY', file=sys.stderr, flush=True); "
        "time.sleep(60)"
    )
    command = subprocess.list2cmdline([sys.executable, "-c", script])

    try:
        started = registry.execute(
            "process_start",
            {
                "command": command,
                "label": "demo service",
                "ready_pattern": "READY",
                "ready_timeout_seconds": 5,
                "env": {"FIRSTCODER_TEST_FLAG": "visible"},
            },
        )

        assert started.ok is True
        process = started.data["process"]
        process_id = process["process_id"]
        assert process["status"] == "running"
        assert process["ready"] is True
        assert started.data["env_keys"] == ["FIRSTCODER_TEST_FLAG"]

        status = registry.execute("process_status", {"process_id": process_id})
        logs = registry.execute("process_logs", {"process_id": process_id})

        assert status.ok is True
        assert status.data["process"]["status"] == "running"
        assert "booting" in logs.content
        assert "READY" in logs.content

        stopped = registry.execute("process_stop", {"process_id": process_id})
        assert stopped.ok is True
        assert stopped.data["process"]["status"] == "exited"
    finally:
        manager.shutdown()


def test_process_start_reports_readiness_timeout_without_losing_process(tmp_path):
    manager = ProcessManager(log_root=tmp_path / ".firstcoder" / "processes")
    registry = create_builtin_registry(
        tmp_path,
        include_execution_tools=True,
        process_manager=manager,
    )
    command = subprocess.list2cmdline([sys.executable, "-c", "import time; time.sleep(60)"])

    try:
        result = registry.execute(
            "process_start",
            {
                "command": command,
                "ready_pattern": "NEVER_READY",
                "ready_timeout_seconds": 1,
            },
        )

        assert result.ok is False
        assert result.data["readiness_timed_out"] is True
        assert result.data["process"]["status"] == "running"
        assert "process_logs/process_status" in result.content
    finally:
        manager.shutdown()


def test_process_start_rejects_sensitive_environment(tmp_path):
    manager = ProcessManager(log_root=tmp_path / ".firstcoder" / "processes")
    registry = create_builtin_registry(
        tmp_path,
        include_execution_tools=True,
        process_manager=manager,
    )

    result = registry.execute(
        "process_start",
        {"command": "echo no", "env": {"FIRSTCODER_API_TOKEN": "secret"}},
    )

    assert result.ok is False
    assert result.data["rejected_env_keys"] == ["FIRSTCODER_API_TOKEN"]
    assert manager.list() == []


def test_diagnostics_runs_pytest(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return CommandResult(
            exit_code=0,
            stdout="ok\n",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            ok=True,
        )

    monkeypatch.setattr("firstcoder.utils.execution_sandbox.run_command", fake_run)
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("diagnostics")

    assert result.ok is True
    assert result.content == "ok"
    assert result.data["command"] == "python -m pytest -q"


def test_diagnostics_failure_returns_test_output_to_model(monkeypatch, tmp_path):
    def fake_run(command, **kwargs):
        return CommandResult(
            exit_code=1,
            stdout="FAILED tests/test_demo.py::test_value\n",
            stderr="AssertionError: expected 2\n",
            stdout_truncated=False,
            stderr_truncated=False,
            ok=False,
        )

    monkeypatch.setattr("firstcoder.utils.execution_sandbox.run_command", fake_run)
    registry = create_builtin_registry(tmp_path)

    result = registry.execute("diagnostics")

    assert result.ok is False
    assert result.error == "诊断命令退出码为 1"
    assert "FAILED tests/test_demo.py::test_value" in result.content
    assert "AssertionError: expected 2" in result.content


def test_diagnostics_requires_permission_confirmation(tmp_path):
    calls = []
    registry = create_builtin_registry(tmp_path)
    permissioned = PermissionAwareToolRegistry(
        registry,
        create_project_permission_manager(tmp_path, mode=PermissionMode.STANDARD),
    )

    result = permissioned.execute("diagnostics", {"command": "touch should_not_run"})

    assert result.ok is True
    assert result.data["requires_user_input"] is True
    assert result.data["permission_request"]["action"] == "execute_shell"
    assert calls == []


def test_python_exec_requires_permission_even_in_aggressive_mode(tmp_path):
    calls = []
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)
    permissioned = PermissionAwareToolRegistry(
        registry,
        create_project_permission_manager(tmp_path, mode=PermissionMode.AGGRESSIVE),
    )

    result = permissioned.execute("python_exec", {"code": "__import__('os').system('id')"})

    assert result.ok is True
    assert result.data["requires_user_input"] is True
    assert result.data["permission_request"]["action"] == "execute_shell"
    assert calls == []
