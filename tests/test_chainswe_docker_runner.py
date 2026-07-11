import json
from pathlib import Path

import pytest

from benchmark.chainswe.docker_runner import (
    CONTAINER_AGENT_SOURCE,
    CONTAINER_AGENT_VENV,
    CONTAINER_PYTHON,
    DEFAULT_CHAIN_ID,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    DEFAULT_PROVIDER_NAME,
    build_container_bootstrap,
    build_docker_command,
    build_parser,
    make_run_plan,
    prepare_run_directory,
    run_docker_plan,
    serialize_chain_for_stdin,
)


def _write_dataset(path: Path, *, continuous_id: str = DEFAULT_CHAIN_ID) -> None:
    record = {
        "continuous_id": continuous_id,
        "repo": "meltano-sdk",
        "base_commit": "b1b3bd2",
        "docker_image": "clisterqj/swechain:official-image",
        "bug_fixes": [
            {
                "order": 1,
                "swebench_instance_id": "meltano_sdk__1864",
                "problem_statement": "Fix the issue.",
                "FAIL_TO_PASS": [],
                "PASS_TO_PASS": [],
                "test_patch": "HIDDEN_TEST_PATCH_CONTENT",
                "test_cmds": "hidden-verifier-command --never-show-agent",
            }
        ],
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def _source_root(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname = 'test'\n", encoding="utf-8")
    return source


def test_default_plan_selects_default_chain_and_requires_only_key_name(tmp_path: Path):
    dataset = tmp_path / "chains.jsonl"
    _write_dataset(dataset)
    source = _source_root(tmp_path)

    plan = make_run_plan(
        chains_path=dataset,
        source_root=source,
        environ={"FIRSTCODER_GPT56_API_KEY": "super-secret"},
    )

    assert plan.chain.continuous_id == DEFAULT_CHAIN_ID
    assert plan.provider == DEFAULT_PROVIDER
    assert plan.provider_name == DEFAULT_PROVIDER_NAME
    assert plan.model == DEFAULT_MODEL
    assert plan.run_dir.parent == source / "runs" / "chainswe"


def test_docker_command_uses_official_image_and_never_exposes_hidden_chain_data(tmp_path: Path):
    dataset = tmp_path / "chains.jsonl"
    _write_dataset(dataset)
    source = _source_root(tmp_path)
    plan = make_run_plan(
        chains_path=dataset,
        source_root=source,
        run_dir=tmp_path / "run",
        api_key_env="BENCHMARK_API_KEY",
        environ={"BENCHMARK_API_KEY": "super-secret-value"},
        max_tool_rounds=17,
    )

    command = build_docker_command(plan)
    rendered = "\n".join(command)

    assert command[:6] == ["docker", "run", "--rm", "-i", "--platform", "linux/amd64"]
    assert "clisterqj/swechain:official-image" in command
    assert "--env\nBENCHMARK_API_KEY" in rendered
    assert "BENCHMARK_API_KEY=super-secret-value" not in rendered
    assert "super-secret-value" not in rendered
    assert f"src={source},dst=/opt/firstcoder-src,readonly" in rendered
    assert f"src={plan.run_dir},dst=/runs" in rendered
    assert "dst=/chains" not in rendered
    assert "HIDDEN_TEST_PATCH_CONTENT" not in rendered
    assert "hidden-verifier-command --never-show-agent" not in rendered
    assert "FIRSTCODER_PROVIDER=openai-compatible" in rendered
    assert "FIRSTCODER_MODEL=gpt-5.6-terra" in rendered
    assert "17" in command


def test_bootstrap_uses_target_python_for_agent_venv_and_keeps_verifier_path_clean():
    bootstrap = build_container_bootstrap()

    assert CONTAINER_PYTHON in bootstrap
    assert f'agent_venv={CONTAINER_AGENT_VENV}' in bootstrap
    assert '"$python_bin" -m venv "$agent_venv"' in bootstrap
    assert f"agent_source={CONTAINER_AGENT_SOURCE}" in bootstrap
    assert 'tar -C /opt/firstcoder-src' in bootstrap
    assert '--exclude=.venv' in bootstrap
    assert '--exclude=.git' in bootstrap
    assert '| tar -C "$agent_source" -xf -' in bootstrap
    assert '"$agent_venv/bin/python" -m pip install --disable-pip-version-check --no-cache-dir "$agent_source"' in bootstrap
    assert f'"$agent_venv/bin/python" "$agent_source/benchmark/chainswe/runner.py"' in bootstrap
    assert 'max_tool_rounds="${5:-}"' in bootstrap
    assert 'if [ -n "$max_tool_rounds" ]; then' in bootstrap
    assert 'set -- "$@" --max-tool-rounds "$max_tool_rounds"' in bootstrap
    assert "--chain-stdin" in bootstrap
    assert "--chains" not in bootstrap
    assert "export PATH=" not in bootstrap
    assert "printenv \"$FIRSTCODER_CHAIN_API_KEY_ENV\"" in bootstrap
    assert "FIRSTCODER_GPT56_API_KEY" not in bootstrap


def test_selected_chain_is_serialized_only_for_stdin(tmp_path: Path):
    dataset = tmp_path / "chains.jsonl"
    _write_dataset(dataset)
    source = _source_root(tmp_path)
    plan = make_run_plan(
        chains_path=dataset,
        source_root=source,
        environ={"FIRSTCODER_GPT56_API_KEY": "secret"},
    )

    payload = serialize_chain_for_stdin(plan.chain)

    assert "HIDDEN_TEST_PATCH_CONTENT" in payload
    assert "hidden-verifier-command --never-show-agent" in payload


def test_run_docker_plan_streams_selected_chain_on_stdin(tmp_path: Path, monkeypatch):
    dataset = tmp_path / "chains.jsonl"
    _write_dataset(dataset)
    source = _source_root(tmp_path)
    plan = make_run_plan(
        chains_path=dataset,
        source_root=source,
        run_dir=tmp_path / "run",
        environ={"FIRSTCODER_GPT56_API_KEY": "secret"},
    )
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured.update(kwargs)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("benchmark.chainswe.docker_runner.subprocess.run", fake_run)

    assert run_docker_plan(plan) == 0
    assert captured["text"] is True
    assert "HIDDEN_TEST_PATCH_CONTENT" in captured["input"]
    assert "HIDDEN_TEST_PATCH_CONTENT" not in "\n".join(captured["command"])


def test_plan_rejects_missing_or_invalid_api_key_environment_name(tmp_path: Path):
    dataset = tmp_path / "chains.jsonl"
    _write_dataset(dataset)
    source = _source_root(tmp_path)

    with pytest.raises(ValueError, match="not set"):
        make_run_plan(chains_path=dataset, source_root=source, environ={})
    with pytest.raises(ValueError, match="valid environment variable"):
        make_run_plan(
            chains_path=dataset,
            source_root=source,
            api_key_env="not valid",
            environ={"not valid": "secret"},
        )


def test_prepare_run_directory_refuses_non_empty_directory(tmp_path: Path):
    dataset = tmp_path / "chains.jsonl"
    _write_dataset(dataset)
    source = _source_root(tmp_path)
    run_dir = tmp_path / "run"
    plan = make_run_plan(
        chains_path=dataset,
        source_root=source,
        run_dir=run_dir,
        environ={"FIRSTCODER_GPT56_API_KEY": "secret"},
    )

    prepare_run_directory(plan)
    assert plan.session_dir.is_dir()
    (run_dir / "old-summary.json").write_text("old", encoding="utf-8")
    with pytest.raises(ValueError, match="not empty"):
        prepare_run_directory(plan)


def test_parser_exposes_gpt56_default_chain_and_provider_configuration():
    args = build_parser().parse_args(["--chains", "chains.jsonl"])

    assert args.chain_id == DEFAULT_CHAIN_ID
    assert args.provider == DEFAULT_PROVIDER
    assert args.provider_name == DEFAULT_PROVIDER_NAME
    assert args.model == DEFAULT_MODEL
