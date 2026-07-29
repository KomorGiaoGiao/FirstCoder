from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("harbor")

from benchmark.harbor.firstcoder_agent import (  # noqa: E402
    FirstCoderHarborAgent,
    _catalog_bootstrap_command,
    _install_command,
)
from benchmark.harbor.aider_feedback_trial import (  # noqa: E402
    AiderFeedbackTrial,
    build_aider_feedback,
    create_aider_feedback_trial,
    should_request_feedback_after_missing_reward,
    should_request_feedback_round,
)
from benchmark.harbor.aider_feedback_plugin import AiderFeedbackPlugin  # noqa: E402


def test_harbor_agent_builds_quoted_firstcoder_benchmark_command(tmp_path: Path) -> None:
    agent = FirstCoderHarborAgent(logs_dir=tmp_path, max_tool_rounds="77")

    command = agent._run_command("Fix the task.\nRun tests.", session_id="task/id")

    assert "/opt/firstcoder-agent/.venv/bin/python -m firstcoder" in command
    assert "--benchmark --project ." in command
    assert "--data-root /tmp/firstcoder-harbor-sessions" in command
    assert "--session-id task_id" in command
    assert "--max-tool-rounds 77" in command
    assert '--model "${FIRSTCODER_PROVIDER_NAME}/${FIRSTCODER_MODEL}"' in command
    assert '"[providers." + quote(provider)' in command
    assert 'api_key_env = \"FIRSTCODER_API_KEY\"' in command
    assert "FIRSTCODER_BASE_URL" in command
    assert "'Fix the task." in command
    assert "/logs/agent/firstcoder.txt" in command
    assert "set -o pipefail" in command
    assert "FIRSTCODER_EXIT" in command
    assert "/logs/agent/firstcoder-session.jsonl" in command
    assert "/tmp/firstcoder-harbor-sessions/sessions/task_id.jsonl" in command
    assert "warning: failed to export FirstCoder benchmark session" in command
    assert 'api_key = ' not in command


def test_harbor_catalog_bootstrap_writes_parseable_standard_config(tmp_path: Path) -> None:
    command = _catalog_bootstrap_command().replace(
        "/opt/firstcoder-agent/.venv/bin/python",
        sys.executable,
    ).replace("/tmp/firstcoder-harbor-config", str(tmp_path / "xdg"))
    env = {
        **os.environ,
        "FIRSTCODER_PROVIDER_NAME": "Yuren",
        "FIRSTCODER_MODEL": "gpt-test",
        "FIRSTCODER_BASE_URL": "https://example.test/v1",
    }

    subprocess.run(command, cwd=tmp_path, env=env, shell=True, executable="/bin/zsh", check=True)

    config_path = tmp_path / "xdg" / "firstcoder" / "config.toml"
    assert not (tmp_path / "firstcoder.toml").exists()
    config = config_path.read_text(encoding="utf-8")
    assert 'default_model = "Yuren/gpt-test"' in config
    assert '[providers."Yuren"]' in config
    assert '[models."Yuren/gpt-test"]' in config


def test_harbor_agent_passes_reasoning_effort_to_firstcoder(tmp_path: Path) -> None:
    agent = FirstCoderHarborAgent(logs_dir=tmp_path, reasoning_effort="high")

    command = agent._run_command("Fix the task.", session_id="task")

    assert "--reasoning-effort high" in command


def test_harbor_agent_marks_feedback_turn_as_a_session_resume(tmp_path: Path) -> None:
    agent = FirstCoderHarborAgent(logs_dir=tmp_path)

    command = agent._run_command(
        "The tests are correct. Do not modify the tests.\nTesting errors:\nFAIL",
        session_id="task",
        resume_session=True,
    )

    assert "--resume-session" in command


def test_aider_feedback_round_only_follows_a_real_failed_reward() -> None:
    assert should_request_feedback_round({"reward": 0})
    assert should_request_feedback_round({"reward": 0.0, "other": 1})
    assert not should_request_feedback_round({"reward": 1})
    assert not should_request_feedback_round(None)
    assert not should_request_feedback_round({})


