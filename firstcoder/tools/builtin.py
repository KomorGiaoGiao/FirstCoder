"""内置工具集合。"""

from __future__ import annotations

from pathlib import Path

from firstcoder.tools.apply_patch import create_apply_patch_tool
from firstcoder.tools.ask_user import create_ask_user_tool
from firstcoder.tools.edit import create_edit_tool
from firstcoder.tools.delete import create_delete_tool
from firstcoder.tools.diagnostics import create_diagnostics_tool
from firstcoder.tools.git_diff import create_git_diff_tool
from firstcoder.tools.git_log import create_git_log_tool
from firstcoder.tools.git_status import create_git_status_tool
from firstcoder.tools.fetch import create_fetch_tool
from firstcoder.tools.glob import create_glob_tool
from firstcoder.tools.grep import create_grep_tool
from firstcoder.tools.ls import create_ls_tool
from firstcoder.tools.python_exec import create_python_exec_tool
from firstcoder.tools.read_multi import create_read_multi_tool
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.think import create_think_tool
from firstcoder.tools.todo import create_todo_tool
from firstcoder.tools.shell import create_shell_tool
from firstcoder.tools.tree import create_tree_tool
from firstcoder.tools.view import create_view_tool
from firstcoder.tools.web_search import create_web_search_tool
from firstcoder.tools.write import create_write_tool


def create_builtin_registry(
    root: str | Path,
    include_mutation_tools: bool = False,
    include_execution_tools: bool = False,
    include_network_tools: bool = False,
) -> ToolRegistry:
    """创建第一阶段默认可用工具。

    默认只注册只读工具。写入类工具必须显式启用，方便后续接确认机制。
    """

    tools = [
        create_ls_tool(root),
        create_view_tool(root),
        create_grep_tool(root),
        create_glob_tool(root),
        create_tree_tool(root),
        create_git_status_tool(root),
        create_git_diff_tool(root),
        create_git_log_tool(root),
        create_diagnostics_tool(root),
        create_think_tool(),
        create_read_multi_tool(root),
        create_ask_user_tool(),
        create_todo_tool(),
    ]
    if include_mutation_tools:
        tools.extend(
            [
                create_write_tool(root),
                create_edit_tool(root),
                create_delete_tool(root),
                create_apply_patch_tool(root),
            ]
        )
    if include_execution_tools:
        tools.extend(
            [
                create_shell_tool(root),
                create_python_exec_tool(root),
            ]
        )
    if include_network_tools:
        tools.extend(
            [
                create_fetch_tool(),
                create_web_search_tool(),
            ]
        )
    return ToolRegistry(tools)
