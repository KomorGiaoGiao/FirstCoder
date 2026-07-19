"""Shared helpers for adapting synchronous provider streams to async consumers."""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from collections.abc import Callable, Mapping
from typing import Any

from firstcoder.providers.types import ProviderDiagnostics, TokenUsage, ToolCall
from firstcoder.utils.json_utils import loads_json_object


def read_field(value: Any, name: str, default: Any = None) -> Any:
    """Read a field from either an SDK object or a test-friendly dictionary."""

    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


@dataclass(frozen=True, slots=True)
class StreamFailure:
    error: BaseException


@dataclass(slots=True)
class StreamToolCallAccumulator:
    index: int
    id: str = ""
    name: str = ""
    arguments_text: str = ""
    saw_arguments: bool = False


STREAM_ENDED = object()


def token_usage(
    input_tokens: int | None,
    output_tokens: int | None,
    total_tokens: int | None = None,
) -> TokenUsage | None:
    if total_tokens is None and input_tokens is not None and output_tokens is not None:
        total_tokens = int(input_tokens) + int(output_tokens)
    if input_tokens is None and output_tokens is None and total_tokens is None:
        return None
    return TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens, total_tokens=total_tokens)


def merge_usage(left: TokenUsage | None, right: TokenUsage | None) -> TokenUsage | None:
    if left is None or right is None:
        return right or left
    return token_usage(
        right.input_tokens if right.input_tokens is not None else left.input_tokens,
        right.output_tokens if right.output_tokens is not None else left.output_tokens,
        right.total_tokens if right.total_tokens is not None else left.total_tokens,
    )


def complete_stream_tool_calls(
    accumulators: Mapping[int, StreamToolCallAccumulator],
    diagnostics: ProviderDiagnostics,
    *,
    require_identity: bool,
) -> list[ToolCall]:
    parsed: list[ToolCall] = []
    for index in sorted(accumulators):
        item = accumulators[index]
        missing_identity = require_identity and (not item.id or not item.name)
        if missing_identity or not item.saw_arguments:
            missing = "id、name 或 arguments" if require_identity else "arguments"
            diagnostics.warnings.append(
                f"streaming tool_call 缺少 {missing}，已丢弃整组不可执行调用："
                f"index={index}, id={item.id}, name={item.name}"
            )
            return []
        arguments = loads_json_object(item.arguments_text)
        if not isinstance(arguments, dict):
            diagnostics.warnings.append(
                f"streaming tool_call 参数不是合法 JSON object，已丢弃整组不可执行调用："
                f"index={index}, id={item.id}, name={item.name}"
            )
            return []
        parsed.append(ToolCall(id=item.id, name=item.name, arguments=arguments))
    return parsed


def close_stream(stream: Any) -> None:
    close = getattr(stream, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            # Closing a cancelled stream is best effort; it must not prevent
            # the worker from publishing its terminal sentinel.
            pass


def start_sync_stream_worker(
    stream: Any,
    *,
    thread_name: str,
) -> tuple[queue.Queue[Any], Callable[[], None]]:
    """Read one synchronous iterator in a dedicated thread and expose a queue."""

    stream_queue: queue.Queue[Any] = queue.Queue()
    stop_event = threading.Event()
    close_lock = threading.Lock()
    stream_closed = False

    def stop() -> None:
        nonlocal stream_closed

        stop_event.set()
        with close_lock:
            if stream_closed:
                return
            close_stream(stream)
            stream_closed = True

    def worker() -> None:
        try:
            for item in stream:
                if stop_event.is_set():
                    break
                stream_queue.put(item)
        except BaseException as exc:
            stream_queue.put(StreamFailure(exc))
        finally:
            stop()
            stream_queue.put(STREAM_ENDED)

    threading.Thread(target=worker, name=thread_name, daemon=True).start()
    return stream_queue, stop
