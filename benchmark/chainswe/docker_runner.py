"""Run one ChainSWE chain in its official task Docker image.

This is deliberately a host-side launcher, not part of FirstCoder's agent
runtime.  It selects the official image from the local ChainSWE dataset,
mounts the current source tree and invokes :mod:`benchmark.chainswe.runner`
inside the image's ``/sdk`` checkout.

The launcher never places an API-key *value* in a Docker command.  Docker is
asked to forward only the configured environment-variable name, and the
container bootstrap maps that value to ``FIRSTCODER_API_KEY`` at runtime.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

from benchmark.chainswe.models import ChainSWEChain, chain_to_record, load_chains_jsonl, select_chain


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CHAIN_ID = "meltano_sdk_20230727"
DEFAULT_PROVIDER = "openai-compatible"
DEFAULT_PROVIDER_NAME = "custom"
DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_BASE_URL = "https://yurenapi.com/v1"
DEFAULT_API_KEY_ENV = "FIRSTCODER_GPT56_API_KEY"
CONTAINER_SOURCE_ROOT = "/opt/firstcoder-src"
CONTAINER_RUN_ROOT = "/runs"
CONTAINER_PYTHON = "/opt/conda/envs/swebench_matterhorn/bin/python"
CONTAINER_AGENT_VENV = "/opt/firstcoder-agent/.venv"
CONTAINER_AGENT_SOURCE = "/opt/firstcoder-agent/src"
_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True, slots=True)
class DockerRunPlan:
    """All public launch inputs for one isolated ChainSWE container run.

    ``api_key_env`` is intentionally a variable *name*, never a credential
    value.  Keeping the secret outside this object makes accidental logging or
    serialization of it considerably harder.
    """

    chain: ChainSWEChain
    source_root: Path
    chains_path: Path
    run_dir: Path
    provider: str
    provider_name: str
    model: str
    base_url: str
    api_key_env: str
    max_tool_rounds: int | None = None

    @property
    def session_dir(self) -> Path:
        return self.run_dir / "session-data"

    @property
    def summary_path(self) -> Path:
        return self.run_dir / "summary.json"


def build_container_bootstrap() -> str:
    """Return the fixed, non-secret shell bootstrap executed in the image.

    Positional parameters provide all variable values, avoiding interpolation
    of host input into shell source.  ``$5`` is an optional max-tool-rounds
    override; an empty value preserves the runner default.
    """

    return f"""set -eu
