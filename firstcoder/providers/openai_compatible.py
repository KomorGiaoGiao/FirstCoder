"""OpenAI-compatible provider 实现。"""

from __future__ import annotations

import asyncio
import queue
import threading
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

from firstcoder.utils.json_utils import dumps_json, loads_json_object
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import (
    ProviderError,
    ProviderErrorKind,
    classify_provider_error,
    classify_provider_exception,
)
from firstcoder.providers.tool_adapters import to_openai_tool
from firstcoder.providers.types import (
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ChatStreamEvent,
    FinishReason,
    ProviderCapabilities,
    ProviderDiagnostics,
    TokenUsage,
    ToolChoice,
    ToolChoiceFunction,
    ToolCall,
)


def _read_field(value: Any, name: str, default: Any = None) -> Any:
    """同时兼容 SDK 对象和普通 dict 的字段读取。

    OpenAI SDK 返回的是带属性访问能力的对象；测试里常常用 dict 或简单假对象。
    统一通过这个函数读取字段，可以让解析逻辑更容易测试。
    """

    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _read_reasoning_delta(delta: Any) -> str:
    """读取 OpenAI-compatible 厂商常见的 reasoning 增量字段。"""

    for name in ("reasoning_content", "reasoning"):
        value = _read_field(delta, name)
        if isinstance(value, str):
            return value
        if value is not None:
            for nested_name in ("delta", "content", "text"):
                nested = _read_field(value, nested_name)
                if isinstance(nested, str):
                    return nested
    return ""


