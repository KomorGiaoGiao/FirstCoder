"""Harbor installed-agent adapter for FirstCoder.

The adapter stages only the local FirstCoder package into each task container,
then runs one non-interactive ``--benchmark`` turn in Harbor's task workdir.
It intentionally does not inspect verifier files or inject benchmark-specific
hints into the task instruction.
"""

from __future__ import annotations

import shutil
import shlex
from pathlib import Path
from typing import Final, override

from harbor.agents.installed.base import BaseInstalledAgent, with_prompt_template
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext


_AGENT_ROOT: Final = "/opt/firstcoder-agent"
_REMOTE_SOURCE_DIR: Final = "/installed-agent/firstcoder-src"
_SESSION_ROOT: Final = "/tmp/firstcoder-harbor-sessions"
_DEFAULT_PACKAGE: Final = (
    "https://github.com/KomorGiaoGiao/FirstCoder/archive/refs/heads/main.zip"
)


class FirstCoderHarborAgent(BaseInstalledAgent):
    """Run the current FirstCoder checkout as a Harbor installed agent.

    ``source_dir`` is deliberately the default installation source.  It lets a
    local benchmark exercise the exact checkout under development, including
    uncommitted prompt or context changes, without uploading the user's whole
    repository, virtualenv, session data, or configuration files.  ``package``
    is an explicit fallback for callers that cannot stage a local checkout.
    """

    def __init__(
        self,
        *args,
        max_tool_rounds: int | str = 90,
        source_dir: str | Path | None = None,
        package: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._max_tool_rounds = _positive_int(max_tool_rounds, "max_tool_rounds")
        self._source_dir = (
            Path(source_dir).expanduser().resolve()
            if source_dir is not None
            else _default_source_dir()
        )
        self._package = package

    @staticmethod
    @override
    def name() -> str:
        return "firstcoder"

    @override
    def get_version_command(self) -> str | None:
        return (
            f"{shlex.quote(_venv_python())} -c "
            "\"from importlib.metadata import version; print(version('firstcoder'))\""
        )

    @override
    async def install(self, environment: BaseEnvironment) -> None:
        """Install FirstCoder into an isolated venv inside the task container."""

        install_spec = await self._prepare_install_spec(environment)
        agent_user = str(environment.default_user or "root")
        quoted_user = shlex.quote(agent_user)
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                f"mkdir -p {shlex.quote(_AGENT_ROOT)} {shlex.quote(_AGENT_ROOT + '/bin')}; "
                f"chown -R {quoted_user}:{quoted_user} {shlex.quote(_AGENT_ROOT)}"
            ),
        )
        await self.exec_as_agent(
            environment,
            command=_install_command(install_spec),
        )

    @with_prompt_template
    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        """Run one FirstCoder benchmark turn in Harbor's configured workdir."""

        del context  # FirstCoder persists its own local benchmark transcript.
        command = self._run_command(instruction, session_id=environment.session_id)
        await self.exec_as_agent(
            environment,
            command=command,
            # Harbor overlays AgentConfig.env after this per-command environment,
            # so users may explicitly override this default in a job config.
            env={"FIRSTCODER_DISABLE_GLOBAL_SKILLS": "1"},
        )

    async def _prepare_install_spec(self, environment: BaseEnvironment) -> str:
        """Stage a minimal local source tree, or return an explicit package spec."""

        if self._package is not None:
            return self._package

        staged = self._stage_local_source()
        await self.exec_as_root(
            environment,
            command=(
                "set -euo pipefail; "
                f"rm -rf {shlex.quote(_REMOTE_SOURCE_DIR)}; "
                f"mkdir -p {shlex.quote(_REMOTE_SOURCE_DIR)}"
            ),
        )
        await environment.upload_dir(staged, _REMOTE_SOURCE_DIR)
        agent_user = str(environment.default_user or "root")
        quoted_user = shlex.quote(agent_user)
        await self.exec_as_root(
            environment,
            command=(
                f"chown -R {quoted_user}:{quoted_user} "
                f"{shlex.quote(_REMOTE_SOURCE_DIR)}"
            ),
        )
        return _REMOTE_SOURCE_DIR

    def _stage_local_source(self) -> Path:
        """Create the minimal host-side package tree copied into a task image."""

        source = self._source_dir
        if source is None:
            raise ValueError(
                "No local FirstCoder source directory is available. Pass "
                "source_dir=... or package=... to FirstCoderHarborAgent."
            )
        package_dir = source / "firstcoder"
        required = (source / "pyproject.toml", source / "README.md", package_dir)
        missing = [str(path) for path in required if not path.exists()]
        if missing:
            raise ValueError(
                "FirstCoder source directory is incomplete; missing " + ", ".join(missing)
            )

        staged = self.logs_dir / "firstcoder-source"
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        shutil.copy2(source / "pyproject.toml", staged / "pyproject.toml")
        shutil.copy2(source / "README.md", staged / "README.md")
        shutil.copytree(package_dir, staged / "firstcoder", ignore=_ignore_source_artifacts)
        return staged

    def _run_command(self, instruction: str, *, session_id: str) -> str:
        """Build the command without provider secrets or verifier information."""

        return (
            f"{shlex.quote(_venv_python())} -m firstcoder "
            "--benchmark --project . "
            f"--data-root {shlex.quote(_SESSION_ROOT)} "
            f"--session-id {shlex.quote(_session_id(session_id))} "
            f"--max-tool-rounds {self._max_tool_rounds} "
            f"--message {shlex.quote(instruction)} "
            "2>&1 | tee /logs/agent/firstcoder.txt"
        )


def _default_source_dir() -> Path | None:
    root = Path(__file__).resolve().parents[2]
    return root if (root / "pyproject.toml").is_file() else None


def _ignore_source_artifacts(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name == "__pycache__" or name.endswith((".pyc", ".pyo"))
    }


def _install_command(install_spec: str) -> str:
    quoted_root = shlex.quote(_AGENT_ROOT)
    quoted_spec = shlex.quote(install_spec)
    return (
        "set -euo pipefail; "
        f"AGENT_ROOT={quoted_root}; "
        'UV_BIN="$AGENT_ROOT/bin/uv"; '
        'if [ ! -x "$UV_BIN" ]; then '
        '  export UV_INSTALL_DIR="$AGENT_ROOT/bin"; '
        "  if command -v curl >/dev/null 2>&1; then "
        "    curl -LsSf https://astral.sh/uv/install.sh | sh -s -- --no-modify-path; "
        "  else "
        "    wget -qO- https://astral.sh/uv/install.sh | sh -s -- --no-modify-path; "
        "  fi; "
        "fi; "
        'PYTHON_BIN=""; '
        'for candidate in python3.12 python3.11 python3; do '
        '  if command -v "$candidate" >/dev/null 2>&1 && '
        '     "$candidate" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"; then '
        '    PYTHON_BIN="$(command -v "$candidate")"; break; '
        '  fi; '
        'done; '
        'if [ -n "$PYTHON_BIN" ]; then '
        '  "$UV_BIN" venv "$AGENT_ROOT/.venv" --python "$PYTHON_BIN" --clear; '
        'else '
        '  "$UV_BIN" venv "$AGENT_ROOT/.venv" --python 3.12 --clear; '
        'fi; '
        f'"$UV_BIN" pip install --python "$AGENT_ROOT/.venv/bin/python" '
        f"--no-cache {quoted_spec}"
    )


def _venv_python() -> str:
    return f"{_AGENT_ROOT}/.venv/bin/python"


def _positive_int(value: int | str, name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _session_id(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return safe or "harbor-task"
