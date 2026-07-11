"""L2 内容路由压缩框架。

这一层只负责“识别内容类型 -> 分发到对应压缩器 -> 验证压缩收益 -> 统一写 metadata”。
具体的 search、diff、build、json、code、html 算法会按第 14 步逐个补齐，避免把
路由边界和具体压缩策略耦合在一起。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol

from firstcoder.context.identity import content_fingerprint
from firstcoder.context.models import MessagePart, utc_now_iso
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.context.versions import COMPACTION_STRATEGY_VERSION


class RouteContentType(str, Enum):
    SEARCH_RESULTS = "search_results"
    GIT_DIFF = "git_diff"
    BUILD_OUTPUT = "build_output"
    JSON_ARRAY = "json_array"
    JSON_OBJECT = "json_object"
    SOURCE_CODE = "source_code"
    HTML = "html"
    PLAIN_TEXT = "plain_text"


@dataclass(slots=True)
class RouteDetection:
    content_type: RouteContentType
    confidence: float
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class RouteContext:
    detection: RouteDetection
    preview_chars: int = 160


@dataclass(slots=True)
class RouteCompactResult:
    content: str
    content_type: RouteContentType
    compacted_by: str
    metadata: dict[str, object] = field(default_factory=dict)


class RouteCompressor(Protocol):
    def compact(self, part: MessagePart, context: RouteContext) -> RouteCompactResult | None:
        """返回压缩结果；不适合压缩时返回 None。"""


@dataclass(slots=True)
class RouteCompactRouter:
    compressors: dict[RouteContentType, RouteCompressor] = field(default_factory=dict)
    min_original_tokens: int = 40
    preview_chars: int = 160

    def compact_part(self, part: MessagePart) -> MessagePart | None:
        original_tokens = estimate_text_tokens(part.content)
        if original_tokens < self.min_original_tokens:
            return None

        detection = detect_route_content_type(part.content, tool_name=_tool_name(part))
        route_content_type = detection.content_type
        compressor = self.compressors.get(route_content_type)
        fallback_from: RouteContentType | None = None
        if compressor is None and route_content_type is not RouteContentType.PLAIN_TEXT:
            compressor = self.compressors.get(RouteContentType.PLAIN_TEXT)
            if compressor is not None:
                fallback_from = route_content_type
        if compressor is None:
            return None

        route_result = compressor.compact(part, RouteContext(detection=detection, preview_chars=self.preview_chars))
        if route_result is None:
            return None

        replacement_tokens = estimate_text_tokens(route_result.content)
        if replacement_tokens >= original_tokens:
            return None

        metadata = dict(part.metadata)
        metadata.update(
            {
                "original_tokens": original_tokens,
                "replacement_tokens": replacement_tokens,
                "content_fingerprint": content_fingerprint(part.content),
                "compaction_state": "route_compacted",
                "compacted_by": route_result.compacted_by,
                "compacted_at": utc_now_iso(),
                "compaction_strategy_version": COMPACTION_STRATEGY_VERSION,
                "content_type": route_result.content_type.value,
                "detected_content_type": route_content_type.value,
                "route_confidence": detection.confidence,
                "route_metadata": detection.metadata,
            }
        )
        if fallback_from is not None:
            metadata["route_fallback_from"] = fallback_from.value
        metadata.update(route_result.metadata)

        return MessagePart(
            id=part.id,
            message_id=part.message_id,
            kind=part.kind,
            content=route_result.content,
            metadata=metadata,
        )


_SEARCH_RESULT_PATTERN = re.compile(r"^[^\s:][^:\n]*:\d+:", re.MULTILINE)
_DIFF_HEADER_PATTERN = re.compile(r"^(diff --git|--- a/|\+\+\+ b/|@@\s+-\d+)", re.MULTILINE)
_BUILD_OUTPUT_PATTERN = re.compile(
    r"(FAILED|ERROR|Traceback \(most recent call last\)|pytest|npm ERR!|cargo test|warning:)",
    re.IGNORECASE,
)
_HTML_PATTERN = re.compile(r"<!doctype\s+html|<html[\s>]|<body[\s>]", re.IGNORECASE)
_CODE_PATTERN = re.compile(
    r"^\s*(def|class|import|from|function|const|let|export|interface|type|fn|struct|impl|package)\b",
    re.MULTILINE,
)


def detect_route_content_type(content: str, *, tool_name: str | None = None) -> RouteDetection:
    stripped = content.strip()
    if not stripped:
        return RouteDetection(RouteContentType.PLAIN_TEXT, 0.0)

    tool_hint = (tool_name or "").lower()
    if tool_hint in {"grep", "rg"}:
        return RouteDetection(RouteContentType.SEARCH_RESULTS, 0.95, {"source": "tool_hint"})
    if tool_hint in {"git_diff", "diff"}:
        return RouteDetection(RouteContentType.GIT_DIFF, 0.95, {"source": "tool_hint"})

    json_detection = _detect_json(stripped)
    if json_detection is not None:
        return json_detection

    if _DIFF_HEADER_PATTERN.search(stripped):
        return RouteDetection(RouteContentType.GIT_DIFF, 0.85)
    if _HTML_PATTERN.search(stripped[:3000]):
        return RouteDetection(RouteContentType.HTML, 0.85)
    if _SEARCH_RESULT_PATTERN.search(stripped):
        return RouteDetection(RouteContentType.SEARCH_RESULTS, 0.8)
    if _CODE_PATTERN.search(stripped):
        return RouteDetection(RouteContentType.SOURCE_CODE, 0.6)
    if tool_hint in {"shell", "pytest"} and _BUILD_OUTPUT_PATTERN.search(stripped):
        return RouteDetection(RouteContentType.BUILD_OUTPUT, 0.75, {"source": "tool_hint"})
    if _BUILD_OUTPUT_PATTERN.search(stripped):
        return RouteDetection(RouteContentType.BUILD_OUTPUT, 0.65)
    return RouteDetection(RouteContentType.PLAIN_TEXT, 0.5)


def _detect_json(content: str) -> RouteDetection | None:
    if not content.startswith(("[", "{")):
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None

    if isinstance(parsed, list):
        return RouteDetection(RouteContentType.JSON_ARRAY, 1.0, {"item_count": len(parsed)})
    if isinstance(parsed, dict):
        return RouteDetection(RouteContentType.JSON_OBJECT, 1.0, {"keys": list(parsed.keys())[:20]})
    return None


def _tool_name(part: MessagePart) -> str | None:
    value = part.metadata.get("tool_name")
    return str(value) if value is not None else None