class OpenAICompatibleProvider(ChatProvider):
    """使用 OpenAI Chat Completions 协议的 provider。

    OpenAI、DeepSeek、Qwen、Moonshot、Zhipu、OpenRouter、Ollama 等都可以通过
    `base_url + api_key + model` 的方式接入这一层。不同厂商的高级参数可以通过
    `ChatRequest.extra_body` 继续透传。
    """

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: str,
        base_url: str | None = None,
        capabilities: ProviderCapabilities | None = None,
        extra_headers: dict[str, str] | None = None,
        extra_body: dict[str, Any] | None = None,
        client: Any | None = None,
    ) -> None:
        self._name = name
        self._model = model
        self._base_url = base_url
        self._capabilities = capabilities or ProviderCapabilities(supports_streaming=True)
        self._extra_headers = dict(extra_headers or {})
        self._extra_body = dict(extra_body or {})

        # 允许测试或上层代码注入 client；没有注入时才创建真实 SDK client。
        if client is not None:
            self._client = client
        else:
            from openai import OpenAI

            kwargs: dict[str, Any] = {"api_key": api_key}
            if base_url:
                kwargs["base_url"] = base_url
            if extra_headers:
                kwargs["default_headers"] = extra_headers
            self._client = OpenAI(**kwargs)

    @property
    def name(self) -> str:
        return self._name

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> ProviderCapabilities:
        return self._capabilities

    @property
    def base_url(self) -> str | None:
        return self._base_url

    @property
    def extra_headers(self) -> dict[str, str]:
        return dict(self._extra_headers)

    @property
    def extra_body(self) -> dict[str, Any]:
        return dict(self._extra_body)

    def complete(self, request: ChatRequest) -> ChatResponse:
        """调用 Chat Completions，并转换成项目内部统一响应。"""

        params = self._build_completion_params(request)
        try:
            response = self._client.chat.completions.create(**params)
        except Exception as exc:
            message = str(exc)
            raise ProviderError(classify_provider_exception(exc), message) from exc
        choice = _read_field(response, "choices", [])[0]
        message = _read_field(choice, "message")
        raw_finish_reason = _read_field(choice, "finish_reason")
        finish_reason = _normalize_finish_reason(raw_finish_reason)
        diagnostics = ProviderDiagnostics(raw_finish_reason=raw_finish_reason)
        diagnostics.reasoning = _read_reasoning_delta(message) or None
        tool_calls = self._parse_tool_calls(_read_field(message, "tool_calls", []) or [], diagnostics=diagnostics)
        if finish_reason == "length" and tool_calls:
            # length 表示模型输出被截断。此时即使 SDK 对象里出现了 tool_calls，也可能只是
            # 半截 JSON 参数；为了避免执行危险的半截工具调用，整组丢弃并写 diagnostics。
            diagnostics.warnings.append("finish_reason=length，丢弃可能不完整的 tool_calls，避免执行半截工具调用。")
            tool_calls = []

        return ChatResponse(
            provider=self._name,
            model=_read_field(response, "model", self._model),
            content=_read_field(message, "content", "") or "",
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            usage=_parse_usage(_read_field(response, "usage")),
            diagnostics=diagnostics,
            raw=response,
        )

    async def astream(self, request: ChatRequest) -> AsyncIterator[ChatStreamEvent]:
        """调用 Chat Completions streaming，并转换成内部流式事件。

        OpenAI-compatible 原始 chunk 只在这里解析。工具调用 delta 会先按 index
        累积，直到 stream 完成后才产出完整 `tool_call_completed` 事件。
        """

        if not self._capabilities.supports_streaming:
            raise ProviderError(
                ProviderErrorKind.UNSUPPORTED,
                f"provider {self._name} 不支持 streaming",
            )

        params = self._build_completion_params(request)
        params["stream"] = True
        diagnostics = ProviderDiagnostics()
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_accumulators: dict[int, _StreamToolCallAccumulator] = {}
        raw_finish_reason: Any = None
        response_model = self._model

        try:
            stream = await asyncio.to_thread(self._client.chat.completions.create, **params)
        except Exception as exc:
            message = str(exc)
            raise ProviderError(classify_provider_exception(exc), message) from exc

        yield ChatStreamEvent(kind="message_started")

        # OpenAI Python SDK 的 stream iterator 是同步对象；Agent/UI 这边是 async 消费。
        # 用后台线程顺序读取 chunk，再通过 Queue 桥接到 async loop，避免阻塞 Textual。
        stream_queue, stop_stream = _start_stream_worker(stream)
        try:
            while True:
                item = await asyncio.to_thread(stream_queue.get)
                if item is _STREAM_ENDED:
                    break
                if isinstance(item, _StreamFailure):
                    message = str(item.error)
                    raise ProviderError(classify_provider_exception(item.error), message) from item.error

                chunk = item
                stream_error = _parse_stream_error(chunk)
                if stream_error is not None:
                    diagnostics.warnings.append(stream_error.message)
                    yield ChatStreamEvent(kind="error", diagnostics=diagnostics)
                    raise stream_error

                response_model = _read_field(chunk, "model", response_model) or response_model
                choices = _read_field(chunk, "choices", []) or []
                if not choices:
                    continue

                choice = choices[0]
                delta = _read_field(choice, "delta", {}) or {}
                choice_finish_reason = _read_field(choice, "finish_reason")
                if choice_finish_reason is not None:
                    raw_finish_reason = choice_finish_reason

                text = _read_field(delta, "content")
                if text:
                    content_parts.append(text)
                    yield ChatStreamEvent(kind="text_delta", text=text)

                reasoning = _read_reasoning_delta(delta)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    yield ChatStreamEvent(kind="reasoning_delta", text=reasoning)

                # tool_calls 在 streaming 中不是一次性完整返回，而是按 index 分片到达。
                # 这里只累计和展示 delta，不解析执行；真正执行要等 finish_reason=tool_calls。
                for event in _accumulate_stream_tool_call_deltas(
                    _read_field(delta, "tool_calls", []) or [],
                    accumulators=tool_accumulators,
                    diagnostics=diagnostics,
                ):
                    yield event
        finally:
            stop_stream.set()
            await asyncio.to_thread(_close_stream, stream)

        finish_reason = _normalize_finish_reason(raw_finish_reason)
        diagnostics.raw_finish_reason = raw_finish_reason
        if reasoning_parts:
            diagnostics.reasoning = "".join(reasoning_parts)

        tool_calls: list[ToolCall] = []
        if tool_accumulators and finish_reason != "tool_calls":
            # 如果 stream 结束原因不是 tool_calls，说明这些工具片段没有完整结束语义。
            # 保守做法是不给 agent 任何可执行 tool_call。
            diagnostics.warnings.append(
                f"finish_reason={finish_reason}，丢弃 streaming 中未以 tool_calls 完成的 tool_calls。"
            )
        elif tool_accumulators:
            tool_calls = _complete_stream_tool_calls(tool_accumulators, diagnostics=diagnostics)
        if diagnostics.warnings and tool_accumulators and not tool_calls:
            yield ChatStreamEvent(kind="error", diagnostics=diagnostics)

        for tool_call in tool_calls:
            yield ChatStreamEvent(
                kind="tool_call_completed",
                tool_call=tool_call,
                tool_call_id=tool_call.id,
                tool_name=tool_call.name,
            )

        response = ChatResponse(
            provider=self._name,
            model=response_model,
            content="".join(content_parts),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            diagnostics=diagnostics,
        )
        yield ChatStreamEvent(kind="message_completed", response=response, diagnostics=diagnostics)

    def _build_completion_params(self, request: ChatRequest) -> dict[str, Any]:
        """构造 OpenAI-compatible Chat Completions 请求参数。"""

        if request.tools and not self._capabilities.supports_tools:
            # 这里显式报错，而不是静默忽略 tools。否则 agent 以为模型看到了工具 schema，
            # 实际请求却没有工具能力，后续行为会很难排查。
            raise ProviderError(
                ProviderErrorKind.CONFIG_ERROR,
                f"provider {self._name} 不支持 tool calling，不能发送 tools",
            )

        params: dict[str, Any] = {
            "model": self._model,
            "messages": [self._to_openai_message(message) for message in request.messages],
        }

        if request.tools:
            params["tools"] = [to_openai_tool(tool) for tool in request.tools]
            params["tool_choice"] = _to_openai_tool_choice(request.tool_choice)
            if self._capabilities.supports_parallel_tool_calls:
                # 只有 preset 明确声明支持时才发送 parallel_tool_calls，兼容一些中转站
                # 对 OpenAI 新字段支持不完整的情况。
                params["parallel_tool_calls"] = True
        if request.temperature is not None:
            params["temperature"] = request.temperature
        if request.max_tokens is not None:
            params[self._capabilities.token_param] = request.max_tokens

        extra_body = {**self._extra_body, **request.extra_body}
        if extra_body:
            # 不同 OpenAI-compatible 厂商常把私有参数放到 extra_body。provider 层统一透传，
            # agent loop 不需要为每个厂商写分支。
            params["extra_body"] = extra_body
        return params

    @staticmethod
    def _to_openai_message(message: ChatMessage) -> dict[str, Any]:
        """把内部消息转换为 OpenAI-compatible 消息。"""

        data: dict[str, Any] = {
            "role": message.role,
            "content": message.content,
        }
        if message.tool_calls:
            data["tool_calls"] = [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {
                        "name": tool_call.name,
                        "arguments": (
                            tool_call.arguments
                            if isinstance(tool_call.arguments, str)
                            else dumps_json(tool_call.arguments)
                        ),
                    },
                }
                for tool_call in message.tool_calls
            ]
        if message.name:
            data["name"] = message.name
        if message.tool_call_id:
            data["tool_call_id"] = message.tool_call_id
        return data

    @staticmethod
    def _parse_tool_calls(tool_calls: list[Any], *, diagnostics: ProviderDiagnostics) -> list[ToolCall]:
        """解析 OpenAI-compatible 返回的 tool_calls。"""

        parsed: list[ToolCall] = []
        for call in tool_calls:
            function = _read_field(call, "function", {})
            raw_arguments = _read_field(function, "arguments", "")
            arguments = loads_json_object(raw_arguments)
            if not isinstance(arguments, dict):
                # OpenAI function calling 约定 arguments 是 JSON object 字符串。参数坏了时不尝试
                # “修复”或执行其中一部分，因为工具调用一旦有副作用就不能靠猜。
                call_id = _read_field(call, "id", "")
                name = _read_field(function, "name", "")
                diagnostics.warnings.append(
                    f"tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用：id={call_id}, name={name}"
                )
                return []

            parsed.append(
                ToolCall(
                    id=_read_field(call, "id", ""),
                    name=_read_field(function, "name", ""),
                    arguments=arguments,
                )
            )
        return parsed


