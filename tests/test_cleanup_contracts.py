from __future__ import annotations

import tomllib
from pathlib import Path

from firstcoder.agent.loop import AgentLoop
from firstcoder.config import AppConfig
from firstcoder.context import token_budget
from firstcoder.mcp.manager import McpManager
from firstcoder.providers import factory as provider_factory


ROOT = Path(__file__).resolve().parents[1]


def test_production_api_does_not_expose_test_only_helpers() -> None:
    assert not hasattr(AgentLoop, "run_user_turn_streaming_sync")
    assert not hasattr(token_budget, "estimate_chat_request_tokens")
    assert not hasattr(McpManager, "wait_for_connections")


def test_provider_factory_exposes_only_catalog_profile_construction() -> None:
    assert not hasattr(provider_factory, "create_provider")
    assert not hasattr(provider_factory, "create_provider_from_config")
    assert hasattr(provider_factory, "create_provider_for_model")


def test_app_config_does_not_cache_a_parallel_provider_selection() -> None:
    assert "provider_name" not in AppConfig.__dataclass_fields__


def test_pyproject_is_the_single_production_dependency_manifest() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]

    assert "pydantic" not in dependencies
    assert not (ROOT / "requirements.txt").exists()
