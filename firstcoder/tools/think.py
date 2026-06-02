"""`think` 工具。

这是一个无副作用的推理工具，让模型可以把中间思考过程显式输出到上下文中。
类似于 Claude Code 的 thinking 机制或 o1/R1 的显式 reasoning。
"""

from __future__ import annotations

from firstcoder.tools.types import Tool, ToolResult, make_text_result
from firstcoder.utils.introspection import tool_from_function


def create_think_tool() -> Tool:
    """创建推理思考工具。"""

    def think(thought: str) -> ToolResult:
        """记录内部思考；不访问外部资源，不修改状态。"""

        return make_text_result("think", thought, thought=thought)

    return tool_from_function(think)