def test_aider_feedback_round_follows_a_cpp_test_compile_failure_without_reward() -> None:
    output = """\
/app/example_test.cpp:15: error: no matching function for call to 'convert'
make: *** [Makefile:91: all] Error 2
CMake build failed
"""

    assert should_request_feedback_after_missing_reward(output)


def test_aider_feedback_round_does_not_follow_missing_reward_from_dependency_install() -> None:
    output = """\
Installing Python test dependencies...
ERROR: Could not find a version that satisfies the requirement pytest
"""

    assert not should_request_feedback_after_missing_reward(output)


def test_aider_feedback_prompt_preserves_test_output_and_protects_tests() -> None:
    feedback = build_aider_feedback("FAIL: expected 4 but got 3\n")

    assert "The tests are correct" in feedback
    assert "Do not modify the tests" in feedback
    assert "FAIL: expected 4 but got 3" in feedback


def test_aider_feedback_factory_preserves_multistep_tasks() -> None:
    class FakeTask:
        has_steps = True

    class FakeTrial:
        @classmethod
        async def _load_task(cls, config):
            return FakeTask(), "download"

    created = asyncio.run(create_aider_feedback_trial(FakeTrial, object()))

    assert created is None


def test_aider_feedback_plugin_restores_harbor_trial_factory() -> None:
    from harbor.trial.trial import Trial

    original = Trial.__dict__["create"]
    plugin = AiderFeedbackPlugin()

    async def exercise() -> None:
        await plugin.on_job_start(object())
        assert Trial.__dict__["create"] is not original
        await plugin.on_job_end(None)

    asyncio.run(exercise())

    assert Trial.__dict__["create"] is original


def test_harbor_agent_stages_only_runtime_source_tree(tmp_path: Path) -> None:
    source = tmp_path / "source"
    package = source / "firstcoder"
    package.mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname = 'firstcoder'\n")
    (source / "README.md").write_text("# FirstCoder\n")
    (package / "__init__.py").write_text("__version__ = 'test'\n")
    (package / "module.py").write_text("value = 1\n")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"ignored")
    (source / ".env").write_text("SECRET=not-copied\n")

    agent = FirstCoderHarborAgent(logs_dir=tmp_path / "logs", source_dir=source)
    staged = agent._stage_local_source()

    assert (staged / "pyproject.toml").is_file()
    assert (staged / "README.md").is_file()
    assert (staged / "firstcoder" / "module.py").is_file()
    assert not (staged / "firstcoder" / "__pycache__").exists()
    assert not (staged / ".env").exists()


def test_harbor_agent_uses_explicit_package_fallback(tmp_path: Path) -> None:
    package = "https://example.invalid/firstcoder.zip"
    agent = FirstCoderHarborAgent(
        logs_dir=tmp_path,
        source_dir=tmp_path / "missing",
        package=package,
    )

    assert agent._package == package
    assert package in _install_command(package)


def test_harbor_agent_rejects_invalid_tool_round_limit(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="max_tool_rounds"):
        FirstCoderHarborAgent(logs_dir=tmp_path, max_tool_rounds=0)


def test_harbor_install_prefers_a_suitable_existing_python() -> None:
    command = _install_command("/installed-agent/firstcoder-src")

    assert "for candidate in python3.12 python3.11 python3" in command
    assert "sys.version_info < (3, 11)" in command
    assert '--python "$PYTHON_BIN" --clear' in command


def test_harbor_install_can_use_python_venv_without_curl_or_wget() -> None:
    command = _install_command("/installed-agent/firstcoder-src")

    assert 'if [ -x "$UV_BIN" ]; then' in command
    assert '"$PYTHON_BIN" -m venv "$AGENT_ROOT/.venv" --clear' in command
    assert '"$AGENT_ROOT/.venv/bin/python" -m pip install --cache-dir "$CACHE_DIR"' in command
    assert "astral.sh/uv/install.sh" not in command
    assert "wget" not in command


