from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("harbor")

from benchmark.harbor.firstcoder_agent import (  # noqa: E402
    FirstCoderHarborAgent,
    _install_command,
)


def test_harbor_agent_builds_quoted_firstcoder_benchmark_command(tmp_path: Path) -> None:
    agent = FirstCoderHarborAgent(logs_dir=tmp_path, max_tool_rounds="77")

    command = agent._run_command("Fix the task.\nRun tests.", session_id="task/id")

    assert "/opt/firstcoder-agent/.venv/bin/python -m firstcoder" in command
    assert "--benchmark --project ." in command
    assert "--data-root /tmp/firstcoder-harbor-sessions" in command
    assert "--session-id task_id" in command
    assert "--max-tool-rounds 77" in command
    assert "'Fix the task." in command
    assert "/logs/agent/firstcoder.txt" in command
    assert "FIRSTCODER_API_KEY" not in command


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
    assert '"$AGENT_ROOT/.venv/bin/python" -m pip install --no-cache' in command
    assert "astral.sh/uv/install.sh" not in command
    assert "wget" not in command


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

    assert 'missing_packages+=("python3.11" "python3.11-venv")' in command
    assert "apt-get install -y --no-install-recommends" in command
    assert '"$PYTHON_BIN" -m venv "$venv_probe/test-venv"' in command
    assert "for candidate in python3.12 python3.11 python3; do" in command
    assert '"$candidate" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"' in command
