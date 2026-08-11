"""`diagnostics` 工具。"""

from __future__ import annotations

import sys
from pathlib import Path

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.command_result import command_tool_result
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.execution_sandbox import ExecutionSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_diagnostics_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建项目诊断工具。"""

    sandbox = ExecutionSandbox(root, access=access)

    def diagnostics(
        command: str = "python -m pytest -q",
        timeout_seconds: int = 300,
        max_output_chars: int = 20000,
        env: dict[str, str] | None = None,
    ) -> ToolResult:
        """运行项目诊断命令，适合测试、lint、类型检查。"""

        if timeout_seconds <= 0:
            return make_error_result("diagnostics", "timeout_seconds 必须大于 0")
        if max_output_chars <= 0:
            return make_error_result("diagnostics", "max_output_chars 必须大于 0")

        try:
            accepted_env, rejected_env = sandbox.prepare_env_overrides(env)
        except ValueError as exc:
            return make_error_result("diagnostics", str(exc))
        if rejected_env:
            return make_error_result(
                "diagnostics",
                "拒绝传入敏感环境变量：" + ", ".join(rejected_env),
                rejected_env_keys=list(rejected_env),
            )
        normalized_command = command.replace("python", sys.executable, 1) if command.startswith("python ") else command
        result = sandbox.run(
            normalized_command,
            cwd=".",
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            shell=True,
            extra_env=accepted_env,
        )

        data = {
            "command": command,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "truncated": result.stdout_truncated or result.stderr_truncated,
            "env_keys": sorted(accepted_env),
        }

        return command_tool_result(
            "diagnostics",
            result,
            data=data,
            nonzero_error=f"诊断命令退出码为 {result.exit_code}",
            success_fallback="诊断通过。",
        )

    tool = tool_from_function(diagnostics)
    tool.definition.parameters["properties"]["env"] = {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Optional non-sensitive environment overrides for validation.",
    }
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.EXECUTE_SHELL,
        target_arg="command",
        reason="运行诊断命令需要用户确认。",
    )
    return tool
