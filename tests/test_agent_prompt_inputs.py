from firstcoder.agent.prompt_inputs import (
    DEFAULT_PERMISSION_POLICY,
    build_system_prompt_inputs,
    provider_capabilities_for,
    read_agents_md,
)
from firstcoder.context.system_prompt import SystemPromptBuilder


def test_read_agents_md_reads_project_root_file(tmp_path) -> None:
    (tmp_path / "AGENTS.md").write_text("项目规则", encoding="utf-8")

    assert read_agents_md(tmp_path) == "项目规则"


def test_read_agents_md_returns_empty_when_missing(tmp_path) -> None:
    assert read_agents_md(tmp_path) == ""


def test_provider_capabilities_are_static_and_include_model() -> None:
    capabilities = provider_capabilities_for("anthropic", provider_model="claude-test")

    assert capabilities["tool_calling"] is True
    assert capabilities["parallel_tool_calls"] is False
    assert capabilities["system_prompt"] == "separate_field"
    assert capabilities["tool_schema"] == "anthropic_messages"
    assert capabilities["model"] == "claude-test"


def test_build_system_prompt_inputs_uses_permission_policy_without_tool_schema() -> None:
    inputs = build_system_prompt_inputs(
        base_rules="基础规则",
        agents_md="项目规则",
        provider_name="fake",
        provider_model="fake-model",
        permission_policy={"write": "allow"},
    )
    content = SystemPromptBuilder().build(inputs).messages[0].content

    assert "项目规则" in content
    assert "Available tools" not in content
    assert '"model": "fake-model"' in content
    assert '"write": "allow"' in content
    assert inputs.permission_policy["shell"] == DEFAULT_PERMISSION_POLICY["shell"]


def test_system_prompt_guides_incremental_task_plan_updates() -> None:
    inputs = build_system_prompt_inputs(
        base_rules="基础规则",
        agents_md="",
        provider_name="fake",
        provider_model="fake-model",
    )
    content = SystemPromptBuilder().build(inputs).messages[0].content

    assert "Use the Current TaskPlan snapshot attached to each main request" in content
    assert "Call task_list only when that snapshot is missing or a write reports a revision conflict." in content
    assert "Start with task_list to read the authoritative plan and its revision." not in content
    assert "Use task_create only to create a plan or append new tasks." in content
    assert "Use task_update to change status, owner, or dependencies by stable task ID." in content
    assert "Use task_revise only when a task's semantic content must change." in content
    assert "Never resend or replace the whole task list just to update one task." in content
    assert "If a write reports a revision conflict, call task_list and retry against its revision." in content
    assert "A `linear` TaskPlan executes in its stable display order" in content
    assert "A `dag` TaskPlan uses explicit dependencies" in content
    assert "old, missing-version, and future-version sessions are rejected" in content
    assert "Do not attempt migration, fallback replay, or recovery from legacy tool results." in content


def test_default_permission_policy_describes_mcp_tool_confirmation() -> None:
    assert DEFAULT_PERMISSION_POLICY["mcp_tools"] == "confirm"
