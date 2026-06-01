"""工具定义、注册和执行入口。"""

from firstcoder.tools.builtin import create_builtin_registry
from firstcoder.tools.apply_patch import create_apply_patch_tool
from firstcoder.tools.ask_user import create_ask_user_tool
from firstcoder.tools.delete import create_delete_tool
from firstcoder.tools.diagnostics import create_diagnostics_tool
from firstcoder.tools.edit import create_edit_tool
from firstcoder.tools.fetch import create_fetch_tool
from firstcoder.tools.git_diff import create_git_diff_tool
from firstcoder.tools.git_log import create_git_log_tool
from firstcoder.tools.git_status import create_git_status_tool
from firstcoder.tools.glob import create_glob_tool
from firstcoder.tools.grep import create_grep_tool
from firstcoder.tools.ls import create_ls_tool
from firstcoder.tools.python_exec import create_python_exec_tool
from firstcoder.tools.read_multi import create_read_multi_tool
from firstcoder.tools.shell import create_shell_tool
from firstcoder.tools.registry import ToolRegistry
from firstcoder.tools.think import create_think_tool
from firstcoder.tools.todo import create_todo_tool
from firstcoder.tools.tree import create_tree_tool
from firstcoder.tools.types import Tool, ToolExecutor, ToolResult
from firstcoder.tools.view import create_view_tool
from firstcoder.tools.web_search import create_web_search_tool
from firstcoder.tools.write import create_write_tool

__all__ = [
    "Tool",
    "ToolExecutor",
    "ToolRegistry",
    "ToolResult",
    "create_apply_patch_tool",
    "create_ask_user_tool",
    "create_builtin_registry",
    "create_delete_tool",
    "create_diagnostics_tool",
    "create_edit_tool",
    "create_fetch_tool",
    "create_git_diff_tool",
    "create_git_log_tool",
    "create_git_status_tool",
    "create_glob_tool",
    "create_grep_tool",
    "create_ls_tool",
    "create_python_exec_tool",
    "create_read_multi_tool",
    "create_shell_tool",
    "create_think_tool",
    "create_todo_tool",
    "create_tree_tool",
    "create_view_tool",
    "create_web_search_tool",
    "create_write_tool",
]
