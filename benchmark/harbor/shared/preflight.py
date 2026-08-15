"""Harbor 本地运行预检，不输出 provider 凭据值。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterable


REQUIRED_PROVIDER_VARIABLES = (
    "FIRSTCODER_PROVIDER",
    "FIRSTCODER_PROVIDER_NAME",
    "FIRSTCODER_MODEL",
    "FIRSTCODER_BASE_URL",
    "FIRSTCODER_API_KEY",
)
PLACEHOLDER_MARKERS = ("replace-with", "your-provider", "your-model", "provider.example")


@dataclass(frozen=True, slots=True)
class PreflightCheck:
    name: str
    status: str
    message: str


@dataclass(slots=True)
class PreflightReport:
    checks: list[PreflightCheck]

    @property
    def ok(self) -> bool:
        return all(check.status != "fail" for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": [asdict(check) for check in self.checks]}


def load_env_file(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    env_path = Path(path)
    if not env_path.is_file():
        return values
    for raw_line in env_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if name:
            values[name] = value
    return values


def build_mounts(
    cache_dir: str | Path,
    wheelhouse_dir: str | Path | None = None,
) -> list[dict[str, object]]:
    mounts: list[dict[str, object]] = [
        {
            "type": "bind",
            "source": str(Path(cache_dir).expanduser().resolve()),
            "target": "/opt/firstcoder-cache",
        }
    ]
    if wheelhouse_dir is not None:
        mounts.append(
            {
                "type": "bind",
                "source": str(Path(wheelhouse_dir).expanduser().resolve()),
                "target": "/opt/firstcoder-wheelhouse",
                "read_only": True,
            }
        )
    return mounts


def run_preflight(
    *,
    env_file: str | Path,
    cache_dir: str | Path,
    model_override: str | None = None,
    wheelhouse_dir: str | Path | None = None,
    wheelhouse_only: bool = False,
    images: Iterable[str] = (),
    pull_images: bool = False,
    require_images: bool = False,
    probe_network: bool = True,
    timeout_seconds: float = 15.0,
    command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    url_opener: Callable[..., object] = urllib.request.urlopen,
) -> PreflightReport:
    checks: list[PreflightCheck] = []
    env_path = Path(env_file)
    values = load_env_file(env_path)
    for name in REQUIRED_PROVIDER_VARIABLES:
        if name not in values and os.environ.get(name):
            values[name] = os.environ[name]

    checks.append(
        PreflightCheck(
            "env_file",
            "pass" if env_path.is_file() else "fail",
            "已读取专用 Harbor 环境文件。" if env_path.is_file() else "专用 Harbor 环境文件不存在。",
        )
    )
    missing = [name for name in REQUIRED_PROVIDER_VARIABLES if not _configured_value(values.get(name))]
    checks.append(
        PreflightCheck(
            "provider_variables",
            "pass" if not missing else "fail",
            "所需 provider 变量均已配置。" if not missing else "缺少或仍为占位值：" + ", ".join(missing),
        )
    )

    docker_ok, docker_message = _docker_version(command_runner, timeout_seconds)
    checks.append(PreflightCheck("docker", "pass" if docker_ok else "fail", docker_message))

    cache_ok, cache_message = _writable_directory(Path(cache_dir))
    checks.append(PreflightCheck("cache", "pass" if cache_ok else "fail", cache_message))

    wheelhouse_status, wheelhouse_message = _wheelhouse_check(
        Path(wheelhouse_dir) if wheelhouse_dir is not None else None,
        required=wheelhouse_only,
    )
    checks.append(PreflightCheck("wheelhouse", wheelhouse_status, wheelhouse_message))

    if probe_network:
        base_url = values.get("FIRSTCODER_BASE_URL")
        if _configured_value(base_url):
            status, message = _probe_provider_endpoint(str(base_url), timeout_seconds, url_opener)
        else:
            status, message = "fail", "无法探测 provider：基础地址未配置。"
        checks.append(PreflightCheck("provider_network", status, message))
        api_key = values.get("FIRSTCODER_API_KEY")
        model = model_override or values.get("FIRSTCODER_MODEL")
        if _configured_value(base_url) and _configured_value(api_key) and _configured_value(model):
            status, message = _probe_provider_model(
                str(base_url),
                str(api_key),
                str(model),
                timeout_seconds,
                url_opener,
            )
        else:
            status, message = "fail", "无法确认 provider 模型：配置不完整。"
        checks.append(PreflightCheck("provider_model", status, message))

    if docker_ok:
        for image in dict.fromkeys(item.strip() for item in images if item.strip()):
            status, message = _check_image(
                image,
                pull=pull_images,
                required=require_images,
                timeout_seconds=timeout_seconds,
                command_runner=command_runner,
            )
            checks.append(PreflightCheck(f"image:{image}", status, message))
    return PreflightReport(checks)


def render_report(report: PreflightReport) -> str:
    labels = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
    lines = [f"[{labels[check.status]}] {check.name}: {check.message}" for check in report.checks]
    lines.append("预检通过。" if report.ok else "预检失败；请先处理 FAIL 项。")
    return "\n".join(lines) + "\n"


def read_image_file(path: str | Path | None) -> list[str]:
    if path is None or not Path(path).is_file():
        return []
    return [
        line.strip()
        for line in Path(path).read_text(encoding="utf-8-sig").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def _docker_version(command_runner, timeout_seconds: float) -> tuple[bool, str]:
    try:
        result = command_runner(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return False, f"Docker 不可用：{type(exc).__name__}。"
    version = (result.stdout or "").strip()
    if result.returncode != 0 or not version:
        return False, "Docker CLI 存在，但 daemon/version 检查失败。"
    return True, f"Docker daemon 可用，Server {version}。"


def _writable_directory(path: Path) -> tuple[bool, str]:
    try:
        path = path.expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(prefix="firstcoder-preflight-", dir=path, delete=True):
            pass
    except OSError as exc:
        return False, f"cache 目录不可写：{type(exc).__name__}。"
    return True, f"cache 目录可写：{path}"


def _wheelhouse_check(path: Path | None, *, required: bool) -> tuple[str, str]:
    if path is None:
        return ("fail" if required else "warn"), "未配置 wheelhouse；将依赖共享下载 cache。"
    path = path.expanduser().resolve()
    if not path.is_dir():
        return ("fail" if required else "warn"), f"wheelhouse 目录不存在：{path}"
    count = sum(item.is_file() for item in path.iterdir())
    if count == 0:
        return ("fail" if required else "warn"), f"wheelhouse 为空：{path}"
    return "pass", f"wheelhouse 可用，共 {count} 个文件：{path}"


def _probe_provider_endpoint(base_url: str, timeout_seconds: float, url_opener) -> tuple[str, str]:
    request = urllib.request.Request(base_url.rstrip("/") + "/", method="HEAD")
    try:
        response = url_opener(request, timeout=timeout_seconds)
        close = getattr(response, "close", None)
        if callable(close):
            close()
        return "pass", "provider endpoint 可建立 HTTP 连接。"
    except urllib.error.HTTPError as exc:
        return ("warn" if exc.code >= 500 else "pass"), f"provider endpoint 已响应 HTTP {exc.code}。"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "fail", f"provider endpoint 连接失败：{type(exc).__name__}。"


def _probe_provider_model(
    base_url: str,
    api_key: str,
    model: str,
    timeout_seconds: float,
    url_opener,
) -> tuple[str, str]:
    request = urllib.request.Request(
        base_url.rstrip("/") + "/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        response = url_opener(request, timeout=timeout_seconds)
        try:
            payload = json.loads(response.read().decode("utf-8"))
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
    except urllib.error.HTTPError as exc:
        if exc.code in {404, 405}:
            return "warn", "provider 未提供可用的模型目录；无法确认模型是否存在。"
        return ("fail" if exc.code in {401, 403} else "warn"), f"provider 模型目录响应 HTTP {exc.code}。"
    except (json.JSONDecodeError, UnicodeDecodeError, AttributeError, TypeError):
        return "warn", "provider 模型目录响应无法解析；无法确认模型是否存在。"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return "warn", f"provider 模型目录连接失败：{type(exc).__name__}。"

    model_ids = _model_ids(payload)
    if not model_ids:
        return "warn", "provider 模型目录为空或格式未知；无法确认模型是否存在。"
    if model not in model_ids:
        return "fail", f"provider 模型目录不包含配置模型：{model}。"
    return "pass", f"provider 模型目录包含配置模型：{model}。"


def _model_ids(payload: object) -> set[str]:
    items = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        return set()
    model_ids: set[str] = set()
    for item in items:
        if isinstance(item, str) and item:
            model_ids.add(item)
            continue
        if not isinstance(item, dict):
            continue
        value = item.get("id") or item.get("name") or item.get("model")
        if value:
            model_ids.add(str(value))
    return model_ids


def _check_image(
    image: str,
    *,
    pull: bool,
    required: bool,
    timeout_seconds: float,
    command_runner,
) -> tuple[str, str]:
    try:
        inspect = command_runner(
            ["docker", "image", "inspect", image],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return "fail", f"镜像检查失败：{type(exc).__name__}。"
    if inspect.returncode == 0:
        return "pass", "镜像已在本地。"
    if not pull:
        return ("fail" if required else "warn"), "镜像尚未在本地；可使用 --pull-images 预拉。"
    try:
        pulled = command_runner(
            ["docker", "pull", image],
            capture_output=True,
            text=True,
            timeout=max(timeout_seconds, 900),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "fail", "镜像预拉超时。"
    return ("pass", "镜像预拉完成。") if pulled.returncode == 0 else ("fail", "镜像预拉失败。")


def _configured_value(value: str | None) -> bool:
    if value is None or not value.strip():
        return False
    normalized = value.strip().lower()
    return not any(marker in normalized for marker in PLACEHOLDER_MARKERS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FirstCoder Harbor 运行预检")
    parser.add_argument("--env-file", type=Path, default=Path(".env.harbor"))
    parser.add_argument("--cache-dir", type=Path, default=Path.home() / ".cache" / "firstcoder-harbor")
    parser.add_argument("--model")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--wheelhouse-only", action="store_true")
    parser.add_argument("--image", action="append", default=[])
    parser.add_argument("--image-file", type=Path)
    parser.add_argument("--pull-images", action="store_true")
    parser.add_argument("--require-images", action="store_true")
    parser.add_argument("--skip-network", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--print-mounts", action="store_true")
    args = parser.parse_args(argv)
    if args.print_mounts:
        print(json.dumps(build_mounts(args.cache_dir, args.wheelhouse), ensure_ascii=False))
        return 0
    report = run_preflight(
        env_file=args.env_file,
        cache_dir=args.cache_dir,
        model_override=args.model,
        wheelhouse_dir=args.wheelhouse,
        wheelhouse_only=args.wheelhouse_only,
        images=[*args.image, *read_image_file(args.image_file)],
        pull_images=args.pull_images,
        require_images=args.require_images,
        probe_network=not args.skip_network,
    )
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) if args.json else render_report(report), end="\n" if args.json else "")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
