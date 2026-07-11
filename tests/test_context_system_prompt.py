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
    assert "# Project conventions" in content
    assert "# Decision and verification discipline" in content
    assert "# Task boundary" in content
    assert "# Tool use" in content
    assert "# Task tracking" in content
    assert "# Verification and completion" in content
    assert "# Communication style" in content
    assert "assume they want you to act" in content
    assert "Persist until the user's task is handled end-to-end" in content
    assert "check for additional AGENTS.md files whose scope may apply" in content
    assert "never revert, overwrite, or reformat changes you did not make" in content
    assert "observable success condition, material constraints, and smallest evidence" in content
    assert "Implement the observable contract, not only a visible example" in content
    assert "shared behavior, change the abstraction that owns the contract" in content
    assert "several material entry points or lifecycle paths" in content
    assert "existing extension boundaries as design constraints" in content
    assert "do not add a type special-case or broader base abstraction" in content
    assert "Do not expose long chain-of-thought" in content
    assert "At the start of every user turn, call task_boundary before answering or using any other tool" in content
    assert "Runtime control messages such as \"Todo planning reminder\"" in content
    assert "Never invent, guess, or display task hashes" in content
    assert "After task_boundary, answer a simple question directly" in content
    assert "issue multiple read-only tool calls in the same assistant response" in content
    assert "Do not batch tools whose inputs depend on previous tool results" in content
    assert "Prefer rg or rg --files" in content
    assert "Use todo for multi-step coding tasks" in content
    assert "infer them from repo files, docs, or neighboring tests" in content
    assert "complete this order: verify the requested behavior, then inspect the relevant diff or status" in content
    assert "Stop calling tools and provide a final answer only after that verification and diff/status review are sufficient" in content
    assert "A successful command is sufficient only when it meaningfully covers" in content
    assert "The user does not see full tool output" in content
    discipline = content.split("# Decision and verification discipline", 1)[1].split("# Task boundary", 1)[0]
    assert discipline.count("\n- ") == 6


def test_system_prompt_requires_task_boundary_at_start_of_each_user_turn() -> None:
    entry = SystemPromptBuilder().build(_inputs())
    content = entry.messages[0].content

    assert "At the start of every user turn, call task_boundary before answering or using any other tool" in content
    assert "Skip task_boundary only when no tools are available" in content


def test_system_prompt_includes_external_few_shots() -> None:
    entry = SystemPromptBuilder().build(_inputs())
    content = entry.messages[0].content

    assert "# Few-shot examples" in content
    assert "Example: new coding task" in content
    assert "task_boundary(decision=\"new\"" in content
    assert "Call `task_boundary` first when the tool is available." in content
    assert "Example: runtime control reminder" in content
    assert "Treat it as an internal continuation message for the active task" in content
    assert "identify the intended public contract and constraints before editing" in content
    assert "use an established extension route instead of a one-off special case in the base" in content
    assert "Verify the changed public behavior and any other material entry path" in content
    assert "Example: sufficient verification" in content
    assert "Do not call more unrelated tools after sufficient verification." in content
    assert "For code changes, inspect the relevant diff or status before the final answer." in content


def test_system_prompt_version_is_v11() -> None:
    entry = SystemPromptBuilder().build(_inputs())

    assert "prompt_version=v11" in entry.messages[0].content


def test_system_prompt_token_estimate_uses_shared_estimator() -> None:
    entry = SystemPromptBuilder().build(_inputs(base_rules="12345", agents_md="", tools=[]))

    assert entry.token_estimate == estimate_text_tokens(entry.messages[0].content)
