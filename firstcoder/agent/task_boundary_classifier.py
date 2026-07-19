"""Hidden LLM task-boundary classification for AgentLoop.

This is separate from firstcoder.context.task_boundary, which owns hash/state
transitions after a decision exists.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol

from firstcoder.agent.session import AgentSession
from firstcoder.context.context_builder import ContextBuilder
from firstcoder.context.manager import ContextWindowTrigger
from firstcoder.context.task_boundary import observation_from_tool_result_data
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.errors import ProviderError
from firstcoder.providers.types import ChatMessage, ChatRequest, ChatResponse

CLASSIFICATION_ATTEMPTS = 3
CLASSIFICATION_MAX_TOKENS = 512
CLASSIFICATION_PROMPT = """Classify whether the latest real user message starts a new task relative to the conversation.
Choose "same" when the latest message is a continuation or follow-up of the active task, including messages that say "continue", "add", "explain further", or refer to the immediately preceding task.
Choose "new" when it starts a different goal, subject, deliverable, or problem from the active task.
Use "uncertain" only when the conversation does not provide enough information to distinguish same from new; do not use it merely because a continuation is short.
Example: active task is username normalization; "continue with its acceptance criteria" -> same.
Example: active task is username normalization; "now explain deep_merge rules instead" -> new.
Return exactly one JSON object, with no Markdown or explanation:
{"decision":"same|new|uncertain","basis_message_id":"CURRENT_USER_MESSAGE_ID"}
The basis_message_id must exactly equal the ID attached to the latest user message."""
CLASSIFICATION_RETRY_PROMPT = """The previous classification was invalid. Return exactly one JSON object and nothing else:
{"decision":"same|new|uncertain","basis_message_id":"CURRENT_USER_MESSAGE_ID"}
The basis_message_id must exactly equal the ID attached to the latest user message."""


def parse_task_boundary_classification(content: str, *, basis_message_id: str) -> str | None:
    """接受精确 JSON 分类，拒绝额外文本和错误的消息锚点。"""

    try:
        parsed = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    decision = parsed.get("decision")
    if decision not in {"same", "new", "uncertain"}:
        return None
    if parsed.get("basis_message_id") != basis_message_id:
        return None
    return decision


class TaskBoundaryClassifier:
    """Run hidden classification calls and record results into session state."""

    def __init__(
        self,
        *,
        session: AgentSession,
        provider: ChatProvider,
        context_builder: ContextBuilder,
        compact_if_needed: Callable[..., object],
        check_cancelled: Callable[[], None],
        reserve_provider_call: Callable[[], None],
        check_turn_timeout: Callable[[], None],
        tag_task_boundary_messages: Callable[[dict[str, object]], None],
    ) -> None:
        self.session = session
        self.provider = provider
        self.context_builder = context_builder
        self._compact_if_needed = compact_if_needed
        self._check_cancelled = check_cancelled
        self._reserve_provider_call = reserve_provider_call
        self._check_turn_timeout = check_turn_timeout
        self._tag_task_boundary_messages = tag_task_boundary_messages

    def classify(self, basis_message_id: str) -> None:
        """运行隐藏的 JSON 分类，并把有效结果写入既有边界状态机。"""

        for attempt in range(CLASSIFICATION_ATTEMPTS):
            try:
                response = self._complete(attempt=attempt)
            except ProviderError:
                continue
            decision = parse_task_boundary_classification(response.content, basis_message_id=basis_message_id)
            if decision is not None:
                self.record(decision, basis_message_id)
                return
        self.record("uncertain", basis_message_id)

    async def classify_async(self, basis_message_id: str) -> None:
        """流式主回复前运行隐藏分类，不向 UI 转发其任何事件。"""

        for attempt in range(CLASSIFICATION_ATTEMPTS):
            try:
                request = self.build_request(attempt=attempt)
                self._reserve_provider_call()
                self._check_turn_timeout()
                self._check_cancelled()
                response = await self.provider.acomplete(request)
            except ProviderError:
                continue
            decision = parse_task_boundary_classification(response.content, basis_message_id=basis_message_id)
            if decision is not None:
                self.record(decision, basis_message_id)
                return
        self.record("uncertain", basis_message_id)

    def _complete(self, *, attempt: int) -> ChatResponse:
        request = self.build_request(attempt=attempt)
        self._reserve_provider_call()
        self._check_turn_timeout()
        self._check_cancelled()
        return self.provider.complete(request)

    def build_request(self, *, attempt: int) -> ChatRequest:
        messages = self.context_builder.build_provider_messages(
            self.session.rebuild_view(),
        )
        prompt = CLASSIFICATION_PROMPT if attempt == 0 else CLASSIFICATION_RETRY_PROMPT
        return ChatRequest(
            messages=[ChatMessage(role="system", content=prompt), *messages],
            tools=[],
            tool_choice="none",
            max_tokens=CLASSIFICATION_MAX_TOKENS,
        )

    def record(self, decision: str, basis_message_id: str) -> None:
        result = self.session.tool_registry.execute(
            "task_boundary",
            {"decision": decision, "basis_message_id": basis_message_id},
        )
        observation = observation_from_tool_result_data(result.data) if result.ok else None
        if observation is None:
            return
        self.session.writer.append_task_boundary_observation(observation)
        self._tag_task_boundary_messages(result.data)
        if result.data.get("should_trigger_compaction"):
            self._compact_if_needed(trigger=ContextWindowTrigger.TASK_HASH_CHANGED)
