"""JSON 辅助函数测试。"""

from __future__ import annotations

from firstcoder.utils.json_utils import dumps_json, loads_json_object


def test_dumps_json_uses_compact_non_ascii_json():
    assert dumps_json({"path": "中文.md"}) == '{"path":"中文.md"}'


def test_loads_json_object_parses_object():
    assert loads_json_object('{"path":"README.md"}') == {"path": "README.md"}


def test_loads_json_object_keeps_invalid_json():
    assert loads_json_object("{bad json") == "{bad json"


def test_loads_json_object_keeps_non_object_json():
    assert loads_json_object('["README.md"]') == '["README.md"]'
