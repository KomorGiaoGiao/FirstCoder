"""工具层 registry 和通用行为测试。"""

from __future__ import annotations

from firstcoder.tools import ToolRegistry, create_builtin_registry
from firstcoder.tools.edit import create_edit_tool
from firstcoder.tools.fetch import create_fetch_tool
from firstcoder.tools.glob import create_glob_tool
from firstcoder.tools.grep import create_grep_tool
from firstcoder.tools.ls import create_ls_tool
from firstcoder.tools.tree import create_tree_tool
from firstcoder.tools.view import create_view_tool
from firstcoder.tools.write import create_write_tool
from firstcoder.tools.delete import create_delete_tool
from firstcoder.tools.apply_patch import create_apply_patch_tool
from firstcoder.tools.diagnostics import create_diagnostics_tool
from firstcoder.tools.python_exec import create_python_exec_tool
from firstcoder.tools.shell import create_shell_tool
from firstcoder.tools.task_boundary import create_task_boundary_tool
from firstcoder.tools.think import create_think_tool
from firstcoder.tools.read_multi import create_read_multi_tool
from firstcoder.tools.ask_user import create_ask_user_tool
from firstcoder.tools.todo import create_todo_tool
from firstcoder.tools.git_log import create_git_log_tool
from firstcoder.tools.git_diff import create_git_diff_tool
from firstcoder.tools.git_status import create_git_status_tool
from firstcoder.tools.web_search import create_web_search_tool


def test_builtin_tool_descriptions_are_agent_facing_english(tmp_path):
    registry = create_builtin_registry(
        tmp_path,
        include_mutation_tools=True,
        include_execution_tools=True,
        include_network_tools=True,
    )
    descriptions = {definition.name: definition.description for definition in registry.definitions()}

    assert descriptions["view"].startswith("Read a UTF-8 text file")
    assert "Use this instead of shell commands like cat" in descriptions["view"]
    assert descriptions["grep"].startswith("Search file contents")
    assert "literal text" in descriptions["grep"]
    assert descriptions["apply_patch"].startswith("Apply a structured patch")
    assert "Prefer this for multi-file edits" in descriptions["apply_patch"]
    assert descriptions["shell"].startswith("Run a shell command")
    assert "Prefer dedicated tools" in descriptions["shell"]
    assert descriptions["todo"].startswith("Replace the current Todo list")
    assert "at most one item may be in_progress" in descriptions["todo"]
    assert descriptions["todo"] == create_todo_tool().definition.description
    assert "evidence returned to the model" in descriptions["diagnostics"]


def test_builtin_registry_contains_read_only_tools(tmp_path):
    registry = create_builtin_registry(tmp_path)

    assert registry.names() == [
        "ls", "view", "grep", "glob", "tree",
        "git_status", "git_diff", "git_log",
        "diagnostics", "think", "read_multi", "ask_user", "todo",
    ]
    assert [definition.name for definition in registry.definitions()] == registry.names()
    assert [tool.name for tool in registry.tools()] == registry.names()


def test_each_tool_has_its_own_module():
    assert create_ls_tool.__module__ == "firstcoder.tools.ls"
    assert create_view_tool.__module__ == "firstcoder.tools.view"
    assert create_grep_tool.__module__ == "firstcoder.tools.grep"
    assert create_glob_tool.__module__ == "firstcoder.tools.glob"
    assert create_tree_tool.__module__ == "firstcoder.tools.tree"
    assert create_git_status_tool.__module__ == "firstcoder.tools.git_status"
    assert create_git_diff_tool.__module__ == "firstcoder.tools.git_diff"
    assert create_write_tool.__module__ == "firstcoder.tools.write"
    assert create_edit_tool.__module__ == "firstcoder.tools.edit"
    assert create_delete_tool.__module__ == "firstcoder.tools.delete"
    assert create_fetch_tool.__module__ == "firstcoder.tools.fetch"
    assert create_web_search_tool.__module__ == "firstcoder.tools.web_search"
    assert create_apply_patch_tool.__module__ == "firstcoder.tools.apply_patch"
    assert create_diagnostics_tool.__module__ == "firstcoder.tools.diagnostics"
    assert create_python_exec_tool.__module__ == "firstcoder.tools.python_exec"
    assert create_shell_tool.__module__ == "firstcoder.tools.shell"
    assert create_task_boundary_tool.__module__ == "firstcoder.tools.task_boundary"
    assert create_think_tool.__module__ == "firstcoder.tools.think"
    assert create_read_multi_tool.__module__ == "firstcoder.tools.read_multi"
    assert create_ask_user_tool.__module__ == "firstcoder.tools.ask_user"
    assert create_todo_tool.__module__ == "firstcoder.tools.todo"
    assert create_git_log_tool.__module__ == "firstcoder.tools.git_log"


def test_builtin_registry_can_include_mutation_tools_when_explicitly_enabled(tmp_path):
    registry = create_builtin_registry(tmp_path, include_mutation_tools=True)

    assert registry.names() == [
        "ls", "view", "grep", "glob", "tree",
        "git_status", "git_diff", "git_log",
        "diagnostics", "think", "read_multi", "ask_user", "todo",
        "write", "edit", "delete", "apply_patch",
    ]


def test_builtin_registry_can_include_network_tools_when_explicitly_enabled(tmp_path):
    registry = create_builtin_registry(tmp_path, include_network_tools=True)

    assert registry.names() == [
        "ls", "view", "grep", "glob", "tree",
        "git_status", "git_diff", "git_log",
        "diagnostics", "think", "read_multi", "ask_user", "todo",
        "fetch", "web_search",
    ]


def test_builtin_registry_can_include_execution_tools_when_explicitly_enabled(tmp_path):
    registry = create_builtin_registry(tmp_path, include_execution_tools=True)

    assert registry.names() == [
        "ls", "view", "grep", "glob", "tree",
        "git_status", "git_diff", "git_log",
        "diagnostics", "think", "read_multi", "ask_user", "todo",
        "shell", "python_exec",
    ]


def test_builtin_tool_definitions_are_generated_from_function_signatures(tmp_path):
    registry = create_builtin_registry(tmp_path)
    definitions = {definition.name: definition for definition in registry.definitions()}

    assert definitions["view"].parameters == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
        "required": ["path"],
    }
    assert definitions["grep"].parameters["required"] == ["pattern"]
    assert definitions["glob"].parameters["required"] == ["pattern"]


def test_registry_returns_error_for_unknown_tool():
    registry = ToolRegistry()

    result = registry.execute("missing_tool", {})

    assert result.ok is False
    assert result.error == "未知工具：missing_tool"
