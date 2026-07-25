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
# Download cache for pip/uv. Bind-mount a host directory here (Harbor
# ``--mounts``) so FirstCoder's dependencies download once and are reused
# across trials and containers instead of being fetched for every task.
_CACHE_DIR: Final = "/opt/firstcoder-cache"
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
        reasoning_effort: str | None = None,
        source_dir: str | Path | None = None,
        package: str | None = None,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._max_tool_rounds = _positive_int(max_tool_rounds, "max_tool_rounds")
        self._reasoning_effort = _optional_nonblank(reasoning_effort, "reasoning_effort")
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
                f"mkdir -p {shlex.quote(_AGENT_ROOT)} {shlex.quote(_AGENT_ROOT + '/bin')} "
                f"{shlex.quote(_CACHE_DIR)}; "
                f"chown -R {quoted_user}:{quoted_user} {shlex.quote(_AGENT_ROOT)}; "
                # The cache may be a shared bind mount; only fix ownership of the
                # mount point itself so a pre-populated host cache is left intact.
                f"chown {quoted_user}:{quoted_user} {shlex.quote(_CACHE_DIR)}"
            ),
        )
        await self.exec_as_root(
            environment,
            command=self._python_setup_command(),
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
        """运行 agent 并将会话副本导出为 Harbor 可收集的日志文件。"""

        safe_session_id = _session_id(session_id)
        session_path = f"{_SESSION_ROOT}/sessions/{safe_session_id}.jsonl"

        effort = f"--reasoning-effort {shlex.quote(self._reasoning_effort)} " if self._reasoning_effort else ""
        return (
            "set -o pipefail; "
            f"{shlex.quote(_venv_python())} -m firstcoder "
            "--benchmark --project . "
            f"--data-root {shlex.quote(_SESSION_ROOT)} "
            f"--session-id {shlex.quote(safe_session_id)} "
            f"--max-tool-rounds {self._max_tool_rounds} "
            f"{effort}"
            f"--message {shlex.quote(instruction)} "
            "2>&1 | tee /logs/agent/firstcoder.txt; "
            'FIRSTCODER_EXIT="${PIPESTATUS[0]}"; '
            f"if [ -f {shlex.quote(session_path)} ]; then "
            f"  if ! cp {shlex.quote(session_path)} /logs/agent/firstcoder-session.jsonl; then "
            '    echo "warning: failed to export FirstCoder benchmark session" >&2; '
            "  fi; "
            "fi; "
            'exit "$FIRSTCODER_EXIT"'
        )

    @staticmethod
    def _python_setup_command() -> str:
        """Ensure FirstCoder has an isolated Python 3.11+ runtime across task images."""

        return """set -euo pipefail
AGENT_ROOT=/opt/firstcoder-agent
find_python() {
  for candidate in python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \\
       "$candidate" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"; then
      command -v "$candidate"
      return 0
    fi
  done
  return 1
}
has_venv() {
  venv_probe="$(mktemp -d)"
  "$1" -m venv "$venv_probe/test-venv" >/dev/null 2>&1 && \\
    "$venv_probe/test-venv/bin/python" -m pip --version >/dev/null 2>&1
  status=$?
  rm -rf "$venv_probe"
  return "$status"
}
# System package installs share upstream mirrors across concurrent trials, so
# retry with backoff to absorb transient apt/apk/dnf failures (e.g. apt exit 100).
retry() {
  attempt=1
  until "$@"; do
    if [ "$attempt" -ge 3 ]; then
      echo "FirstCoder Harbor agent: command failed after $attempt attempts: $*" >&2
      return 1
    fi
    echo "FirstCoder Harbor agent: attempt $attempt failed, retrying: $*" >&2
    sleep "$((attempt * 5))"
    attempt=$((attempt + 1))
  done
}
PYTHON_BIN="$(find_python || true)"
PACKAGE_MANAGER="unsupported"
if command -v apt-get >/dev/null 2>&1; then
  PACKAGE_MANAGER="apt-get"
elif command -v apk >/dev/null 2>&1; then
  PACKAGE_MANAGER="apk"
elif command -v dnf >/dev/null 2>&1; then
  PACKAGE_MANAGER="dnf"
elif command -v yum >/dev/null 2>&1; then
  PACKAGE_MANAGER="yum"
fi
install_system_python() {
  case "$PACKAGE_MANAGER" in
    apt-get)
      retry sh -c 'apt-get update && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates'
      ;;
    apk)
      retry apk add --no-cache python3 py3-pip
      ;;
    dnf)
      retry dnf install -y python3 python3-pip
      ;;
    yum)
      retry yum install -y python3 python3-pip
      ;;
    *)
      echo "FirstCoder Harbor agent cannot bootstrap Python 3.11 or newer: no supported package manager is available." >&2
      exit 64
      ;;
  esac
}
install_python_venv() {
  case "$PACKAGE_MANAGER" in
    apt-get)
      retry sh -c 'apt-get update && apt-get install -y --no-install-recommends python3-venv'
      ;;
    apk)
      retry apk add --no-cache py3-pip
      ;;
    dnf)
      retry dnf install -y python3-pip
      ;;
    yum)
      retry yum install -y python3-pip
      ;;
    *)
      return 1
      ;;
  esac
}
if [ -n "$PYTHON_BIN" ] && ! has_venv "$PYTHON_BIN"; then
  if install_python_venv && has_venv "$PYTHON_BIN"; then
    exit 0
  fi
fi
if [ -n "$PYTHON_BIN" ] && has_venv "$PYTHON_BIN"; then
  exit 0
fi
if [ -z "$PYTHON_BIN" ] && ! command -v python3 >/dev/null 2>&1; then
  install_system_python
  PYTHON_BIN="$(find_python || true)"
  if [ -n "$PYTHON_BIN" ] && has_venv "$PYTHON_BIN"; then
    exit 0
  fi
fi
install_uv() {
  if [ -x "$AGENT_ROOT/bin/uv" ]; then
    return 0
  fi
  if command -v curl >/dev/null 2>&1; then
    retry sh -c 'curl -LsSf https://astral.sh/uv/install.sh | UV_UNMANAGED_INSTALL="'"$AGENT_ROOT"'/bin" sh'
  elif command -v wget >/dev/null 2>&1; then
    retry sh -c 'wget -qO- https://astral.sh/uv/install.sh | UV_UNMANAGED_INSTALL="'"$AGENT_ROOT"'/bin" sh'
  else
    echo "FirstCoder Harbor agent needs curl or wget to install uv." >&2
    exit 64
  fi
}
install_uv
retry "$AGENT_ROOT/bin/uv" python install 3.11
PYTHON_BIN="$("$AGENT_ROOT/bin/uv" python find 3.11)"
if [ -z "$PYTHON_BIN" ] || ! has_venv "$PYTHON_BIN"; then
  echo "FirstCoder Harbor agent requires Python 3.11+ with venv and pip after bootstrap." >&2
  exit 64
fi
"""


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
    quoted_cache = shlex.quote(_CACHE_DIR)
    quoted_spec = shlex.quote(install_spec)
    return (
        "set -euo pipefail; "
        f"AGENT_ROOT={quoted_root}; "
        f"CACHE_DIR={quoted_cache}; "
        'UV_BIN="$AGENT_ROOT/bin/uv"; '
        'PYTHON_BIN=""; '
        'if [ -x "$UV_BIN" ]; then PYTHON_BIN="$("$UV_BIN" python find 3.11)"; fi; '
        'for candidate in python3.12 python3.11 python3; do '
        '  [ -n "$PYTHON_BIN" ] && break; '
        '  if command -v "$candidate" >/dev/null 2>&1 && '
        '     "$candidate" -c "import sys; raise SystemExit(sys.version_info < (3, 11))"; then '
        '    PYTHON_BIN="$(command -v "$candidate")"; break; '
        '  fi; '
        'done; '
        'if [ -z "$PYTHON_BIN" ]; then '
        '  echo "FirstCoder Harbor agent requires Python 3.11 or newer in the task image." >&2; exit 64; '
        'fi; '
        # The download cache is shared across concurrent trials; retry the
        # install with backoff so a single flaky fetch does not error the trial.
        'mkdir -p "$CACHE_DIR"; '
        'install_deps() { '
        '  if [ -x "$UV_BIN" ]; then '
        '    "$UV_BIN" venv "$AGENT_ROOT/.venv" --python "$PYTHON_BIN" --clear; '
        '    "$UV_BIN" pip install --python "$AGENT_ROOT/.venv/bin/python" --cache-dir "$CACHE_DIR" '
        f"{quoted_spec}; "
        '  else '
        '    "$PYTHON_BIN" -m venv "$AGENT_ROOT/.venv" --clear; '
        '    "$AGENT_ROOT/.venv/bin/python" -m pip install --cache-dir "$CACHE_DIR" '
        f"{quoted_spec}; "
        '  fi; '
        '}; '
        'attempt=1; '
        'until install_deps; do '
        '  if [ "$attempt" -ge 3 ]; then '
        '    echo "FirstCoder Harbor agent failed to install dependencies after $attempt attempts." >&2; '
        '    exit 1; '
        '  fi; '
        '  echo "FirstCoder dependency install attempt $attempt failed; retrying." >&2; '
        '  sleep "$((attempt * 5))"; '
        '  attempt=$((attempt + 1)); '
        'done'
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


def _optional_nonblank(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    return value.strip()


def _session_id(value: str) -> str:
    safe = "".join(char if char.isalnum() or char in "._-" else "_" for char in value)
    return safe or "harbor-task"
