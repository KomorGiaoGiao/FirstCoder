"""子进程执行通用工具。

shell、python_exec、diagnostics、grep 共用同一个 Popen 进程组边界，统一处理
超时/取消、进程树回收、TimeoutExpired / OSError 和输出截断。
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from firstcoder.runtime.cancellation import CancellationToken
from firstcoder.utils.text import truncate


@dataclass(slots=True)
class CommandResult:
    """子进程执行的统一结果类型。

    工具层可以直接把 CommandResult 转成 ToolResult，
    不用每个工具重复处理 exit_code、stdout/stderr 截断等逻辑。
    """

    exit_code: int
    stdout: str
    stderr: str
    stdout_truncated: bool
    stderr_truncated: bool
    ok: bool
    error: str | None = None


def run_command(
    command: list[str] | str,
    *,
    cwd: Path,
    timeout_seconds: int = 30,
    max_output_chars: int = 20000,
    shell: bool = False,
    env: dict[str, str] | None = None,
    cancellation_token: CancellationToken | None = None,
) -> CommandResult:
    """执行子进程命令并返回统一结果。

    每个命令都在独立进程组中启动；超时或取消时终止整个进程组，并回收已经产生的输出。
    自动处理 TimeoutExpired 和 OSError，自动截断超长输出。
    这是 shell / python_exec / diagnostics / grep 四个工具共同需要的执行模式。
    """

    return _run_command_with_process_group(
        command,
        cwd=cwd,
        timeout_seconds=timeout_seconds,
        max_output_chars=max_output_chars,
        shell=shell,
        env=env,
        cancellation_token=cancellation_token,
    )


def _run_command_with_process_group(
    command: list[str] | str,
    *,
    cwd: Path,
    timeout_seconds: int,
    max_output_chars: int,
    shell: bool,
    env: dict[str, str] | None,
    cancellation_token: CancellationToken | None,
) -> CommandResult:
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=shell,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            **_process_group_kwargs(),
        )
    except OSError as exc:
        return CommandResult(
            exit_code=-1,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            ok=False,
            error=f"命令执行失败：{exc}",
        )

    deadline = time.monotonic() + timeout_seconds
    timed_out = False
    interrupted = False
    stdout = stderr = ""
    while True:
        if cancellation_token is not None and cancellation_token.is_cancelled:
            interrupted = True
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            timed_out = True
            break
        try:
            stdout, stderr = process.communicate(
                timeout=min(remaining, 0.05) if cancellation_token is not None else remaining,
            )
            break
        except subprocess.TimeoutExpired:
            if cancellation_token is None:
                timed_out = True
                break

    if interrupted or timed_out:
        _terminate_process_group(process)
        # communicate() again drains everything the process group emitted before
        # termination; this is the output that must accompany a timeout result.
        stdout, stderr = process.communicate()
    stdout, stdout_truncated = truncate(stdout, max_output_chars)
    stderr, stderr_truncated = truncate(stderr, max_output_chars)

    if interrupted:
        return CommandResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            ok=False,
            error="命令已中断",
        )
    if timed_out:
        return CommandResult(
            exit_code=process.returncode if process.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            stdout_truncated=stdout_truncated,
            stderr_truncated=stderr_truncated,
            ok=False,
            error="命令执行超时",
        )

    ok = process.returncode == 0
    return CommandResult(
        exit_code=process.returncode if process.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        ok=ok,
    )


def _process_group_kwargs() -> dict[str, int | bool]:
    """Start each command in its own process group/session."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Terminate the command and every descendant in its process group."""

    if os.name == "nt":
        _taskkill_process_tree(process.pid)
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass
    # The leader may have exited while a descendant ignored SIGTERM.  Kill the
    # group unconditionally after the grace period so those descendants cannot
    # survive merely because Popen's direct child is already reaped.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    if process.poll() is None:
        process.wait(timeout=1)


def _taskkill_process_tree(pid: int) -> None:
    """Best-effort Windows equivalent of killing a POSIX process group."""

    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
    except OSError:
        pass
