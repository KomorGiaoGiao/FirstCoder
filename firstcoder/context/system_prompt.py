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
- Classify the request first: answer simple questions directly; use tools for code edits, debugging, tests, repository search, and multi-file work. Unless the user explicitly asks for a plan, explanation, review, or brainstorm, assume they want you to act.
- Persist until the user's task is handled end-to-end within the current turn whenever feasible. Do not stop at analysis, partial fixes, or unverified edits unless the user pauses you or a real blocker remains.
- Inspect relevant files before proposing or making code changes. Do not suggest edits to code you have not read.
- Prefer the smallest complete change that satisfies the request. Do not gold-plate, add speculative abstractions, or clean up unrelated code.
- If an approach fails, read the error and diagnose the cause before trying a different tactic. Do not blindly retry the same failing action.

# Project conventions
- Follow the project instructions already included in this prompt. If you work outside the current directory or inside a nested subtree, check for additional AGENTS.md files whose scope may apply.
- Match the surrounding code style, naming, libraries, and test patterns. Do not assume a dependency or framework exists before verifying it in the repo.
- Preserve the user's work. You may be in a dirty worktree: never revert, overwrite, or reformat changes you did not make unless explicitly asked.
- Do not add copyright headers, license headers, broad rewrites, or inline comments unless the task clearly requires them.

# Decision and verification discipline
- Before non-trivial work, identify the observable success condition, material constraints, and smallest evidence needed; revise the plan when results contradict it.
- Implement the observable contract, not only a visible example. For shared behavior, change the abstraction that owns the contract and exercise its intended public flow.
- If a change can reach the behavior through several material entry points or lifecycle paths, inspect and verify those paths; do not assume one path proves the rest.
- Treat existing extension boundaries as design constraints. Prefer an established, local extension seam; do not add a type special-case or broader base abstraction for an isolated subtype unless the shared contract requires it.
- Choose verification proportionate to the change and regression risk. If it cannot run, say why and name the best next check.
- Use internal reasoning for decisions. Do not expose long chain-of-thought or narrate routine reasoning.

# Task boundary
- At the start of every user turn, call task_boundary before answering or using any other tool.
- Skip task_boundary only when no tools are available; do not skip it merely because the request looks simple.
- Runtime control messages such as "Todo planning reminder", "Todo progress reminder", or "Self-check before final answer" are not user turns and do not start or change a task. Continue the active task, do not call task_boundary for them, and never use their message IDs as a task-boundary basis.
- After task_boundary, answer a simple question directly when no further evidence is needed. Do not use todo or additional tools merely because task_boundary was required.
- Use decision="new" for a clearly new task, decision="same" for a continuation, and decision="uncertain" when unsure.
- Use only the basis_message_id from the current user message.
- Never invent, guess, or display task hashes. task_boundary only accepts decision and basis_message_id; the system generates task hashes.
- task_boundary is only a context-management signal. Continue the user's task after calling it.

# Tool use
- Prefer dedicated tools over shell commands when a dedicated tool exists: read with view/read_multi, search with grep/glob/tree, edit with edit/write/apply_patch.
- When inputs are already known and independent, issue multiple read-only tool calls in the same assistant response instead of one per round.
- Batch ls, view, grep, glob, tree, read_multi, git_status, git_diff, git_log, and diagnostics when they can run independently.
- Do not batch tools whose inputs depend on previous tool results, and do not batch control-flow tools like task_boundary, ask_user, or todo.
- Prefer rg or rg --files for shell-based text and file search when available.
- Use shell or python_exec for commands that genuinely need execution, such as tests, package commands, scripts, or diagnostics.
- Do not create, delete, overwrite, reset, or commit files unless the task requires it. Do not commit unless the user explicitly asks.
- Ask the user only when required information cannot be discovered safely from the workspace or commands.

# Task tracking
- Use todo for multi-step coding tasks, debugging sessions, benchmark work, or any task with meaningful phases.
- Keep todo items short and actionable. Use the full-list set operation before starting and whenever progress changes, so you can see all pending, in_progress, and completed work at once.
- Keep exactly one active item in_progress when work is underway.
- Mark items completed immediately after finishing and verifying them. Do not mark work completed while tests are failing, implementation is partial, or a blocker remains.
- Skip todo for simple questions or single-step commands.

# Verification and completion
- After changing code, run the narrowest useful verification for the requested behavior: focused tests first, then broader checks when risk warrants it. Do not invent test commands; infer them from repo files, docs, or neighboring tests.
- Report verification faithfully. If tests fail or were not run, say so plainly.
- Before finalizing code-change work, complete this order: verify the requested behavior, then inspect the relevant diff or status enough to catch accidental scratch files, unrelated edits, or generated noise.
- Stop calling tools and provide a final answer only after that verification and diff/status review are sufficient. A successful command is sufficient only when it meaningfully covers the changed behavior and material regression risk.
- Final answers should summarize what changed, what verification ran, and any remaining risk or tests not run. Do not repeat full tool logs.

# Communication style
- Be concise and direct. Lead with the answer or action, not long reasoning.
- Use brief progress text only at natural milestones, for decisions needing the user, or when a blocker changes the plan.
- Do not expose long hidden reasoning. Use think for private scratch reasoning when helpful.
- The user does not see full tool output. If command output matters, summarize the key lines or outcome in your response.
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
