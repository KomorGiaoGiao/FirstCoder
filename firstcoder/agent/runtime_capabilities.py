"""Agent 运行能力配置与 benchmark 工具路由。"""

from __future__ import annotations

import re
from dataclasses import dataclass

BENCHMARK_BACKGROUND_TOOL_NAMES = frozenset(
    {
        "diagnostics",
        "shell",
        "python_exec",
        "fetch",
        "delegate",
    }
)
PLANNING_TOOL_NAMES = frozenset(
    {"task_create", "task_update", "task_revise", "task_list"}
)

_COMPLEX_TASK_KEYWORDS = re.compile(
    r"\b(?:"
    r"compile|configure|install|service|daemon|server|docker|qemu|ssh|"
    r"repository[- ]wide|multiple files?|end[- ]to[- ]end|migration|"
    r"integration|benchmark|refactor"
    r")\b",
    re.IGNORECASE,
)
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")


@dataclass(frozen=True, slots=True)
class AgentRuntimeCapabilities:
    """由 app/factory 统一装配的运行能力，而不是散落的 benchmark 分支。"""

    allow_user_input: bool = True
    enable_completion_gate: bool = False
    enable_stagnation_guard: bool = False
    enable_delegate_tool: bool = True
    expose_planning_tools: bool = True
    expose_think_tool: bool = True
    expose_web_search_tool: bool = True
    enable_process_tools: bool = True
    background_tool_names: frozenset[str] | None = None

    @classmethod
    def interactive(cls) -> "AgentRuntimeCapabilities":
        return cls()

    @classmethod
    def benchmark(cls, task: str) -> "AgentRuntimeCapabilities":
        complex_task = benchmark_task_is_complex(task)
        return cls(
            allow_user_input=False,
            enable_completion_gate=True,
            enable_stagnation_guard=True,
            enable_delegate_tool=complex_task,
            expose_planning_tools=complex_task,
            expose_think_tool=False,
            expose_web_search_tool=False,
            enable_process_tools=True,
            background_tool_names=BENCHMARK_BACKGROUND_TOOL_NAMES,
        )


def benchmark_task_is_complex(task: str) -> bool:
    """保守识别值得暴露 TaskPlan/delegate 的多步骤 benchmark 任务。"""

    text = task.strip()
    if len(text) >= 1200:
        return True
    list_items = sum(1 for line in text.splitlines() if _LIST_ITEM_RE.match(line))
    if list_items >= 3:
        return True
    keyword_hits = len(_COMPLEX_TASK_KEYWORDS.findall(text))
    return keyword_hits >= 2
