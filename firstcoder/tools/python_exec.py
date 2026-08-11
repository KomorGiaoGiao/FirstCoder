"""`python_exec` 工具。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.command_result import command_tool_result
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.execution_sandbox import ExecutionSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_python_exec_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建 Python 代码执行工具。"""

    sandbox = ExecutionSandbox(root, access=access)

    def python_exec(
        code: str,
        cwd: str = ".",
        timeout_seconds: int = 120,
        max_output_chars: int = 20000,
        env: dict[str, str] | None = None,
    ) -> ToolResult:
        """在项目内执行 Python 代码；高风险，需显式启用。"""

        if timeout_seconds <= 0:
            return make_error_result("python_exec", "timeout_seconds 必须大于 0")
        if max_output_chars <= 0:
            return make_error_result("python_exec", "max_output_chars 必须大于 0")

        try:
            workdir = sandbox.resolve_cwd(cwd)
            accepted_env, rejected_env = sandbox.prepare_env_overrides(env)
        except ValueError as exc:
            return make_error_result("python_exec", str(exc))
        if rejected_env:
            return make_error_result(
                "python_exec",
                "拒绝传入敏感环境变量：" + ", ".join(rejected_env),
                rejected_env_keys=list(rejected_env),
            )

        result = sandbox.run(
            [sys.executable, "-c", code],
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            extra_env=accepted_env,
        )

        data = {
            "cwd": sandbox.relative(workdir) or ".",
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "env_keys": sorted(accepted_env),
        }

        return command_tool_result(
            "python_exec",
            result,
            data=data,
            nonzero_error=f"Python 退出码为 {result.exit_code}",
            success_fallback=f"Python 退出码：{result.exit_code}",
        )

    tool = tool_from_function(python_exec)
    tool.definition.parameters["properties"]["env"] = {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Optional non-sensitive environment overrides for this Python process.",
    }
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.EXECUTE_SHELL,
        target_builder=_permission_target_for_python_exec,
        cwd_arg="cwd",
        reason="执行 Python 代码需要用户确认。",
        allow_always=False,
        allow_auto=False,
    )
    return tool


def _permission_target_for_python_exec(arguments: dict[str, object]) -> str:
    code = str(arguments.get("code") or "")
    preview = code if len(code) <= 200 else code[:200] + "..."
    return f"python -c {preview}"
