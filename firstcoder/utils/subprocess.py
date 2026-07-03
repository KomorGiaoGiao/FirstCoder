"""子进程执行通用工具。

shell、python_exec、diagnostics、grep 都有近乎相同的 subprocess.run 调用
加上 TimeoutExpired / OSError 处理和输出截断，统一到这里消除重复。
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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
) -> CommandResult:
    """执行子进程命令并返回统一结果。

    自动处理 TimeoutExpired 和 OSError，自动截断超长输出。
    这是 shell / python_exec / diagnostics / grep 四个工具共同需要的执行模式。
    """

    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            shell=shell,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CommandResult(
            exit_code=-1,
            stdout="",
            stderr="",
            stdout_truncated=False,
            stderr_truncated=False,
            ok=False,
            error="命令执行超时",
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

    stdout, stdout_truncated = truncate(completed.stdout, max_output_chars)
    stderr, stderr_truncated = truncate(completed.stderr, max_output_chars)
    ok = completed.returncode == 0

    return CommandResult(
        exit_code=completed.returncode,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        ok=ok,
    )
