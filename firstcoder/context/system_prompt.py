"""稳定系统前缀构造与缓存。

系统提示词属于请求配置，不属于普通会话事实。这里把会影响系统前缀的稳定输入集中
计算 fingerprint，后续 agent loop 可以据此复用上一轮前缀，避免普通消息追加导致
系统提示词缓存失效。
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from firstcoder.context.identity import content_fingerprint, stable_json_hash
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.context.versions import SYSTEM_PROMPT_VERSION
from firstcoder.providers.types import ChatMessage


@dataclass(frozen=True, slots=True)
class SystemPromptInputs:
    """生成 stable system prefix 所需的稳定输入。

    这里刻意不包含最近消息、token 统计、checkpoint、task hash 候选等动态状态。
    这些内容属于 conversation projection 或 runtime state，不应该污染系统前缀缓存。
    """

    base_rules: str
    agents_md: str
    provider_name: str
    provider_capabilities: dict[str, Any]
    permission_policy: dict[str, Any]
    skill_protocol: str = ""
    skill_catalog_summary: str = ""
    loaded_skill_context: str = ""
    mode: str = "default"
    prompt_version: str = SYSTEM_PROMPT_VERSION


@dataclass(frozen=True, slots=True)
class PromptPrefixCacheEntry:
    fingerprint: str
    messages: list[ChatMessage]
    token_estimate: int


class SystemPromptBuilder:
    """构造可复用的 system prompt 前缀。"""

    def fingerprint(self, inputs: SystemPromptInputs) -> str:
        value = {
            "prompt_version": inputs.prompt_version,
            "base_rules_hash": content_fingerprint(inputs.base_rules),
            "agents_md_hash": content_fingerprint(inputs.agents_md),
            "skill_protocol_hash": content_fingerprint(inputs.skill_protocol),
            "skill_catalog_summary_hash": content_fingerprint(inputs.skill_catalog_summary),
            "loaded_skill_context_hash": content_fingerprint(inputs.loaded_skill_context),
            "provider_name": inputs.provider_name,
            "provider_capabilities": inputs.provider_capabilities,
            "permission_policy": inputs.permission_policy,
            "mode": inputs.mode,
        }
        return stable_json_hash(value)

    def build(self, inputs: SystemPromptInputs) -> PromptPrefixCacheEntry:
        fingerprint = self.fingerprint(inputs)
        content = "\n\n".join(
            section
            for section in [
                inputs.base_rules.strip(),
                _agent_instructions(),
                _format_section("Project instructions", inputs.agents_md),
                _format_section("Project skill protocol", inputs.skill_protocol),
                _format_section("Available skills", inputs.skill_catalog_summary),
                _format_section("Loaded skills", inputs.loaded_skill_context),
                _format_section("Provider", _format_provider(inputs)),
                _format_section("Permission policy", _format_json(inputs.permission_policy)),
            ]
            if section
        )
        message = ChatMessage(role="system", content=content)
        return PromptPrefixCacheEntry(
            fingerprint=fingerprint,
            messages=[message],
            token_estimate=_estimate_message_tokens(message),
        )


class PromptPrefixCache:
    """第一版只缓存当前会话最近一次 stable prefix。"""

    def __init__(self) -> None:
        self._entry: PromptPrefixCacheEntry | None = None

    def get_or_build(
        self,
        inputs: SystemPromptInputs,
        builder: SystemPromptBuilder | None = None,
    ) -> PromptPrefixCacheEntry:
        builder = builder or SystemPromptBuilder()
        fingerprint = builder.fingerprint(inputs)
        if self._entry is not None and self._entry.fingerprint == fingerprint:
            return self._entry

        self._entry = builder.build(inputs)
        return self._entry

    @property
    def entry(self) -> PromptPrefixCacheEntry | None:
        return self._entry


def _format_section(title: str, content: str) -> str:
    content = content.strip()
    if not content:
        return ""
    return f"{title}:\n{content}"


def _agent_instructions() -> str:
    path = Path(__file__).with_name("prompts") / "agent_instructions.md"
    return path.read_text(encoding="utf-8").strip()

def _format_provider(inputs: SystemPromptInputs) -> str:
    return "\n".join(
        [
            f"name={inputs.provider_name}",
            f"capabilities={_format_json(inputs.provider_capabilities)}",
            f"mode={inputs.mode}",
            f"prompt_version={inputs.prompt_version}",
        ]
    )


def _estimate_message_tokens(message: ChatMessage) -> int:
    return estimate_text_tokens(message.content)


def _format_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