def _normalize_finish_reason(reason: Any) -> FinishReason:
    """把 OpenAI-compatible finish_reason 收敛成内部受控值。"""

    if reason in {"stop", "tool_calls", "length", "content_filter"}:
        return reason
    if reason is None:
        return "unknown"
    return "unknown"


def _parse_usage(usage: Any) -> TokenUsage | None:
    """解析 OpenAI-compatible usage 字段，缺字段时保留 None。"""

    if usage is None:
        return None
    input_tokens = _read_field(usage, "prompt_tokens")
    output_tokens = _read_field(usage, "completion_tokens")
    total_tokens = _read_field(usage, "total_tokens")
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def _to_openai_tool_choice(tool_choice: ToolChoice | None) -> str | dict[str, Any] | None:
    """把内部 tool_choice 转成 OpenAI function calling wire format。"""

    if tool_choice is None:
        return None
    if isinstance(tool_choice, ToolChoiceFunction):
        return {
            "type": "function",
            "function": {"name": tool_choice.name},
        }
    if isinstance(tool_choice, str) and tool_choice in {"auto", "none", "required"}:
        return tool_choice
    raise ProviderError(ProviderErrorKind.CONFIG_ERROR, f"不支持的 tool_choice：{tool_choice!r}")


@dataclass(slots=True)
class _StreamToolCallAccumulator:
    index: int
    id: str = ""
    name: str = ""
    arguments_text: str = ""
    saw_arguments: bool = False


