"""项目内部共用的 JSON 辅助函数。"""

from __future__ import annotations

import json
from typing import Any


def dumps_json(value: Any) -> str:
    """把 Python 对象序列化成紧凑 JSON 字符串。"""

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def loads_json(value: str) -> Any:
    """把 JSON 字符串解析成 Python 对象。

    这个函数保留 `json.loads` 的严格语义：解析失败时抛出 `ValueError`。
    工具协议解析层需要区分“不是 JSON”和“JSON 但结构不符合预期”，所以不能复用
    `loads_json_object` 那种失败后回退为原字符串的宽松行为。
    """

    return json.loads(value)


def loads_json_object(value: str) -> dict[str, Any] | str:
    """把 JSON 字符串解析成对象；失败或不是对象时保留原字符串。"""

    if not value:
        return {}

    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return value

    if isinstance(parsed, dict):
        return parsed
    return value
