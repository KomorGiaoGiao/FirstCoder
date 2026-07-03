"""`diagnostics` 工具。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.execution_sandbox import ExecutionSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_diagnostics_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建项目诊断工具。"""

    sandbox = ExecutionSandbox(root, access=access)

    def diagnostics(command: str = "python -m pytest -q", timeout_seconds: int = 120, max_output_chars: int = 20000) -> ToolResult:
        """运行项目诊断命令，适合测试、lint、类型检查。"""

        if timeout_seconds <= 0:
            return make_error_result("diagnostics", "timeout_seconds 必须大于 0")
        if max_output_chars <= 0:
            return make_error_result("diagnostics", "max_output_chars 必须大于 0")

        normalized_command = command.replace("python", sys.executable, 1) if command.startswith("python ") else command
        result = sandbox.run(
            normalized_command,
            cwd=".",
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            shell=True,
        )

        data = {
            "command": command,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.stdout_truncated or result.stderr_truncated,
        }

        if result.error:
            return make_error_result("diagnostics", result.error, **data)
        if not result.ok:
            return make_error_result("diagnostics", f"诊断命令退出码为 {result.exit_code}", **data)

        content = (result.stdout or result.stderr).strip() or "诊断通过。"
        return make_text_result("diagnostics", content, **data)

    return tool_from_function(diagnostics)
