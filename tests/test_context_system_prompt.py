from firstcoder.context.system_prompt import (
    PromptPrefixCache,
    SystemPromptInputs,
    SystemPromptBuilder,
)
from firstcoder.context.token_budget import estimate_text_tokens
from firstcoder.providers.types import ToolDefinition


def _inputs(**overrides: object) -> SystemPromptInputs:
    values = {
        "base_rules": "你是 FirstCoder。",
        "agents_md": "项目规则：上下文放在 firstcoder/context。",
        "tools": [
            ToolDefinition(
                name="read_file",
                description="读取文件",
                parameters={"type": "object", "properties": {"path": {"type": "string"}}},
            )
        ],
        "provider_name": "openai-compatible",
        "provider_capabilities": {"tool_calling": True, "parallel_tool_calls": False},
        "permission_policy": {"shell": "confirm", "read": "allow"},
        "mode": "default",
    }
    values.update(overrides)
    return SystemPromptInputs(**values)


def test_system_prompt_fingerprint_is_stable_for_same_inputs() -> None:
    builder = SystemPromptBuilder()

    assert builder.fingerprint(_inputs()) == builder.fingerprint(_inputs())


def test_system_prompt_cache_reuses_prefix_when_fingerprint_matches() -> None:
    builder = SystemPromptBuilder()
    cache = PromptPrefixCache()
    inputs = _inputs()

    first = cache.get_or_build(inputs, builder)
    second = cache.get_or_build(inputs, builder)

    assert first.fingerprint == second.fingerprint
    assert first is second
    assert first.messages[0].role == "system"
    assert "你是 FirstCoder。" in first.messages[0].content


def test_agents_md_change_invalidates_system_prompt_fingerprint() -> None:
    builder = SystemPromptBuilder()

    before = builder.fingerprint(_inputs(agents_md="规则 A"))
    after = builder.fingerprint(_inputs(agents_md="规则 B"))

    assert before != after


def test_tool_schema_change_invalidates_system_prompt_fingerprint() -> None:
    builder = SystemPromptBuilder()

    changed_tool = [
        ToolDefinition(
            name="read_file",
            description="读取文件",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "encoding": {"type": "string"},
                },
            },
        )
    ]

    assert builder.fingerprint(_inputs()) != builder.fingerprint(_inputs(tools=changed_tool))


def test_conversation_messages_do_not_invalidate_system_prompt_fingerprint() -> None:
    builder = SystemPromptBuilder()
    inputs = _inputs()
    before = builder.fingerprint(inputs)
    after = builder.fingerprint(inputs)

    assert before == after


def test_provider_capability_change_invalidates_system_prompt_fingerprint() -> None:
    builder = SystemPromptBuilder()

    before = builder.fingerprint(_inputs(provider_capabilities={"tool_calling": True}))
    after = builder.fingerprint(_inputs(provider_capabilities={"tool_calling": False}))

    assert before != after


def test_skill_catalog_change_invalidates_system_prompt_fingerprint() -> None:
    builder = SystemPromptBuilder()

    before = builder.fingerprint(_inputs(skill_catalog_summary="skills/brief.md - 简报"))
    after = builder.fingerprint(_inputs(skill_catalog_summary="skills/review.md - 复核"))

    assert before != after


def test_system_prompt_includes_skill_protocol_catalog_and_loaded_skill() -> None:
    entry = SystemPromptBuilder().build(
        _inputs(
            skill_protocol="被路由到 skill 时，必须先加载 skill。",
            skill_catalog_summary=(
                "- project:skills/global-family-office-news-brief.md - 全球家办资讯简报\n"
                "- global:fetch-tweet/SKILL.md - 读取 X/Twitter 帖子"
            ),
            loaded_skill_context=(
                "Loaded skill: skills/global-family-office-news-brief.md\n"
                "# 全球家族办公室资讯简报\n"
                "必须读取 docs/evidence-policy.md"
            ),
        )
    )
    content = entry.messages[0].content

    assert "Project skill protocol" in content
    assert "被路由到 skill 时，必须先加载 skill。" in content
    assert "Available skills" in content
    assert "project:skills/global-family-office-news-brief.md" in content
    assert "global:fetch-tweet/SKILL.md" in content
    assert "Loaded skills" in content
    assert "# 全球家族办公室资讯简报" in content
    assert "unrelated skill body" not in content


def test_system_prompt_contains_readable_permission_policy_and_tool_schema() -> None:
    entry = SystemPromptBuilder().build(_inputs())
    content = entry.messages[0].content

    assert '"shell": "confirm"' in content
    assert '"read": "allow"' in content
    assert "read_file" in content
    assert '"path": {' in content
    assert '"type": "string"' in content


def test_system_prompt_contains_english_agent_behavior_rules() -> None:
    entry = SystemPromptBuilder().build(_inputs())
    content = entry.messages[0].content

    assert "# Role and operating context" in content
    assert "# Working loop" in content
    assert "# Task boundary" in content
    assert "# Tool use" in content
    assert "# Task tracking" in content
    assert "# Verification and completion" in content
    assert "# Communication style" in content
    assert "Call task_boundary before substantial work" in content
    assert "Never invent, guess, or display task hashes" in content
    assert "Use todo for multi-step coding tasks" in content
    assert "After successful verification, stop calling tools" in content


def test_system_prompt_includes_external_few_shots() -> None:
    entry = SystemPromptBuilder().build(_inputs())
    content = entry.messages[0].content

    assert "# Few-shot examples" in content
    assert "Example: new coding task" in content
    assert "task_boundary(decision=\"new\"" in content
    assert "Example: verification passed" in content
    assert "Do not call more tools after a successful verification command." in content


def test_system_prompt_version_is_v4() -> None:
    entry = SystemPromptBuilder().build(_inputs())

    assert "prompt_version=v4" in entry.messages[0].content


def test_system_prompt_token_estimate_uses_shared_estimator() -> None:
    entry = SystemPromptBuilder().build(_inputs(base_rules="12345", agents_md="", tools=[]))

    assert entry.token_estimate == estimate_text_tokens(entry.messages[0].content)
