"""为 Linux/Python 3.11 Harbor 容器准备 FirstCoder 依赖 wheelhouse。"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path


def project_requirements(pyproject: str | Path) -> list[str]:
    data = tomllib.loads(Path(pyproject).read_text(encoding="utf-8"))
    project = data.get("project") or {}
    build_system = data.get("build-system") or {}
    values = [*build_system.get("requires", []), *project.get("dependencies", [])]
    return list(dict.fromkeys(str(value) for value in values))


def build_download_command(
    *,
    pyproject: str | Path,
    output: str | Path,
    platform: str = "manylinux2014_x86_64",
    python_version: str = "311",
) -> list[str]:
    return [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--dest",
        str(Path(output).expanduser().resolve()),
        "--only-binary=:all:",
        "--platform",
        platform,
        "--implementation",
        "cp",
        "--python-version",
        python_version,
        "--abi",
        f"cp{python_version}",
        *project_requirements(pyproject),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="下载 Harbor Linux 容器可复用的 FirstCoder wheels")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path.home() / ".cache" / "firstcoder-harbor" / "wheelhouse",
    )
    parser.add_argument("--platform", default="manylinux2014_x86_64")
    parser.add_argument("--python-version", default="311")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    command = build_download_command(
        pyproject=args.pyproject,
        output=args.output,
        platform=args.platform,
        python_version=args.python_version,
    )
    if args.dry_run:
        print(subprocess.list2cmdline(command))
        return 0
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