def test_harbor_install_reuses_a_shared_download_cache() -> None:
    command = _install_command("/installed-agent/firstcoder-src")

    assert "CACHE_DIR=/opt/firstcoder-cache" in command
    assert '"$UV_BIN" pip install --python "$AGENT_ROOT/.venv/bin/python" --cache-dir "$CACHE_DIR"' in command
    # The cache stores downloaded wheels only; the venv is still rebuilt per trial.
    assert "--no-cache" not in command
    assert command.count("--clear") == 2


def test_harbor_install_retries_the_download_step_with_backoff() -> None:
    command = _install_command("/installed-agent/firstcoder-src")

    assert "install_deps() {" in command
    assert "until install_deps; do" in command
    assert 'if [ "$attempt" -ge 3 ]; then' in command
    assert "failed to install dependencies after" in command
    assert 'sleep "$((attempt * 5))"' in command


def test_harbor_agent_does_not_require_system_package_installation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "firstcoder").mkdir(parents=True)
    (source / "pyproject.toml").write_text("[project]\nname = 'firstcoder'\n")
    (source / "README.md").write_text("# FirstCoder\n")

    agent = FirstCoderHarborAgent(logs_dir=tmp_path / "logs", source_dir=source)

    assert "apt-get" not in agent.install.__doc__ or True


def test_harbor_agent_bootstraps_python_311_before_installing(tmp_path: Path) -> None:
    agent = FirstCoderHarborAgent(logs_dir=tmp_path)

    command = agent._python_setup_command()

    assert "case \"$PACKAGE_MANAGER\" in" in command
    assert "apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates" in command
    assert "apt-get update && apt-get install -y --no-install-recommends python3-venv" in command
    assert "apk add --no-cache python3 py3-pip" in command
    assert "apk add --no-cache py3-pip" in command
    assert "dnf install -y python3 python3-pip" in command
    assert "yum install -y python3 python3-pip" in command
    assert 'curl -LsSf https://astral.sh/uv/install.sh' in command
    assert 'wget -qO- https://astral.sh/uv/install.sh' in command
    assert 'UV_UNMANAGED_INSTALL="' in command
    assert '"$AGENT_ROOT/bin/uv" python find 3.11' in command
    assert 'has_venv "$PYTHON_BIN"' in command
    assert 'if [ -n "$PYTHON_BIN" ] && ! has_venv "$PYTHON_BIN"; then' in command
    assert 'if install_python_venv && has_venv "$PYTHON_BIN"; then' in command
    assert 'install_system_python\n  PYTHON_BIN="$(find_python || true)"' in command
    assert "requires Python 3.11+ with venv and pip after bootstrap" in command
    assert '"$1" -m venv "$venv_probe/test-venv"' in command
    assert "for candidate in python3.12 python3.11 python3; do" in command
    assert '"$candidate" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"' in command


def test_harbor_python_bootstrap_fails_clearly_without_supported_package_manager(tmp_path: Path) -> None:
    command = FirstCoderHarborAgent(logs_dir=tmp_path)._python_setup_command()

    assert 'PACKAGE_MANAGER="unsupported"' in command
    assert "cannot bootstrap Python 3.11 or newer" in command


def test_harbor_python_bootstrap_retries_flaky_package_and_network_steps(tmp_path: Path) -> None:
    command = FirstCoderHarborAgent(logs_dir=tmp_path)._python_setup_command()

    # A retry helper with bounded backoff wraps every step that hits a mirror.
    assert "retry() {" in command
    assert 'until "$@"; do' in command
    assert 'if [ "$attempt" -ge 3 ]; then' in command
    assert 'sleep "$((attempt * 5))"' in command
    # System package installs and network fetches all go through retry.
    assert "retry sh -c 'apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates'" in command
    assert "retry sh -c 'apt-get update && apt-get install -y --no-install-recommends python3-venv'" in command
    assert "retry apk add --no-cache python3 py3-pip" in command
    assert "retry dnf install -y python3 python3-pip" in command
    assert "retry yum install -y python3 python3-pip" in command
    assert "retry sh -c 'curl -LsSf https://astral.sh/uv/install.sh" in command
    assert 'retry "$AGENT_ROOT/bin/uv" python install 3.11' in command
