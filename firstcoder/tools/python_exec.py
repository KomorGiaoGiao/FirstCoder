"""`python_exec` 工具。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.subprocess import run_command


def create_python_exec_tool(root: str | Path) -> Tool:
    """创建 Python 代码执行工具。"""

    sandbox = PathSandbox(root)

    def python_exec(code: str, cwd: str = ".", timeout_seconds: int = 30, max_output_chars: int = 20000) -> ToolResult:
        """在项目内执行 Python 代码；高风险，需显式启用。"""

        if timeout_seconds <= 0:
            return make_error_result("python_exec", "timeout_seconds 必须大于 0")
        if max_output_chars <= 0:
            return make_error_result("python_exec", "max_output_chars 必须大于 0")

        try:
            workdir = sandbox.resolve_validated(cwd, expect="dir")
        except ValueError as exc:
            return make_error_result("python_exec", str(exc))

        result = run_command(
            [sys.executable, "-c", code],
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
        )

        data = {
            "cwd": sandbox.relative(workdir) or ".",
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
        }

        if result.error:
            return make_error_result("python_exec", result.error, **data)
        if not result.ok:
            return make_error_result("python_exec", f"Python 退出码为 {result.exit_code}", **data)

        content = result.stdout.strip() or result.stderr.strip() or f"Python 退出码：{result.exit_code}"
        return make_text_result("python_exec", content, **data)

    return tool_from_function(python_exec)
