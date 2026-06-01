"""工具参数 JSON Schema 构造测试。"""

from __future__ import annotations

from firstcoder.utils.schema import object_schema, property_schema


def test_object_schema_builds_required_properties():
    schema = object_schema(
        {
            "path": property_schema("string"),
            "max_chars": property_schema("integer"),
        },
        required=["path"],
    )

    assert schema == {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "max_chars": {"type": "integer"},
        },
        "required": ["path"],
    }


def test_property_schema_supports_extra_constraints():
    assert property_schema("array", items=property_schema("string")) == {
        "type": "array",
        "items": {"type": "string"},
    }
