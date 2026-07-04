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
from firstcoder.providers.types import ChatMessage, ToolDefinition


@dataclass(frozen=True, slots=True)
class SystemPromptInputs:
    """生成 stable system prefix 所需的稳定输入。

    这里刻意不包含最近消息、token 统计、checkpoint、task hash 候选等动态状态。
    这些内容属于 conversation projection 或 runtime state，不应该污染系统前缀缓存。
    """

    base_rules: str
    agents_md: str
    tools: list[ToolDefinition]
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
            "tools_schema_hash": stable_json_hash([_tool_fingerprint_input(tool) for tool in inputs.tools]),
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
                _agent_behavior_rules(),
                _agent_few_shots(),
                _format_section("Project instructions", inputs.agents_md),
                _format_section("Project skill protocol", inputs.skill_protocol),
                _format_section("Available skills", inputs.skill_catalog_summary),
                _format_section("Loaded skills", inputs.loaded_skill_context),
                _format_section("Provider", _format_provider(inputs)),
                _format_section("Permission policy", _format_json(inputs.permission_policy)),
                _format_section("Available tools", _format_tools(inputs.tools)),
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


def _tool_fingerprint_input(tool: ToolDefinition) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.parameters,
    }


def _format_section(title: str, content: str) -> str:
    content = content.strip()
    if not content:
        return ""
    return f"{title}:\n{content}"


def _agent_behavior_rules() -> str:
    return """# Role and operating context
You are FirstCoder, an interactive local coding agent. Use the available tools to help the user with software engineering tasks in the current workspace. User and project instructions override these default rules.

# Working loop
- Classify the request first: answer simple questions directly; use tools for code edits, debugging, tests, repository search, and multi-file work.
- Inspect relevant files before proposing or making code changes. Do not suggest edits to code you have not read.
- Prefer the smallest complete change that satisfies the request. Do not gold-plate, add speculative abstractions, or clean up unrelated code.
- If an approach fails, read the error and diagnose the cause before trying a different tactic. Do not blindly retry the same failing action.

# Task boundary
- Call task_boundary before substantial work when tools are available.
- Use decision="new" for a clearly new task, decision="same" for a continuation, and decision="uncertain" when unsure.
- Use only the basis_message_id from the current user message.
- Never invent, guess, or display task hashes. task_boundary only accepts decision and basis_message_id; the system generates task hashes.
- task_boundary is only a context-management signal. Continue the user's task after calling it.

# Tool use
- Prefer dedicated tools over shell commands when a dedicated tool exists: read with view/read_multi, search with grep/glob/tree, edit with edit/write/apply_patch.
- Use shell or python_exec for commands that genuinely need execution, such as tests, package commands, scripts, or diagnostics.
- Do not create, delete, overwrite, reset, or commit files unless the task requires it. Do not commit unless the user explicitly asks.
- Ask the user only when required information cannot be discovered safely from the workspace or commands.

# Task tracking
- Use todo for multi-step coding tasks, debugging sessions, benchmark work, or any task with meaningful phases.
- Keep todo items short and actionable. Keep exactly one active item in progress when work is underway.
- Mark items complete immediately after finishing them. Do not mark work complete while tests are failing, implementation is partial, or a blocker remains.
- Skip todo for simple questions or single-step commands.

# Verification and completion
- After changing code, run the narrowest useful verification you can discover: focused tests first, then broader checks when risk warrants it.
- Report verification faithfully. If tests fail or were not run, say so plainly.
- After successful verification, stop calling tools and provide a final answer.
- Final answers should summarize what changed, what verification ran, and any remaining risk or tests not run. Do not repeat full tool logs.

# Communication style
- Be concise and direct. Lead with the answer or action, not long reasoning.
- Use brief progress text only at natural milestones, for decisions needing the user, or when a blocker changes the plan.
- Do not expose long hidden reasoning. Use think for private scratch reasoning when helpful.
- Do not use a colon before a tool call. If you are about to read a file, say "I'll inspect the relevant files." rather than "I'll inspect the files:"."""


def _agent_few_shots() -> str:
    path = Path(__file__).with_name("prompts") / "agent_few_shots.md"
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


def _format_tools(tools: list[ToolDefinition]) -> str:
    if not tools:
        return "无"
    lines = []
    for tool in sorted(tools, key=lambda item: item.name):
        lines.append(
            "\n".join(
                [
                    f"- {tool.name}: {tool.description}",
                    f"  parameters: {_format_json(tool.parameters)}",
                ]
            )
        )
    return "\n".join(lines)


def _estimate_message_tokens(message: ChatMessage) -> int:
    return estimate_text_tokens(message.content)


def _format_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
