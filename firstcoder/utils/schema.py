"""项目内部 JSON Schema 构造辅助函数。"""

from __future__ import annotations

from typing import Any, Literal

JsonSchemaType = Literal["string", "integer", "boolean", "number", "object", "array"]


def property_schema(schema_type: JsonSchemaType, **extra: Any) -> dict[str, Any]:
    """创建单个参数的 JSON Schema。"""

    schema: dict[str, Any] = {"type": schema_type}
    schema.update(extra)
    return schema


def object_schema(properties: dict[str, dict[str, Any]], required: list[str] | None = None) -> dict[str, Any]:
    """创建工具参数对象 schema。"""

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema
