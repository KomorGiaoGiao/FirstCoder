"""从函数签名生成工具定义的测试。"""

from __future__ import annotations

import pytest

from firstcoder.utils.introspection import function_to_parameters, tool_from_function
from firstcoder.tools.types import ToolResult, make_text_result


def sample_tool(path: str, max_chars: int = 100, dry_run: bool = False, ratio: float = 0.5) -> ToolResult:
    """读取文件内容。"""

    return make_text_result("sample_tool", f"{path}:{max_chars}:{dry_run}:{ratio}")


def test_function_to_parameters_uses_signature_annotations_and_defaults():
    parameters = function_to_parameters(sample_tool)

    assert parameters == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer"},
            "dry_run": {"type": "boolean"},
            "ratio": {"type": "number"},
        },
        "required": ["path"],
    }


def test_tool_from_function_builds_definition_and_keeps_executor():
    tool = tool_from_function(sample_tool)

    assert tool.name == "sample_tool"
    assert tool.definition.description == "读取文件内容。"
    assert tool.definition.parameters["required"] == ["path"]
    assert tool.executor(path="README.md").content == "README.md:100:False:0.5"


def test_tool_from_function_allows_name_and_description_override():
    tool = tool_from_function(sample_tool, name="read_file", description="读取项目文件。")

    assert tool.name == "read_file"
    assert tool.definition.description == "读取项目文件。"


def test_function_to_parameters_rejects_args_and_kwargs():
    def bad_tool(path: str, *args: str) -> ToolResult:
        return make_text_result("bad_tool", path)

    with pytest.raises(ValueError, match="不支持可变参数"):
        function_to_parameters(bad_tool)
