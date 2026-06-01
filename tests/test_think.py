"""`think` 工具测试。"""

from __future__ import annotations

from firstcoder.tools.think import create_think_tool


def test_think_returns_input_as_result():
    tool = create_think_tool()

    result = tool.executor(thought="我需要分析这段代码的依赖关系。")

    assert result.ok is True
    assert result.name == "think"
    assert "我需要分析这段代码的依赖关系。" in result.content
    assert result.data["thought"] == "我需要分析这段代码的依赖关系。"


def test_think_accepts_empty_string():
    tool = create_think_tool()

    result = tool.executor(thought="")

    assert result.ok is True
    assert result.content == ""
    assert result.data["thought"] == ""


def test_think_definition_has_correct_schema():
    tool = create_think_tool()

    assert tool.name == "think"
    assert tool.definition.description
    assert "thought" in tool.definition.parameters["properties"]
    assert tool.definition.parameters["required"] == ["thought"]
    assert tool.definition.parameters["properties"]["thought"]["type"] == "string"
