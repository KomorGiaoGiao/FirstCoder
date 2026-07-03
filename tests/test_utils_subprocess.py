"""utils/subprocess 模块测试：run_command。"""

from __future__ import annotations

import subprocess

import pytest

from firstcoder.utils.subprocess import CommandResult, run_command


def _fake_completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(["test"], returncode, stdout, stderr)


class TestRunCommand:
    def test_successful_command(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: _fake_completed(stdout="hello\n"),
        )
        result = run_command(["echo", "hello"], cwd=tmp_path)

        assert result.ok is True
        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        assert result.stderr == ""

    def test_failed_command(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: _fake_completed(returncode=1, stderr="error\n"),
        )
        result = run_command(["false"], cwd=tmp_path)

        assert result.ok is False
        assert result.exit_code == 1
        assert result.stderr == "error\n"

    def test_timeout_expired(self, monkeypatch, tmp_path):
        def _raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(["test"], timeout=30)

        monkeypatch.setattr(subprocess, "run", _raise_timeout)
        result = run_command(["sleep", "999"], cwd=tmp_path, timeout_seconds=30)

        assert result.ok is False
        assert result.error == "命令执行超时"

    def test_os_error(self, monkeypatch, tmp_path):
        def _raise_os_error(*a, **kw):
            raise OSError("not found")

        monkeypatch.setattr(subprocess, "run", _raise_os_error)
        result = run_command(["missing_cmd"], cwd=tmp_path)

        assert result.ok is False
        assert "not found" in result.error

    def test_stdout_truncation(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: _fake_completed(stdout="abcdefghij"),
        )
        result = run_command(["echo"], cwd=tmp_path, max_output_chars=5)

        assert result.ok is True
        assert result.stdout == "abcde\n\n[输出已截断]"
        assert result.stdout_truncated is True

    def test_stderr_truncation(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: _fake_completed(returncode=1, stderr="abcdefghij"),
        )
        result = run_command(["fail"], cwd=tmp_path, max_output_chars=5)

        assert result.ok is False
        assert result.stderr == "abcde\n\n[输出已截断]"
        assert result.stderr_truncated is True

    def test_result_is_command_result_type(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **kw: _fake_completed(stdout="ok"),
        )
        result = run_command(["echo"], cwd=tmp_path)

        assert isinstance(result, CommandResult)

    def test_custom_timeout(self, monkeypatch, tmp_path):
        called = {}

        def _capture_run(*a, **kw):
            called["timeout"] = kw.get("timeout")
            return _fake_completed(stdout="ok")

        monkeypatch.setattr(subprocess, "run", _capture_run)
        run_command(["echo"], cwd=tmp_path, timeout_seconds=60)

        assert called["timeout"] == 60

    def test_shell_mode(self, monkeypatch, tmp_path):
        called = {}

        def _capture_run(*a, **kw):
            called["shell"] = kw.get("shell", False)
            return _fake_completed(stdout="ok")

        monkeypatch.setattr(subprocess, "run", _capture_run)
        run_command(["echo hi"], cwd=tmp_path, shell=True)

        assert called["shell"] is True

    def test_passes_custom_environment(self, monkeypatch, tmp_path):
        called = {}

        def _capture_run(*a, **kw):
            called["env"] = kw.get("env")
            return _fake_completed(stdout="ok")

        monkeypatch.setattr(subprocess, "run", _capture_run)
        run_command(["echo"], cwd=tmp_path, env={"PATH": "/bin", "CUSTOM": "1"})

        assert called["env"] == {"PATH": "/bin", "CUSTOM": "1"}
