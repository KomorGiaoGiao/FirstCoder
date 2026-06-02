"""`ask_user` 工具。

当模型遇到歧义、需要确认、或缺少关键信息时，主动向用户提问。
返回的结果包含 `requires_user_input` 标记，后续 agent 主循环可以识别这个标记
并暂停执行，等待用户回答后再继续。

这是骨架阶段的实现：工具本身只负责生成标准化的问题请求，
真正的"暂停并等待用户输入"由上层 agent 主循环和 UI 负责处理。
"""

from __future__ import annotations

from firstcoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from firstcoder.utils.introspection import tool_from_function


def create_ask_user_tool() -> Tool:
    """创建向用户提问的工具。"""

    def ask_user(question: str, options: list[str] | None = None) -> ToolResult:
        """缺少关键信息或需要用户确认时提问；会暂停等待回答。"""

        if not question.strip():
            return make_error_result("ask_user", "question 不能为空")

        lines: list[str] = [question]
        if options:
            for index, option in enumerate(options, start=1):
                lines.append(f"{index}. {option}")

        content = "\n".join(lines)
        data: dict[str, object] = {
            "requires_user_input": True,
            "question": question,
        }
        if options:
            data["options"] = options

        return make_text_result("ask_user", content, **data)

    return tool_from_function(ask_user)
