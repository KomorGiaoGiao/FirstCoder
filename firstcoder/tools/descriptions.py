"""Agent-facing tool descriptions.

Tool docstrings stay close to implementation. These descriptions are the compact
instructions shown to the model, so they explain when to use each tool and the
important boundary conditions.
"""

from __future__ import annotations

from firstcoder.tools.types import Tool


TOOL_DESCRIPTIONS: dict[str, str] = {
    "ls": (
        "List entries in a workspace directory. Use for quick directory inspection; "
        "returns names and file/dir types, not file contents."
    ),
    "view": (
        "Read a UTF-8 text file by line range. Use this instead of shell commands "
        "like cat, head, tail, or sed when inspecting file contents."
    ),
    "grep": (
        "Search file contents for literal text inside the workspace. Use this to "
        "find symbols, strings, and call sites before editing."
    ),
    "glob": (
        "Find workspace paths with a glob pattern. Use this to locate files by name "
        "or extension; use grep when you need to search file contents."
    ),
    "tree": (
        "Show a bounded directory tree. Use this for orientation when you need the "
        "project layout, not for reading file contents."
    ),
    "git_status": (
        "Show concise git working tree status. Use before or after edits to see "
        "changed files without reading diffs."
    ),
    "git_diff": (
        "Show unstaged or staged git diff. Use to review pending changes before "
        "summarizing, testing, or committing."
    ),
    "git_log": (
        "Show recent git commit history, optionally scoped to a path. Use when "
        "history helps explain current behavior or conventions."
    ),
    "diagnostics": (
        "Run a project verification command such as tests, lint, or typecheck. Use "
        "for validation; successful test commands may signal that the task is done."
    ),
    "think": (
        "Record brief private scratch reasoning without touching the filesystem. Use "
        "sparingly for planning or debugging; do not put long user-facing narration here."
    ),
    "read_multi": (
        "Read multiple UTF-8 text files in one call with a shared output budget. Use "
        "when several known files are needed together."
    ),
    "ask_user": (
        "Ask the user for required information and pause the turn. Use only when the "
        "answer cannot be safely discovered from the workspace or commands."
    ),
    "todo": (
        "Track progress for multi-step work. Prefer action='set' once at the start "
        "to create the whole plan, then action='update' as work progresses. Keep "
        "exactly one item in_progress while actively working."
    ),
    "write": (
        "Write a UTF-8 text file in the workspace. Use for new files or full-file "
        "replacement; prefer edit or apply_patch for targeted changes."
    ),
    "edit": (
        "Replace a specific UTF-8 text snippet in one workspace file. Use for precise "
        "single-file edits after reading the target file."
    ),
    "delete": (
        "Delete a workspace file or directory. Use only when deletion is explicitly "
        "required; directories require recursive=true."
    ),
    "apply_patch": (
        "Apply a structured patch to add, update, delete, or move text files. Prefer "
        "this for multi-file edits or changes where patch review matters."
    ),
    "shell": (
        "Run a shell command in the workspace for validation, project scripts, and "
        "data/toolchain inspection. Prefer dedicated tools for ordinary reading, "
        "searching, and editing. Use shell to run tests across languages (pytest, "
        "npm/pnpm/yarn test, go test, cargo test, make test), inspect SQLite files "
        "with sqlite3 or Python sqlite3, process XLSX files with Python/openpyxl, "
        "and apply ordered patch stacks with git apply before verifying outputs."
    ),
    "python_exec": (
        "Run short Python code in the workspace. Use for focused data inspection or "
        "small scripts; prefer project tests and dedicated tools when available."
    ),
    "fetch": (
        "Fetch a URL and return bounded text content. Use only when network access is "
        "needed and the URL is relevant to the task."
    ),
    "web_search": (
        "Search the web for current external information. Use only when local files "
        "are insufficient or the user asks for up-to-date information."
    ),
    "task_boundary": (
        "Report whether the current user message starts a new task. Pass only "
        "decision and basis_message_id. Do not provide task hashes; the system "
        "generates and validates them."
    ),
}


def apply_agent_tool_description(tool: Tool) -> Tool:
    """Replace a tool's model-visible description when a curated one exists."""

    description = TOOL_DESCRIPTIONS.get(tool.name)
    if description:
        tool.definition.description = description
    return tool
