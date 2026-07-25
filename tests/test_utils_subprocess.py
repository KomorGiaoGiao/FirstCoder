"""utils/subprocess 模块测试：run_command。"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

import pytest

from firstcoder.runtime.cancellation import CancellationToken
from firstcoder.utils.subprocess import CommandResult, run_command


class TestRunCommand:
    def test_successful_command(self, tmp_path):
        result = run_command([sys.executable, "-c", "print('hello')"], cwd=tmp_path)

        assert result.ok is True
        assert result.exit_code == 0
        assert result.stdout == "hello\n"
        assert result.stderr == ""

    def test_failed_command(self, tmp_path):
        result = run_command([sys.executable, "-c", "import sys; print('error', file=sys.stderr); sys.exit(1)"], cwd=tmp_path)

        assert result.ok is False
        assert result.exit_code == 1
        assert result.stderr == "error\n"

    def test_timeout_expired(self, tmp_path):
        result = run_command([sys.executable, "-c", "import time; time.sleep(999)"], cwd=tmp_path, timeout_seconds=0.05)

        assert result.ok is False
        assert result.error == "命令执行超时"

    @pytest.mark.skipif(os.name == "nt", reason="进程组断言使用 POSIX 进程组语义")
    def test_timeout_kills_process_group_and_collects_partial_output(self, tmp_path):
        marker = tmp_path / "grandchild-survived"
        child_code = (
            "import pathlib, time; time.sleep(0.5); "
            f"pathlib.Path({str(marker)!r}).write_text('survived')"
        )
        command = (
            f"printf 'before-timeout\\n'; "
            f"{sys.executable} -c \"{child_code}\" & "
            "wait"
        )

        result = run_command(command, cwd=tmp_path, timeout_seconds=0.1, shell=True)

        assert result.ok is False
        assert result.error == "命令执行超时"
        assert "before-timeout" in result.stdout
        time.sleep(0.7)
        assert not marker.exists()

    def test_os_error(self, tmp_path):
        result = run_command(["missing_cmd"], cwd=tmp_path)

        assert result.ok is False
        assert "No such file or directory" in result.error

    def test_stdout_truncation(self, tmp_path):
        result = run_command([sys.executable, "-c", "print('abcdefghij', end='')"], cwd=tmp_path, max_output_chars=5)

        assert result.ok is True
        assert result.stdout == "abcde\n\n[输出已截断]"
        assert result.stdout_truncated is True

    def test_stderr_truncation(self, tmp_path):
        result = run_command(
            [sys.executable, "-c", "import sys; print('abcdefghij', file=sys.stderr, end=''); sys.exit(1)"],
            cwd=tmp_path,
            max_output_chars=5,
        )

        assert result.ok is False
        assert result.stderr == "abcde\n\n[输出已截断]"
        assert result.stderr_truncated is True

    def test_result_is_command_result_type(self, tmp_path):
        result = run_command(["echo"], cwd=tmp_path)

        assert isinstance(result, CommandResult)

    def test_custom_timeout(self, monkeypatch, tmp_path):
        called = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, timeout=None):
                called["timeout"] = timeout
                return "ok", ""

            def poll(self):
                return self.returncode

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: FakeProcess())
        run_command(["echo"], cwd=tmp_path, timeout_seconds=60)

        assert called["timeout"] == pytest.approx(60, abs=0.01)

    def test_shell_mode(self, monkeypatch, tmp_path):
        called = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, timeout=None):
                return "ok", ""

            def poll(self):
                return self.returncode

        def _capture_popen(*a, **kw):
            called["shell"] = kw.get("shell", False)
            return FakeProcess()

        monkeypatch.setattr(subprocess, "Popen", _capture_popen)
        run_command(["echo hi"], cwd=tmp_path, shell=True)

        assert called["shell"] is True

    def test_passes_custom_environment(self, monkeypatch, tmp_path):
        called = {}

        class FakeProcess:
            returncode = 0

            def communicate(self, timeout=None):
                return "ok", ""

            def poll(self):
                return self.returncode

        def _capture_popen(*a, **kw):
            called["env"] = kw.get("env")
            return FakeProcess()

        monkeypatch.setattr(subprocess, "Popen", _capture_popen)
        run_command(["echo"], cwd=tmp_path, env={"PATH": "/bin", "CUSTOM": "1"})

        assert called["env"] == {"PATH": "/bin", "CUSTOM": "1"}

    def test_cancellation_terminates_running_process(self, tmp_path):
        token = CancellationToken()
        thread = threading.Thread(
            target=lambda: (time.sleep(0.2), token.cancel()),
            daemon=True,
        )
        thread.start()

        started_at = time.perf_counter()
        result = run_command(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            timeout_seconds=10,
            cancellation_token=token,
        )
        elapsed = time.perf_counter() - started_at

        assert result.ok is False
        assert result.error == "命令已中断"
        assert elapsed < 2
