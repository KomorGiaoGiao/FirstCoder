"""VCR 式 provider cassette：录制/回放 LLM 交互。

借鉴 opencode 的 http-recorder，但挂在消息层而不是 HTTP 层：所有 agent 侧流量都
收敛到 ``ChatProvider.complete`` / ``acomplete`` / ``astream``，所以用装饰器包住
这三个方法即可覆盖非流式和流式调用，不需要引入 vcrpy 之类的新依赖。

cassette 是提交进仓库的 JSON 文件，格式对齐 opencode::

    {"version": 1,
     "metadata": {"provider": "...", "model": "...", "recorded_at": "..."},
     "interactions": [{"kind": "complete", "request": {...}, "response": {...}},
                      {"kind": "stream", "request": {...}, "events": [...]}]}

运行模式由 ``FIRSTCODER_CASSETTE_MODE`` 控制（auto/record/replay/off，默认 auto：
有 cassette 就回放，没有就录制）。``CI=true`` 时 auto 退化为 replay，cassette 缺失
直接抛 ``CassetteMissingError``，避免 CI 悄悄触网。

``ChatResponse.raw`` 是 SDK 原始对象，不可序列化，录制时丢弃；agent 主循环只透传
raw 不读取内容，回放语义不受影响。

落盘前对序列化后的 JSON 做一轮脱敏（secret/路径/PII），这是纵深防御；提交 cassette
前仍然需要人工 review diff。
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Literal

from firstcoder.providers.base import ChatProvider
from firstcoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    ContentPart,
    ProviderDiagnostics,
    TokenUsage,
    ToolCall,
    ToolChoice,
    ToolChoiceFunction,
    ToolDefinition,
)
from firstcoder.session.models import PiiOptions, RedactionOptions
from firstcoder.session.redaction import redact_text
from firstcoder.utils.json_utils import dumps_json

CassetteMode = Literal["auto", "record", "replay", "off"]

CASSETTE_VERSION = 1
CASSETTE_MODE_ENV = "FIRSTCODER_CASSETTE_MODE"
_VALID_MODES: tuple[str, ...] = ("auto", "record", "replay", "off")


class CassetteError(Exception):
    """cassette 基础设施错误的基类。"""


class CassetteMissingError(CassetteError):
    """replay（或 CI 上的 auto）模式下 cassette 文件不存在。"""


class CassetteMismatchError(CassetteError):
    """回放时找不到与当前请求匹配的未消费交互。"""


class CassetteUnusedError(CassetteError):
    """回放结束后仍有未消费的交互（对齐 opencode "unused interactions fail"）。"""


class CassetteNameError(CassetteError):
    """cassette 名称非法（绝对路径或路径穿越）。"""


@dataclass(slots=True)
class _Interaction:
    """一条已录制交互；回放时用 consumed 标记"认领第一条未消费的匹配交互"。"""

    kind: Literal["complete", "stream"]
    request: dict[str, Any]
    response: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    consumed: bool = False


def resolve_cassette_path(root: Path | str, name: str) -> Path:
    """把 cassette 名解析成 ``root`` 下的文件路径，拒绝路径穿越。

    允许 ``provider/scenario`` 式子路径；没有 ``.json`` 后缀时自动补上。
    """

    if not name or not name.strip():
        raise CassetteNameError("cassette 名称不能为空")
    pure = PurePath(name)
    if pure.is_absolute() or (len(name) >= 2 and name[1] == ":"):
        raise CassetteNameError(f"cassette 名称不允许绝对路径: {name!r}")
    if any(part == ".." for part in pure.parts):
        raise CassetteNameError(f"cassette 名称不允许路径穿越: {name!r}")
    relative = Path(*pure.parts)
    if relative.suffix != ".json":
        relative = relative.with_suffix(".json")
    root_path = Path(root)
    candidate = root_path / relative
    # 双保险: 解析后的真实路径必须仍在 root 之内（防符号链接等逃逸）
    resolved_root = root_path.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_root != resolved_candidate and resolved_root not in resolved_candidate.parents:
        raise CassetteNameError(f"cassette 路径越出根目录: {name!r}")
    return candidate


def request_to_dict(request: ChatRequest) -> dict[str, Any]:
    """把 ``ChatRequest`` 序列化成 JSON 友好的 dict。"""

    return {
        "messages": [_message_to_dict(message) for message in request.messages],
        "tools": [_tool_definition_to_dict(tool) for tool in request.tools],
        "tool_choice": _tool_choice_to_dict(request.tool_choice),
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "extra_body": request.extra_body,
    }


def request_from_dict(data: dict[str, Any]) -> ChatRequest:
    """从 dict 还原 ``ChatRequest``，与 ``request_to_dict`` 互逆。"""

    return ChatRequest(
        messages=[_message_from_dict(item) for item in data["messages"]],
        tools=[_tool_definition_from_dict(item) for item in data["tools"]],
        tool_choice=_tool_choice_from_dict(data["tool_choice"]),
        temperature=data["temperature"],
        max_tokens=data["max_tokens"],
        extra_body=data["extra_body"],
    )


def response_to_dict(response: ChatResponse) -> dict[str, Any]:
    """把 ``ChatResponse`` 序列化成 dict；``raw`` 是 SDK 原始对象，不录制。"""

    return {
        "provider": response.provider,
        "model": response.model,
        "content": response.content,
        "tool_calls": [_tool_call_to_dict(call) for call in response.tool_calls],
        "finish_reason": response.finish_reason,
        "usage": _usage_to_dict(response.usage),
        "diagnostics": _diagnostics_to_dict(response.diagnostics),
    }


def response_from_dict(data: dict[str, Any]) -> ChatResponse:
    """从 dict 还原 ``ChatResponse``；``raw`` 回放时固定为 None。"""

    return ChatResponse(
        provider=data["provider"],
        model=data["model"],
        content=data["content"],
        tool_calls=[_tool_call_from_dict(item) for item in data["tool_calls"]],
        finish_reason=data["finish_reason"],
        usage=_usage_from_dict(data["usage"]),
        diagnostics=_diagnostics_from_dict(data["diagnostics"]),
        raw=None,
    )


def event_to_dict(event: ChatStreamEvent) -> dict[str, Any]:
    """把 ``ChatStreamEvent`` 序列化成 dict。"""

    return {
        "kind": event.kind,
        "text": event.text,
        "tool_call": _tool_call_to_dict(event.tool_call) if event.tool_call is not None else None,
        "tool_call_index": event.tool_call_index,
        "tool_call_id": event.tool_call_id,
        "tool_name": event.tool_name,
        "arguments_delta": event.arguments_delta,
        "response": response_to_dict(event.response) if event.response is not None else None,
        "diagnostics": _diagnostics_to_dict(event.diagnostics),
    }


def event_from_dict(data: dict[str, Any]) -> ChatStreamEvent:
    """从 dict 还原 ``ChatStreamEvent``，与 ``event_to_dict`` 互逆。"""

    return ChatStreamEvent(
        kind=data["kind"],
        text=data["text"],
        tool_call=_tool_call_from_dict(data["tool_call"]) if data["tool_call"] is not None else None,
        tool_call_index=data["tool_call_index"],
        tool_call_id=data["tool_call_id"],
        tool_name=data["tool_name"],
        arguments_delta=data["arguments_delta"],
        response=response_from_dict(data["response"]) if data["response"] is not None else None,
        diagnostics=_diagnostics_from_dict(data["diagnostics"]),
    )


class CassetteProvider(ChatProvider):
    """``ChatProvider`` 装饰器：录制或回放 LLM 交互。

    - record：委托 inner，把每次交互追加进 cassette，``close()`` 时脱敏落盘。
    - replay：按"认领第一条未消费的匹配交互"语义回放；请求不匹配抛
      ``CassetteMismatchError`` 并附字段级 diff；``close()`` 时仍有未消费交互抛
      ``CassetteUnusedError``。replay 允许 ``inner=None``。
    - auto：cassette 文件存在则 replay，否则 record；``CI=true`` 时退化为 replay。
    - off：纯透传 inner（要求 inner 非空）。

    ``acomplete`` 沿用基类默认实现（``asyncio.to_thread(complete)``），自动生效。
    """

    def __init__(
        self,
        inner: ChatProvider | None,
        path: Path | str,
        mode: CassetteMode | None = None,
    ) -> None:
        self._inner = inner
        self._path = Path(path)
        self._mode = self._resolve_mode(mode)
        self._interactions: list[_Interaction] = []
        self._metadata: dict[str, Any] = {}
        self._recorded = False
        self._closed = False
        if self._mode == "replay":
            self._interactions = self._load()
        elif self._mode == "record" and inner is None:
            raise CassetteError("record 模式需要 inner provider")
        elif self._mode == "off" and inner is None:
            raise CassetteError("off 模式需要 inner provider")

    @property
    def name(self) -> str:
        if self._inner is not None:
            return self._inner.name
        return self._metadata.get("provider", "cassette")

    @property
    def model(self) -> str:
        if self._inner is not None:
            return self._inner.model
        return self._metadata.get("model", "cassette-model")

    @property
    def capabilities(self):
        if self._inner is not None and hasattr(self._inner, "capabilities"):
            return self._inner.capabilities
        raise AttributeError("replay 模式没有 inner provider，capabilities 不可用")

    def complete(self, request: ChatRequest) -> ChatResponse:
        if self._mode == "replay":
            interaction = self._claim("complete", request)
            assert interaction.response is not None
            return response_from_dict(interaction.response)
        if self._mode == "record":
            assert self._inner is not None
            response = self._inner.complete(request)
            self._interactions.append(
                _Interaction(kind="complete", request=request_to_dict(request), response=response_to_dict(response))
            )
            self._recorded = True
            return response
        assert self._inner is not None
        return self._inner.complete(request)

    def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        if self._mode == "replay":
            interaction = self._claim("stream", request)
            events = [event_from_dict(item) for item in interaction.events]

            async def replay_stream() -> AsyncIterator[ChatStreamEvent]:
                for event in events:
                    yield event

            return replay_stream()
        if self._mode == "record":
            assert self._inner is not None
            inner_stream = self._inner.astream(request)
            collected: list[ChatStreamEvent] = []

            async def record_stream() -> AsyncIterator[ChatStreamEvent]:
                async for event in inner_stream:
                    collected.append(event)
                    yield event
                # 完整消费后才落一条交互, 避免半截流污染 cassette
                self._interactions.append(
                    _Interaction(
                        kind="stream",
                        request=request_to_dict(request),
                        events=[event_to_dict(event) for event in collected],
                    )
                )
                self._recorded = True

            return record_stream()
        assert self._inner is not None
        return self._inner.astream(request)

    def save(self) -> None:
        """record 模式下落盘 cassette；没有录制任何交互或 replay/off 模式是 no-op。"""

        if self._mode != "record" or not self._recorded:
            return
        payload = {
            "version": CASSETTE_VERSION,
            "metadata": {
                "provider": self._inner.name if self._inner is not None else "cassette",
                "model": self._inner.model if self._inner is not None else "cassette-model",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            },
            "interactions": [
                {
                    "kind": interaction.kind,
                    "request": interaction.request,
                    **({"response": interaction.response} if interaction.kind == "complete" else {"events": interaction.events}),
                }
                for interaction in self._interactions
            ],
        }
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        # 纵深防御脱敏: secret/绝对路径/PII 在离开工作区前替换; 提交前仍需人工 review
        text = redact_text(text, RedactionOptions(pii=PiiOptions()))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(text + "\n", encoding="utf-8")

    def close(self) -> None:
        """结束会话：record 落盘；replay 检查未消费交互。幂等。"""

        if self._closed:
            return
        self._closed = True
        if self._mode == "record":
            self.save()
            return
        if self._mode == "replay":
            unused = [interaction for interaction in self._interactions if not interaction.consumed]
            if unused:
                raise CassetteUnusedError(f"cassette {self._path} 还有 {len(unused)} 条未消费的交互")

    def __enter__(self) -> CassetteProvider:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def _resolve_mode(self, mode: CassetteMode | None) -> Literal["record", "replay", "off"]:
        requested = mode if mode is not None else os.environ.get(CASSETTE_MODE_ENV, "auto")
        if requested not in _VALID_MODES:
            raise CassetteError(f"非法 cassette 模式 {requested!r}，可选: {', '.join(_VALID_MODES)}")
        if requested in ("record", "replay", "off"):
            return requested
        # auto: CI 上退化为 replay, 缺失即失败, 避免 CI 悄悄触网录制
        if _is_ci():
            return "replay"
        return "replay" if self._path.exists() else "record"

    def _load(self) -> list[_Interaction]:
        if not self._path.exists():
            raise CassetteMissingError(
                f"cassette 不存在: {self._path}（CI 或 replay 模式下请先用 {CASSETTE_MODE_ENV}=record 本地录制并提交）"
            )
        payload = json.loads(self._path.read_text(encoding="utf-8"))
        version = payload.get("version")
        if version != CASSETTE_VERSION:
            raise CassetteError(f"不支持的 cassette 版本 {version!r}，当前支持 {CASSETTE_VERSION}")
        self._metadata = dict(payload.get("metadata") or {})
        return [
            _Interaction(
                kind=item["kind"],
                request=item["request"],
                response=item.get("response"),
                events=item.get("events") or [],
            )
            for item in payload.get("interactions", [])
        ]

    def _claim(self, kind: Literal["complete", "stream"], request: ChatRequest) -> _Interaction:
        """认领第一条未消费且请求匹配的交互；不匹配时给出字段级 diff。"""

        canonical = _canonical_request(request)
        candidates = [item for item in self._interactions if not item.consumed and item.kind == kind]
        for interaction in candidates:
            if json.dumps(interaction.request, ensure_ascii=False, sort_keys=True) == canonical:
                interaction.consumed = True
                return interaction
        diff = _diff_request(candidates[0].request, request_to_dict(request)) if candidates else "（cassette 中已无剩余同类交互）"
        raise CassetteMismatchError(f"回放请求与 cassette {self._path} 中的 {kind} 交互不匹配:\n{diff}")


def _is_ci() -> bool:
    return os.environ.get("CI", "").lower() in {"true", "1", "yes"}


def _canonical_request(request: ChatRequest) -> str:
    """请求规范化：key 排序后的 JSON，作为回放匹配的指纹。"""

    return json.dumps(request_to_dict(request), ensure_ascii=False, sort_keys=True)


def _diff_request(expected: dict[str, Any], actual: dict[str, Any]) -> str:
    """生成请求顶层字段级 diff，帮助定位是消息、工具还是参数不一致。"""

    lines: list[str] = []
    for key in sorted(set(expected) | set(actual)):
        before = expected.get(key, "<缺失>")
        after = actual.get(key, "<缺失>")
        if before != after:
            lines.append(f"  {key}:\n    cassette: {dumps_json(before) if before != '<缺失>' else before}\n    实际请求: {dumps_json(after) if after != '<缺失>' else after}")
    return "\n".join(lines) if lines else "  （顶层字段一致，可能是交互顺序变化）"


def _message_to_dict(message: ChatMessage) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "content_parts": [_content_part_to_dict(part) for part in message.content_parts] if message.content_parts is not None else None,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": [_tool_call_to_dict(call) for call in message.tool_calls],
    }


def _message_from_dict(data: dict[str, Any]) -> ChatMessage:
    return ChatMessage(
        role=data["role"],
        content=data["content"],
        content_parts=[_content_part_from_dict(item) for item in data["content_parts"]] if data["content_parts"] is not None else None,
        name=data["name"],
        tool_call_id=data["tool_call_id"],
        tool_calls=[_tool_call_from_dict(item) for item in data["tool_calls"]],
    )


def _content_part_to_dict(part: ContentPart) -> dict[str, Any]:
    # base64 图片等内容原样序列化, 不做额外处理
    return {
        "type": part.type,
        "text": part.text,
        "media_type": part.media_type,
        "data_base64": part.data_base64,
        "filename": part.filename,
    }


def _content_part_from_dict(data: dict[str, Any]) -> ContentPart:
    return ContentPart(
        type=data["type"],
        text=data["text"],
        media_type=data["media_type"],
        data_base64=data["data_base64"],
        filename=data["filename"],
    )


def _tool_call_to_dict(call: ToolCall) -> dict[str, Any]:
    return {"id": call.id, "name": call.name, "arguments": call.arguments}


def _tool_call_from_dict(data: dict[str, Any]) -> ToolCall:
    return ToolCall(id=data["id"], name=data["name"], arguments=data["arguments"])


def _tool_definition_to_dict(tool: ToolDefinition) -> dict[str, Any]:
    return {"name": tool.name, "description": tool.description, "parameters": tool.parameters}


def _tool_definition_from_dict(data: dict[str, Any]) -> ToolDefinition:
    return ToolDefinition(name=data["name"], description=data["description"], parameters=data["parameters"])


def _tool_choice_to_dict(tool_choice: ToolChoice | None) -> Any:
    if isinstance(tool_choice, ToolChoiceFunction):
        return {"type": "function", "name": tool_choice.name}
    return tool_choice


def _tool_choice_from_dict(data: Any) -> ToolChoice | None:
    if isinstance(data, dict) and data.get("type") == "function":
        return ToolChoiceFunction(name=data["name"])
    return data


def _usage_to_dict(usage: TokenUsage | None) -> dict[str, Any] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "total_tokens": usage.total_tokens,
    }


def _usage_from_dict(data: dict[str, Any] | None) -> TokenUsage | None:
    if data is None:
        return None
    return TokenUsage(
        input_tokens=data["input_tokens"],
        output_tokens=data["output_tokens"],
        total_tokens=data["total_tokens"],
    )


def _diagnostics_to_dict(diagnostics: ProviderDiagnostics) -> dict[str, Any]:
    return {
        "reasoning": diagnostics.reasoning,
        "raw_finish_reason": diagnostics.raw_finish_reason,
        "warnings": list(diagnostics.warnings),
    }


def _diagnostics_from_dict(data: dict[str, Any]) -> ProviderDiagnostics:
    return ProviderDiagnostics(
        reasoning=data["reasoning"],
        raw_finish_reason=data["raw_finish_reason"],
        warnings=list(data["warnings"]),
    )


__all__ = [
    "CASSETTE_MODE_ENV",
    "CASSETTE_VERSION",
    "CassetteError",
    "CassetteMismatchError",
    "CassetteMissingError",
    "CassetteMode",
    "CassetteNameError",
    "CassetteProvider",
    "CassetteUnusedError",
    "event_from_dict",
    "event_to_dict",
    "request_from_dict",
    "request_to_dict",
    "resolve_cassette_path",
    "response_from_dict",
    "response_to_dict",
]
