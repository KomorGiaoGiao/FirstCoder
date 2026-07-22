"""用 provider 实现 L4 checkpoint 摘要的适配器。"""

from __future__ import annotations

import re

from firstcoder.context.llm_compact import (
    CODING_HANDOFF_HEADINGS,
    CompactTimeoutError,
    LlmCompactSummarizer,
    LlmCompactSummary,
    NoSummaryError,
    PromptTooLongError,
    normalize_coding_handoff,
)
from firstcoder.context.models import AgentMessage
from firstcoder.context.tool_sequence import InvalidToolCallSequenceError, validate_tool_call_sequence
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError, ProviderErrorKind
from firstcoder.providers.types import ChatMessage, ChatRequest


class ProviderLlmCompactSummarizer(LlmCompactSummarizer):
    """把上下文层的 L4 summarizer 协议适配到通用 `ChatProvider`。

    L4 checkpoint 需要三个事实：摘要正文、被摘要覆盖到哪里、从哪里开始保留 tail。
    默认实现只让模型生成摘要正文，边界由本地根据当前消息序列选择并继续交给
    `LlmCompactService` 校验，避免把恢复边界完全交给模型决定。
    """

    def __init__(self, provider: ChatProvider, *, max_tokens: int = 1200) -> None:
        self.provider = provider
        self.max_tokens = max_tokens

    def summarize(self, messages: list[AgentMessage], *, summary_mode: str = "default") -> LlmCompactSummary:
        tail = _tail_boundary(messages)
        prompt = _build_summary_prompt(messages, summary_mode=summary_mode)
        try:
            response = self.provider.complete(
                ChatRequest(
                    messages=[
                        ChatMessage(
                            role="system",
                            content=(
                                "你是 FirstCoder 的上下文压缩器。输出简洁的 coding handoff；"
                                "必须且只能使用指定的七个 Markdown 标题，每个恰好一次；"
                                "只在标题下写有证据支持的事实。不要选择 checkpoint 边界。"
                            ),
                        ),
                        ChatMessage(role="user", content=prompt),
                    ],
                    tools=[],
                    tool_choice="none",
                    max_tokens=self.max_tokens,
                )
            )
        except ProviderError as error:
            if error.kind == ProviderErrorKind.PROMPT_TOO_LONG:
                raise PromptTooLongError(str(error)) from error
            if error.kind == ProviderErrorKind.TIMEOUT:
                raise CompactTimeoutError(str(error)) from error
            raise NoSummaryError(str(error)) from error
        summary = response.content.strip()
        if not summary:
            raise NoSummaryError("empty summary")
        return LlmCompactSummary(
            summary=normalize_coding_handoff(summary),
            tail_start_message_id=tail.tail_start_message_id,
            covered_until_message_id=tail.covered_until_message_id,
        )


class _TailBoundary:
    def __init__(self, *, tail_start_message_id: str, covered_until_message_id: str) -> None:
        self.tail_start_message_id = tail_start_message_id
        self.covered_until_message_id = covered_until_message_id


def _tail_boundary(messages: list[AgentMessage]) -> _TailBoundary:
    """选择一个保守 tail：尽量保留少量尾部，同时保证 tool 序列合法。"""

    candidates = _boundary_candidates(messages)
    if len(candidates) < 2:
        raise NoSummaryError("not enough messages to summarize")
    for index in range(len(candidates) - 1, 0, -1):
        try:
            validate_tool_call_sequence(candidates[index:])
        except InvalidToolCallSequenceError:
            continue
        return _TailBoundary(
            tail_start_message_id=candidates[index].id,
            covered_until_message_id=candidates[index - 1].id,
        )
    raise NoSummaryError("could not find a valid checkpoint tail boundary")


def _boundary_candidates(messages: list[AgentMessage]) -> list[AgentMessage]:
    return [message for message in messages if not any(part.kind == "checkpoint_summary" for part in message.parts)]


def _build_summary_prompt(messages: list[AgentMessage], *, summary_mode: str) -> str:
    mode_hint = "更强压缩，优先保留可恢复事实。" if summary_mode == "stronger" else "常规压缩。"
    headings = "\n".join(CODING_HANDOFF_HEADINGS)
    sections = [
        f"摘要模式：{mode_hint}",
        "",
        "只能按以下 coding handoff 格式输出。每个标题必须恰好出现一次；" "若历史中没有某项的证据，写“无”：",
        headings,
        "",
        "需要压缩的会话历史：",
    ]
    for message in messages:
        content = _message_text(message)
        if not content:
            continue
        sections.append(f"\n[{message.id}] role={message.role}\n{content}")
    return "\n".join(sections)


def _message_text(message: AgentMessage) -> str:
    chunks = [part.content for part in message.parts if part.content]
    return _collapse_text("\n".join(chunks))


def _collapse_text(value: str, *, max_chars: int = 4000) -> str:
    collapsed = re.sub(r"\n{3,}", "\n\n", value.strip())
    if len(collapsed) <= max_chars:
        return collapsed
    return f"{collapsed[:max_chars]}\n...[truncated]"
