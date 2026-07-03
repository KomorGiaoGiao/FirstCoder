"""`git_status` 工具。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.permissions.types import PermissionAction
from firstcoder.tools.types import Tool, ToolPermissionSpec, ToolResult, make_error_result, make_text_result
from firstcoder.utils import git as git_utils
from firstcoder.utils.introspection import tool_from_function
from firstcoder.utils.sandbox import PathSandbox
from firstcoder.utils.sandbox_access import SandboxAccess


def create_git_status_tool(root: str | Path, *, access: SandboxAccess | None = None) -> Tool:
    """创建查看 git 工作区状态的工具。"""

    sandbox = PathSandbox(root, access=access)

    def git_status() -> ToolResult:
        """查看项目 git 工作区状态；不显示 diff 内容。"""

        repo_result = git_utils.run_git(sandbox, ["rev-parse", "--is-inside-work-tree"])
        if repo_result.returncode != 0:
            return make_error_result("git_status", "当前目录不是 git 仓库")

        status_result = git_utils.run_git(sandbox, ["status", "--short"])
        if status_result.returncode != 0:
            return make_error_result("git_status", status_result.stderr.strip() or "git status 执行失败")

        content = status_result.stdout.strip() or "工作区干净。"
        return make_text_result(
            "git_status",
            content,
            clean=status_result.stdout.strip() == "",
        )

    tool = tool_from_function(git_status)
    tool.permission = ToolPermissionSpec(
        action=PermissionAction.GIT_OPERATION,
        target_value="status --short",
        reason="查看 git 状态属于 git 操作。",
    )
    return tool
