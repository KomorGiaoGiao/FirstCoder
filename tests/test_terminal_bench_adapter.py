from __future__ import annotations

import os

import pytest


terminal_bench = pytest.importorskip("terminal_bench")

from benchmark.terminal_bench.firstcoder_agent import (  # noqa: E402
    FirstCoderTerminalBenchAgent,
)


def test_terminal_bench_agent_builds_firstcoder_benchmark_command(monkeypatch) -> None:
    monkeypatch.setenv("FIRSTCODER_API_KEY", "secret")
    agent = FirstCoderTerminalBenchAgent(
        model_name="openai/gpt-4.1-mini",
        max_tool_rounds="77",
        session_root="/tmp/fc-sessions",
    )

    commands = agent._run_agent_commands("Fix the task.\nRun tests.")

    assert len(commands) == 1
    command = commands[0].command
    assert "/opt/firstcoder-agent/.venv/bin/python -m firstcoder" in command
    assert "--benchmark" in command
    assert "--project ." in command
    assert "--data-root /tmp/fc-sessions" in command
    assert "--session-id terminal-bench" in command
    assert "--max-tool-rounds 77" in command
    assert "'Fix the task." in command
    assert commands[0].block is True


def test_terminal_bench_agent_forwards_provider_environment(monkeypatch) -> None:
    for key in list(os.environ):
        if key.startswith("FIRSTCODER_") or key == "OPENAI_API_KEY":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "openai-secret")
    agent = FirstCoderTerminalBenchAgent(model_name="openai/gpt-4.1-mini")

    env = agent._env

    assert env["OPENAI_API_KEY"] == "openai-secret"
    assert env["FIRSTCODER_PROVIDER"] == "openai"
    assert env["FIRSTCODER_MODEL"] == "openai/gpt-4.1-mini"


def test_terminal_bench_agent_can_select_openai_compatible_provider() -> None:
    agent = FirstCoderTerminalBenchAgent(model_name="yurenapi/gpt-5.5")

    env = agent._env

    assert env["FIRSTCODER_PROVIDER"] == "openai-compatible"
    assert env["FIRSTCODER_PROVIDER_NAME"] == "yurenapi"
    assert env["FIRSTCODER_MODEL"] == "gpt-5.5"


def test_terminal_bench_factory_can_load_firstcoder_agent() -> None:
    from terminal_bench.agents.agent_factory import AgentFactory

    agent = AgentFactory.get_agent(
        import_path="benchmark.terminal_bench.firstcoder_agent:FirstCoderTerminalBenchAgent",
        model_name="openai/gpt-4.1-mini",
        max_tool_rounds="5",
    )

    assert isinstance(agent, FirstCoderTerminalBenchAgent)
    assert agent._run_agent_commands("hello")[0].command == (
        "/opt/firstcoder-agent/.venv/bin/python -m firstcoder "
        "--benchmark --project . "
        "--data-root /tmp/firstcoder-terminal-bench --session-id terminal-bench "
        "--max-tool-rounds 5 --message hello"
    )


def test_terminal_bench_setup_script_installs_git_when_missing() -> None:
    agent = FirstCoderTerminalBenchAgent(
        package="https://github.com/KomorGiaoGiao/FirstCoder/archive/refs/heads/main.zip"
    )

    script = agent._install_agent_script_path.read_text()

    assert "command -v git" in script
    assert 'missing_packages+=("git")' in script
    assert 'AGENT_VENV="/opt/firstcoder-agent/.venv"' in script
    assert 'missing_packages+=("python3-venv")' in script
    assert 'missing_packages+=("python3.11" "python3.11-venv")' in script
    assert 'PYTHON_BIN="python3"' in script
    assert 'PYTHON_BIN="python3.11"' in script
    assert "python3-pip" not in script
    assert "apt-get install -y --no-install-recommends" in script
    assert 'venv_probe="$(mktemp -d)"' in script
    assert '"$PYTHON_BIN" -m venv "$venv_probe/test-venv"' in script
    assert '"$venv_probe/test-venv/bin/python" -m pip --version' in script
    assert '"$PYTHON_BIN" - <<' in script
    assert '"$PYTHON_BIN" -m venv "$AGENT_VENV"' in script
    assert 'fail "venv pip is unavailable"' in script
    assert 'fail "python >=3.11 is required"' in script
    assert '"$AGENT_VENV/bin/python" -m pip install "$PACKAGE_SPEC"' in script
    assert 'fail "pip install failed"' in script
    assert "pip install --upgrade pip" not in script
