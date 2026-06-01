"""`shell` 工具。"""

from __future__ import annotations

import subprocess
from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.subprocess import run_command


DEFAULT_TIMEOUT_SECONDS = 30
DEFAULT_MAX_OUTPUT_CHARS = 20000


def create_shell_tool(root: str | Path) -> Tool:
    """创建命令执行工具。

    这是高风险工具：调用方必须在用户明确开启执行权限后才能注册它。
    """

    sandbox = PathSandbox(root)

    def shell(
        command: str,
        cwd: str = ".",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
    ) -> ToolResult:
        """在项目目录内执行 shell 命令并返回输出。高风险，必须由用户显式启用。"""

        if timeout_seconds <= 0:
            return make_error_result("shell", "timeout_seconds 必须大于 0")
        if max_output_chars <= 0:
            return make_error_result("shell", "max_output_chars 必须大于 0")

        try:
            workdir = sandbox.resolve_validated(cwd, expect="dir")
        except ValueError as exc:
            return make_error_result("shell", str(exc))

        result = run_command(
            command,
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            shell=True,
        )

        data = {
            "command": command,
            "cwd": sandbox.relative(workdir) or ".",
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        }

        if result.error:
            return make_error_result("shell", result.error, **data)
        if not result.ok:
            return make_error_result("shell", f"命令退出码为 {result.exit_code}", **data)

        content = result.stdout.strip() or result.stderr.strip() or f"命令退出码：{result.exit_code}"
        return make_text_result("shell", content, **data)

    return tool_from_function(shell)
