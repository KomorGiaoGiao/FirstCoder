"""`shell` 工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.command_result import command_tool_result
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.execution_sandbox import ExecutionSandbox
from firstcoder.utils.sandbox_access import SandboxAccess

DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_MAX_OUTPUT_CHARS = 20000


def create_shell_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建命令执行工具。

    这是高风险工具：调用方必须在用户明确开启执行权限后才能注册它。
    """

    sandbox = ExecutionSandbox(root, access=access)

    def shell(
        command: str,
        cwd: str = ".",
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        env: dict[str, str] | None = None,
    ) -> ToolResult:
        """在项目内执行 shell 命令；高风险，需显式启用。"""

        if timeout_seconds <= 0:
            return make_error_result("shell", "timeout_seconds 必须大于 0")
        if max_output_chars <= 0:
            return make_error_result("shell", "max_output_chars 必须大于 0")

        try:
            workdir = sandbox.resolve_cwd(cwd)
            accepted_env, rejected_env = sandbox.prepare_env_overrides(env)
        except ValueError as exc:
            return make_error_result("shell", str(exc))
        if rejected_env:
            return make_error_result(
                "shell",
                "拒绝传入敏感环境变量：" + ", ".join(rejected_env),
                rejected_env_keys=list(rejected_env),
            )

        result = sandbox.run(
            command,
            cwd=workdir,
            timeout_seconds=timeout_seconds,
            max_output_chars=max_output_chars,
            shell=True,
            extra_env=accepted_env,
        )

        data = {
            "command": command,
            "cwd": sandbox.relative(workdir) or ".",
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "stdout_truncated": result.stdout_truncated,
            "stderr_truncated": result.stderr_truncated,
            "env_keys": sorted(accepted_env),
        }

        tool_result = command_tool_result(
            "shell",
            result,
            data=data,
            nonzero_error=f"命令退出码为 {result.exit_code}",
            success_fallback=f"命令退出码：{result.exit_code}",
        )
        if result.error == "命令执行超时":
            tool_result.content += (
                "\n\n[agent guidance]\n长编译/安装可显式提高 timeout_seconds（例如 900）；"
                "长期服务请改用 process_start，并用 readiness 条件验证。"
            )
        return tool_result

    tool = tool_from_function(shell)
    tool.definition.parameters["properties"]["env"] = {
        "type": "object",
        "additionalProperties": {"type": "string"},
        "description": "Optional non-sensitive environment overrides for this command.",
    }
    tool.definition.parameters["properties"]["timeout_seconds"]["description"] = (
        "Timeout in seconds. Default 120; use up to about 900 for installs/compiles. "
        "Use process_start for a service that should remain running."
    )
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.EXECUTE_SHELL,
        target_arg="command",
        cwd_arg="cwd",
        reason="执行 shell 命令需要用户确认。",
    )
    return tool
