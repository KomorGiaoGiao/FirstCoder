"""Activity-line, tool-event, and task-plan rendering helpers."""

from __future__ import annotations

from collections.abc import Mapping

from rich.markup import escape


def activity_markup(text: str) -> str:
    color = "#7bba55"
    if text.startswith("waiting"):
        color = "#b28443"
    elif text.startswith("running"):
        color = "#808185"
    elif text.startswith("streaming"):
        color = "#6e6d72"
    elif text.startswith("error"):
        color = "#c85f5f"
    return f"[{color}]{escape(text)}[/]"


def truncate_activity_text(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    if width <= 1:
        return text[:width]
    return text[: width - 1] + "."


def turn_metrics_text(elapsed_seconds: float, tool_count: int) -> str:
    elapsed = format_elapsed_time(elapsed_seconds)
    return f"{elapsed} · {tool_count} {'tool' if tool_count == 1 else 'tools'}"


def format_elapsed_time(elapsed_seconds: float) -> str:
    if elapsed_seconds < 60:
        return f"{max(0.0, elapsed_seconds):.1f}s"
    total_seconds = int(max(0, elapsed_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"


def tool_event_status(event) -> str | None:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "prewrite_review":
        return "review"
    if kind == "started":
        return "running"
    if kind == "finished":
        result = getattr(event, "result", None)
        return "success" if getattr(result, "ok", False) else "error"
    if kind == "permission_requested":
        return "permission_requested"
    if kind == "denied":
        return "denied"
    if kind == "skipped":
        return "skipped"
    return None


def tool_event_label(event) -> str:
    tool_call = getattr(event, "tool_call", None)
    name = str(getattr(tool_call, "name", "") or "tool")
    status = tool_event_status(event)
    if status == "permission_requested":
        return "permission requested"
    return f"tool {name} {status}" if status else f"tool {name}"


def tool_activity_summary(event) -> str:
    kind = str(getattr(event, "kind", "") or "")
    if kind == "started":
        tool_call = getattr(event, "tool_call", None)
        return compact_tool_arguments(getattr(tool_call, "arguments", None))
    if kind == "finished":
        result = getattr(event, "result", None)
        return compact_tool_content(str(getattr(result, "content", "") or ""))
    return ""


def tool_activity_line_text(name: str, status: str) -> str:
    if status == "running":
        return f"running · {name}"
    if status == "success":
        return post_tool_reasoning_text(name)
    if status == "permission_requested":
        return "waiting · permission"
    if status in {"error", "failed"}:
        return f"error · {name}"
    return f"{status} · {name}"


def post_tool_reasoning_text(name: str) -> str:
    return f"reading {name} result"


def task_plan_panel_text(projection: Mapping[str, object]) -> str:
    """Render one canonical task-plan projection without retaining plan data."""

    mode = str(projection["mode"])
    tasks = _task_lookup(projection.get("tasks"))
    ready = _task_id_set(projection.get("ready_task_ids"))
    blocked = _task_id_set(projection.get("blocked_task_ids"))

    if mode == "linear":
        lines = ["Task Plan · linear"]
        for task_id, task in sorted(tasks.items(), key=_task_order_key):
            lines.append(f"{_task_marker(task_id, task, ready, blocked)} {task['content']}")
        return "\n".join(lines)

    lines = ["Task Plan · dag"]
    for level_index, level in enumerate(_topological_levels(projection.get("topological_levels"))):
        suffix = " · parallel" if len(level) > 1 else ""
        lines.append(f"Level {level_index}{suffix}")
        for task_id in level:
            task = tasks[task_id]
            dependency_text = _dependency_text(task)
            lines.append(f"  {_task_marker(task_id, task, ready, blocked)} {task['content']} ({task_id}){dependency_text}")
    return "\n".join(lines)


def _task_lookup(value: object) -> dict[str, dict[str, object]]:
    if not isinstance(value, list):
        return {}
    return {str(task["id"]): dict(task) for task in value if isinstance(task, Mapping) and isinstance(task.get("id"), str) and isinstance(task.get("content"), str)}


def _task_id_set(value: object) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {task_id for task_id in value if isinstance(task_id, str)}


def _task_order_key(item: tuple[str, dict[str, object]]) -> tuple[int, str]:
    task_id, task = item
    order = task.get("order")
    return (order if isinstance(order, int) else 0, task_id)


def _topological_levels(value: object) -> list[list[str]]:
    if not isinstance(value, list):
        return []
    return [[task_id for task_id in level if isinstance(task_id, str)] for level in value if isinstance(level, list)]


def _task_marker(
    task_id: str,
    task: Mapping[str, object],
    ready: set[str],
    blocked: set[str],
) -> str:
    status = task.get("status")
    if status == "completed":
        return "[✓]"
    if status == "cancelled":
        return "[-]"
    if status == "in_progress":
        return "[~]"
    if task_id in ready:
        return "[→]"
    if task_id in blocked:
        return "[!]"
    return "[ ]"


def _dependency_text(task: Mapping[str, object]) -> str:
    dependencies = task.get("depends_on")
    if not isinstance(dependencies, list) or not dependencies:
        return ""
    return " · depends on: " + ", ".join(str(dependency) for dependency in dependencies)


def tool_status_text(event) -> str:
    tool_call = getattr(event, "tool_call", None)
    name = str(getattr(tool_call, "name", "") or "tool")
    kind = str(getattr(event, "kind", "") or "")
    if kind == "prewrite_review":
        return ""
    if kind == "started":
        arguments = compact_tool_arguments(getattr(tool_call, "arguments", None))
        suffix = f" {arguments}" if arguments else ""
        return f"正在调用工具：{name}{suffix}"
    if kind == "finished":
        result = getattr(event, "result", None)
        status = "完成" if getattr(result, "ok", False) else "失败"
        content = compact_tool_content(str(getattr(result, "content", "") or ""))
        suffix = f"：{content}" if content else ""
        return f"工具{status}：{name}{suffix}"
    if kind == "permission_requested":
        request = getattr(event, "permission_request", None)
        target = str(getattr(request, "target", "") or "")
        action = str(getattr(request, "action", "") or "")
        suffix = f"  {action} {target}".rstrip() if action or target else f"  {name}"
        return f"permission requested{suffix}"
    if kind == "denied":
        return f"工具已拒绝：{name}"
    if kind == "skipped":
        return f"工具已跳过：{name}"
    return ""


def compact_tool_arguments(arguments) -> str:
    if not arguments:
        return ""
    rendered = str(arguments)
    return compact_tool_content(rendered, max_chars=120)


def compact_tool_content(text: str, max_chars: int = 180) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return "." * max_chars
    return normalized[: max_chars - 3] + "..."