@dataclass(frozen=True, slots=True)
class _StreamFailure:
    error: BaseException


_STREAM_ENDED = object()


def _start_stream_worker(stream: Any) -> tuple[queue.Queue[Any], threading.Event]:
    """用单一后台线程拥有同步 stream iterator。

    OpenAI SDK 的同步 streaming iterator 可能持有底层 HTTP 连接。让同一个线程顺序
    消费它，可以避免每次 `next()` 被调度到不同线程造成的隐性连接生命周期问题。
    """

    stream_queue: queue.Queue[Any] = queue.Queue()
    stop_event = threading.Event()

    def worker() -> None:
        try:
            for chunk in stream:
                if stop_event.is_set():
                    break
                stream_queue.put(chunk)
        except BaseException as exc:
            stream_queue.put(_StreamFailure(exc))
        finally:
            _close_stream(stream)
            stream_queue.put(_STREAM_ENDED)

    threading.Thread(target=worker, name="firstcoder-openai-stream", daemon=True).start()
    return stream_queue, stop_event


def _close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        close()


def _parse_stream_error(chunk: Any) -> ProviderError | None:
    error = _read_field(chunk, "error")
    if error is None:
        return None

    message = _read_field(error, "message") or str(error)
    status_code = _read_field(error, "status_code")
    return ProviderError(classify_provider_error(message, status_code=status_code), message)


def _accumulate_stream_tool_call_deltas(
    tool_call_deltas: list[Any],
    *,
    accumulators: dict[int, _StreamToolCallAccumulator],
    diagnostics: ProviderDiagnostics,
) -> list[ChatStreamEvent]:
    events: list[ChatStreamEvent] = []
    for delta in tool_call_deltas:
        index = _read_field(delta, "index")
        if not isinstance(index, int):
            diagnostics.warnings.append("streaming tool_call delta 缺少 index，已忽略该片段。")
            continue

        is_new = index not in accumulators
        accumulator = accumulators.setdefault(index, _StreamToolCallAccumulator(index=index))
        # 同一个 tool_call 的 id/name/arguments 可能分散在多个 chunk。按 index 聚合能支持
        # 一个 assistant message 中同时出现多个并行工具调用。
        call_id = _read_field(delta, "id")
        if call_id:
            accumulator.id = call_id

        function = _read_field(delta, "function", {}) or {}
        name_delta = _read_field(function, "name", "") or ""
        arguments_delta = _read_field(function, "arguments", "") or ""
        if name_delta:
            accumulator.name += name_delta
        if arguments_delta:
            accumulator.arguments_text += arguments_delta
            accumulator.saw_arguments = True

        if is_new:
            events.append(
                ChatStreamEvent(
                    kind="tool_call_started",
                    tool_call_index=index,
                    tool_call_id=accumulator.id or None,
                    tool_name=accumulator.name or None,
                )
            )
        events.append(
            ChatStreamEvent(
                kind="tool_call_delta",
                tool_call_index=index,
                tool_call_id=accumulator.id or None,
                tool_name=accumulator.name or None,
                arguments_delta=arguments_delta,
            )
        )
    return events


def _complete_stream_tool_calls(
    accumulators: dict[int, _StreamToolCallAccumulator],
    *,
    diagnostics: ProviderDiagnostics,
) -> list[ToolCall]:
    parsed: list[ToolCall] = []
    for index in sorted(accumulators):
        accumulator = accumulators[index]
        if not accumulator.id or not accumulator.name or not accumulator.saw_arguments:
            # 缺任一关键字段都不能执行。返回空列表表示整组 streaming tool_calls 作废，
            # 避免只执行部分工具造成模型上下文和真实副作用不一致。
            diagnostics.warnings.append(
                f"streaming tool_call 缺少 id、name 或 arguments，已丢弃整组不可执行调用：index={index}"
            )
            return []

        arguments = loads_json_object(accumulator.arguments_text)
        if not isinstance(arguments, dict):
            diagnostics.warnings.append(
                f"streaming tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用："
                f"index={index}, id={accumulator.id}, name={accumulator.name}"
            )
            return []

        parsed.append(
            ToolCall(
                id=accumulator.id,
                name=accumulator.name,
                arguments=arguments,
            )
        )
    return parsed
