"""json_array / json_object 输出的确定性压缩器。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from firstcoder.context.content.router import RouteCompactResult, RouteContentType, RouteContext
from firstcoder.context.models import MessagePart

_IMPORTANT_KEYS = {
    "error",
    "errors",
    "exception",
    "failed",
    "failure",
    "message",
    "reason",
    "status",
    "summary",
    "traceback",
    "warning",
    "warnings",
}


@dataclass(slots=True)
class JsonRouteCompressor:
    max_array_items: int = 12
    max_object_keys: int = 24
    max_string_chars: int = 240
    max_nested_items: int = 6

    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        try:
            parsed = json.loads(part.content)
        except json.JSONDecodeError:
            return None

        if isinstance(parsed, list):
            return self._compact_array(parsed, context)
        if isinstance(parsed, dict):
            return self._compact_object(parsed, context)
        return None

    def _compact_array(self, items: list[Any], context: RouteContext) -> RouteCompactResult | None:
        if not items:
            return None

        selected_indexes = _select_array_indexes(items, max_items=self.max_array_items)
        selected_items = [_summarize_value(items[index], self) for index in selected_indexes]
        payload = {
            "_compact": {
                "type": "json_array",
                "original_items": len(items),
                "kept_items": len(selected_items),
                "omitted_items": len(items) - len(selected_items),
                "schema_keys": _schema_keys(items),
            },
            "items": selected_items,
        }

        return RouteCompactResult(
            content=_dump_json(payload),
            content_type=RouteContentType.JSON_ARRAY,
            compacted_by="l2_json_array",
            metadata={
                "json_original_items": len(items),
                "json_kept_items": len(selected_items),
                "json_omitted_items": len(items) - len(selected_items),
                "json_schema_keys": _schema_keys(items),
                "json_detection_type": context.detection.content_type.value,
            },
        )

    def _compact_object(self, value: dict[str, Any], context: RouteContext) -> RouteCompactResult | None:
        if not value:
            return None

        selected_keys = _select_object_keys(value, max_keys=self.max_object_keys)
        compacted = {key: _summarize_value(value[key], self) for key in selected_keys}
        payload = {
            "_compact": {
                "type": "json_object",
                "original_keys": len(value),
                "kept_keys": len(compacted),
                "omitted_keys": len(value) - len(compacted),
                "omitted_key_names": [key for key in value.keys() if key not in compacted][:20],
            },
            "object": compacted,
        }

        return RouteCompactResult(
            content=_dump_json(payload),
            content_type=RouteContentType.JSON_OBJECT,
            compacted_by="l2_json_object",
            metadata={
                "json_original_keys": len(value),
                "json_kept_keys": len(compacted),
                "json_omitted_keys": len(value) - len(compacted),
                "json_detection_type": context.detection.content_type.value,
            },
        )


def _select_array_indexes(items: list[Any], *, max_items: int) -> list[int]:
    if len(items) <= max_items:
        return list(range(len(items)))

    selected: set[int] = set()
    front = max(1, max_items // 3)
    back = max(1, max_items // 4)
    for index in range(min(front, len(items))):
        selected.add(index)
    for index in range(max(0, len(items) - back), len(items)):
        selected.add(index)

    scored = sorted(
        ((index, _value_score(item)) for index, item in enumerate(items)),
        key=lambda item: (item[1], -item[0]),
        reverse=True,
    )
    for index, score in scored:
        if len(selected) >= max_items:
            break
        if score > 0:
            selected.add(index)

    cursor = front
    while len(selected) < max_items and cursor < len(items):
        selected.add(cursor)
        cursor += max(1, len(items) // max_items)

    return sorted(selected)


def _select_object_keys(value: dict[str, Any], *, max_keys: int) -> list[str]:
    keys = list(value.keys())
    if len(keys) <= max_keys:
        return keys

    selected: list[str] = []
    for key in keys:
        if _is_important_key(key) or _value_score(value[key]) > 0:
            selected.append(key)
        if len(selected) >= max_keys:
            return selected

    for key in keys:
        if key not in selected:
            selected.append(key)
        if len(selected) >= max_keys:
            break
    return selected


def _summarize_value(value: Any, config: JsonRouteCompressor) -> Any:
    if isinstance(value, str):
        if len(value) <= config.max_string_chars:
            return value
        return value[: config.max_string_chars].rstrip() + "...[truncated]"
    if isinstance(value, list):
        if len(value) <= config.max_nested_items:
            return [_summarize_value(item, config) for item in value]
        selected = _select_array_indexes(value, max_items=config.max_nested_items)
        return {
            "_type": "array",
            "items": len(value),
            "kept": [_summarize_value(value[index], config) for index in selected],
            "omitted": len(value) - len(selected),
        }
    if isinstance(value, dict):
        if len(value) <= config.max_nested_items:
            return {key: _summarize_value(item, config) for key, item in value.items()}
        selected_keys = _select_object_keys(value, max_keys=config.max_nested_items)
        return {
            "_type": "object",
            "keys": len(value),
            "kept": {key: _summarize_value(value[key], config) for key in selected_keys},
            "omitted": len(value) - len(selected_keys),
        }
    return value


def _schema_keys(items: list[Any]) -> list[str]:
    keys: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in item.keys():
            if key not in seen:
                seen.add(key)
                keys.append(key)
            if len(keys) >= 30:
                return keys
    return keys


def _value_score(value: Any) -> int:
    if isinstance(value, dict):
        score = sum(20 for key in value.keys() if _is_important_key(key))
        score += sum(_value_score(item) for item in value.values())
        return score
    if isinstance(value, list):
        return sum(_value_score(item) for item in value[:20])
    if isinstance(value, str):
        lowered = value.lower()
        return sum(10 for keyword in _IMPORTANT_KEYS if keyword in lowered)
    return 0


def _is_important_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _IMPORTANT_KEYS or any(marker in lowered for marker in ("error", "fail", "warn", "trace"))


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
