"""Agent system prompt 输入装配。

这一层把项目规则、provider 静态能力、权限占位策略和工具 schema 合并成
`SystemPromptInputs`。它不生成 prompt 文本，也不读取会话历史；这些职责分别属于
`SystemPromptBuilder` 和 `ContextBuilder`。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from firstcoder.context.system_prompt import SystemPromptInputs
from firstcoder.providers.types import ProviderCapabilities


DEFAULT_PERMISSION_POLICY: dict[str, Any] = {
    "path_access": "project_root_only",
    "read": "allow",
    "write": "confirm",
    "delete": "confirm",
    "shell": "confirm",
    "network": "confirm",
    "mcp_tools": "confirm",
    "env_secrets": "redact",
}


def read_agents_md(project_root: str | Path) -> str:
    """读取项目根目录的 AGENTS.md。

    第一版只读取项目根目录这一份规则，避免在 context 装配阶段引入目录继承、覆盖和
    多文件合并语义。后续如果支持子目录 AGENTS.md，应在这里扩展，而不是散落在 agent
    loop 或 Textual UI 中。
    """

    agents_path = Path(project_root) / "AGENTS.md"
    if not agents_path.exists():
        return ""
    return agents_path.read_text(encoding="utf-8")


def provider_capabilities_for(provider_name: str, *, provider_model: str = "") -> dict[str, Any]:
    """返回第一版 provider 能力描述。

    这里使用静态表是刻意的：真实 provider 还没有 capability discovery 协议，先把影响
    system prompt fingerprint 的 provider 事实集中在一个窄入口，后续再替换成配置或探测。
    """

    normalized = provider_name.lower()
    base: dict[str, Any] = {
        "tool_calling": True,
        "parallel_tool_calls": False,
        "system_prompt": "message",
        "tool_result_role": "tool",
    }
    if normalized == "anthropic":
        base.update(
            {
                "system_prompt": "separate_field",
                "tool_schema": "anthropic_messages",
                "tool_result_role": "user_tool_result_block",
            }
        )
    else:
        base.update({"tool_schema": "openai_compatible_tools"})

    if provider_model:
        base["model"] = provider_model
    return base


def provider_capabilities_from_instance(
    capabilities: ProviderCapabilities | None,
    *,
    provider_name: str,
    provider_model: str = "",
) -> dict[str, Any]:
    """把 provider 实例能力转换成 system prompt 使用的稳定描述。"""

    base = provider_capabilities_for(provider_name, provider_model=provider_model)
    if capabilities is None:
        return base
    base.update(
        {
            "tool_calling": capabilities.supports_tools,
            "parallel_tool_calls": capabilities.supports_parallel_tool_calls,
            "streaming": capabilities.supports_streaming,
            "json_mode": capabilities.supports_json_mode,
            "vision": capabilities.supports_vision,
            "reasoning": capabilities.supports_reasoning,
            "token_param": capabilities.token_param,
        }
    )
    return base


def build_system_prompt_inputs(
    *,
    base_rules: str,
    agents_md: str,
    skill_protocol: str = "",
    skill_catalog_summary: str = "",
    loaded_skill_context: str = "",
    provider_name: str,
    provider_model: str = "",
    provider_capabilities: ProviderCapabilities | None = None,
    provider_capability_overrides: dict[str, Any] | None = None,
    permission_policy: dict[str, Any] | None = None,
    mode: str = "default",
) -> SystemPromptInputs:
    """组装 `SystemPromptInputs`，保证调用侧不用手写分散字段。"""

    capabilities = provider_capabilities_from_instance(
        provider_capabilities,
        provider_name=provider_name,
        provider_model=provider_model,
    )
    capabilities.update(provider_capability_overrides or {})

    resolved_permission_policy = dict(DEFAULT_PERMISSION_POLICY)
    resolved_permission_policy.update(permission_policy or {})

    return SystemPromptInputs(
        base_rules=base_rules,
        agents_md=agents_md,
        skill_protocol=skill_protocol,
        skill_catalog_summary=skill_catalog_summary,
        loaded_skill_context=loaded_skill_context,
        provider_name=provider_name,
        provider_capabilities=capabilities,
        permission_policy=resolved_permission_policy,
        mode=mode,
    )