python_bin={CONTAINER_PYTHON}
if [ ! -x \"$python_bin\" ]; then
  echo \"ChainSWE image does not contain the expected Python: $python_bin\" >&2
  exit 64
fi

if [ -z \"${{FIRSTCODER_API_KEY:-}}\" ]; then
  FIRSTCODER_API_KEY=\"$(printenv \"$FIRSTCODER_CHAIN_API_KEY_ENV\" || true)\"
  export FIRSTCODER_API_KEY
fi
if [ -z \"${{FIRSTCODER_API_KEY:-}}\" ]; then
  echo \"Configured ChainSWE API-key environment variable is empty or missing.\" >&2
  exit 64
fi

agent_venv={CONTAINER_AGENT_VENV}
if [ ! -x \"$agent_venv/bin/python\" ]; then
  \"$python_bin\" -m venv \"$agent_venv\"
fi
agent_source={CONTAINER_AGENT_SOURCE}
rm -rf \"$agent_source\"
mkdir -p \"$agent_source\"
tar -C {CONTAINER_SOURCE_ROOT} \
  --exclude=.venv \
  --exclude=.git \
  --exclude=runs \
  --exclude='*.egg-info' \
  --exclude=__pycache__ \
  -cf - . | tar -C \"$agent_source\" -xf -
\"$agent_venv/bin/python\" -m pip install --disable-pip-version-check --no-cache-dir \"$agent_source\"

data_root=\"$1\"
summary_out=\"$2\"
provider=\"$3\"
model=\"$4\"
max_tool_rounds=\"${{5:-}}\"
set -- \
  --chain-stdin \
  --workspace /sdk \
  --data-root \"$data_root\" \
  --provider \"$provider\" \
  --model \"$model\" \
  --summary-out \"$summary_out\"
if [ -n \"$max_tool_rounds\" ]; then
  set -- \"$@\" --max-tool-rounds \"$max_tool_rounds\"
fi
exec \"$agent_venv/bin/python\" \"$agent_source/benchmark/chainswe/runner.py\" \"$@\"
"""


def build_docker_command(plan: DockerRunPlan) -> list[str]:
    """Build a Docker command without resolving or embedding credential values."""

    _validate_plan(plan)
    return [
        "docker",
        "run",
        "--rm",
        "-i",
        "--platform",
        "linux/amd64",
        "--workdir",
        "/sdk",
        "--entrypoint",
        "/bin/sh",
        "--mount",
        _bind_mount(plan.source_root, CONTAINER_SOURCE_ROOT, readonly=True),
        "--mount",
        _bind_mount(plan.run_dir, CONTAINER_RUN_ROOT, readonly=False),
        "--env",
        f"FIRSTCODER_PROVIDER={plan.provider}",
        "--env",
        f"FIRSTCODER_PROVIDER_NAME={plan.provider_name}",
        "--env",
        f"FIRSTCODER_MODEL={plan.model}",
        "--env",
        f"FIRSTCODER_BASE_URL={plan.base_url}",
        "--env",
        f"FIRSTCODER_CHAIN_API_KEY_ENV={plan.api_key_env}",
        "--env",
        plan.api_key_env,
        plan.chain.docker_image,
        "-c",
        build_container_bootstrap(),
        "chainswe-docker-launcher",
        f"{CONTAINER_RUN_ROOT}/session-data",
        f"{CONTAINER_RUN_ROOT}/summary.json",
        plan.provider,
        plan.model,
        "" if plan.max_tool_rounds is None else str(plan.max_tool_rounds),
    ]


def prepare_run_directory(plan: DockerRunPlan) -> None:
    """Create an empty, host-visible directory for durable run artifacts."""

    if plan.run_dir.exists():
        if any(plan.run_dir.iterdir()):
            raise ValueError(f"run directory already exists and is not empty: {plan.run_dir}")
    else:
        plan.run_dir.mkdir(parents=True)
    plan.session_dir.mkdir(exist_ok=True)


def run_docker_plan(plan: DockerRunPlan) -> int:
    """Prepare artifacts and run Docker, streaming Docker's normal output."""

    prepare_run_directory(plan)
    completed = subprocess.run(
        build_docker_command(plan),
        input=serialize_chain_for_stdin(plan.chain),
        text=True,
        check=False,
    )
    return completed.returncode


def make_run_plan(
    *,
    chains_path: str | Path,
    chain_id: str = DEFAULT_CHAIN_ID,
    run_dir: str | Path | None = None,
    provider: str = DEFAULT_PROVIDER,
    provider_name: str = DEFAULT_PROVIDER_NAME,
    model: str = DEFAULT_MODEL,
    base_url: str = DEFAULT_BASE_URL,
    api_key_env: str = DEFAULT_API_KEY_ENV,
    max_tool_rounds: int | None = None,
    source_root: str | Path = PROJECT_ROOT,
    environ: Mapping[str, str] | None = None,
) -> DockerRunPlan:
    """Select a chain and validate a launch plan before Docker is contacted."""

    source = Path(source_root).resolve()
    dataset = Path(chains_path).resolve()
    if not source.is_dir() or not (source / "pyproject.toml").is_file():
        raise ValueError(f"FirstCoder source root is not a project directory: {source}")
    if not dataset.is_file():
        raise ValueError(f"ChainSWE JSONL dataset does not exist: {dataset}")
    if not _ENVIRONMENT_NAME.fullmatch(api_key_env):
        raise ValueError(f"api_key_env must be a valid environment variable name: {api_key_env!r}")
    values = os.environ if environ is None else environ
    if not values.get(api_key_env):
        raise ValueError(f"required API-key environment variable is not set: {api_key_env}")
    if max_tool_rounds is not None and max_tool_rounds < 1:
        raise ValueError("max_tool_rounds must be positive")

    chain = select_chain(load_chains_jsonl(dataset), chain_id)
    resolved_run_dir = (
        Path(run_dir).resolve()
        if run_dir is not None
        else _default_run_dir(source, chain.continuous_id)
    )
    plan = DockerRunPlan(
        chain=chain,
        source_root=source,
        chains_path=dataset,
        run_dir=resolved_run_dir,
        provider=_require_nonempty(provider, "provider"),
        provider_name=_require_nonempty(provider_name, "provider_name"),
        model=_require_nonempty(model, "model"),
        base_url=_require_nonempty(base_url, "base_url"),
        api_key_env=api_key_env,
        max_tool_rounds=max_tool_rounds,
    )
    _validate_plan(plan)
    return plan


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one ChainSWE chain in its official Docker image.")
    parser.add_argument("--chains", required=True, help="Path to the local official ChainSWE JSONL dataset.")
    parser.add_argument("--chain-id", default=DEFAULT_CHAIN_ID, help="continuous_id to run.")
    parser.add_argument(
        "--run-dir",
        default=None,
        help="Host directory for summary/session artifacts (default: unique runs/chainswe directory).",
    )
    parser.add_argument("--provider", default=DEFAULT_PROVIDER, help="FirstCoder provider override.")
    parser.add_argument("--provider-name", default=DEFAULT_PROVIDER_NAME, help="OpenAI-compatible provider display name.")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="FirstCoder model override.")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="OpenAI-compatible provider base URL.")
    parser.add_argument(
        "--api-key-env",
        default=DEFAULT_API_KEY_ENV,
        help="Host environment-variable name containing the provider API key.",
    )
    parser.add_argument("--max-tool-rounds", type=int, default=None, help="Optional FirstCoder turn limit override.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        plan = make_run_plan(
            chains_path=args.chains,
            chain_id=args.chain_id,
            run_dir=args.run_dir,
            provider=args.provider,
            provider_name=args.provider_name,
            model=args.model,
            base_url=args.base_url,
            api_key_env=args.api_key_env,
            max_tool_rounds=args.max_tool_rounds,
        )
        exit_code = run_docker_plan(plan)
    except ValueError as exc:
        print(f"ChainSWE Docker launcher error: {exc}", file=sys.stderr)
        return 2

    if exit_code == 0:
        print(f"ChainSWE run artifacts: {plan.run_dir}")
    return exit_code


def _bind_mount(source: Path, target: str, *, readonly: bool) -> str:
    mount = f"type=bind,src={source},dst={target}"
    return f"{mount},readonly" if readonly else mount


def serialize_chain_for_stdin(chain: ChainSWEChain) -> str:
    """Serialize hidden verifier data only for Docker's one-time stdin pipe."""

    import json

    return json.dumps(chain_to_record(chain), ensure_ascii=False)


def _default_run_dir(source_root: Path, chain_id: str) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    safe_chain_id = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in chain_id)
    return source_root / "runs" / "chainswe" / f"{safe_chain_id}-{timestamp}"


def _require_nonempty(value: str, name: str) -> str:
    if not value or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value


def _validate_plan(plan: DockerRunPlan) -> None:
    if not _ENVIRONMENT_NAME.fullmatch(plan.api_key_env):
        raise ValueError(f"api_key_env must be a valid environment variable name: {plan.api_key_env!r}")
    if plan.max_tool_rounds is not None and plan.max_tool_rounds < 1:
        raise ValueError("max_tool_rounds must be positive")


if __name__ == "__main__":
    raise SystemExit(main())
