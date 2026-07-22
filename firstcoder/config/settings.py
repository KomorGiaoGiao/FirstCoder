"""应用配置加载。

配置层是 FirstCoder 的统一入口：CLI、provider factory、TUI 都应该通过这里拿到
运行配置，而不是在各处直接读取文件或环境变量。
"""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tomllib

from dotenv import load_dotenv

from firstcoder.config.models import ModelCatalog, build_model_catalog

PROJECT_CONFIG_NAME = "firstcoder.toml"


@dataclass(frozen=True, slots=True)
class AppConfig:
    """FirstCoder 的应用级配置。

    `env` 保留 FIRSTCODER_* 和厂商环境变量；`project_config` / `global_config`
    承载 TOML 配置。provider factory 只依赖这个对象的方法，不需要知道配置来自哪里。
    """

    provider_name: str
    env: dict[str, str]
    project_config: dict[str, Any] | None = None
    global_config: dict[str, Any] | None = None
    project_config_path: Path | None = None
    global_config_path: Path | None = None

    def get_env(self, name: str, default: str | None = None) -> str | None:
        """读取配置中的环境变量值。

        这个方法保留给已有 provider 代码和测试；新增配置优先使用
        `get_provider_value()`。
        """

        return self.env.get(name, default)

    def get_provider_value(
        self,
        name: str,
        *,
        env: str | None = None,
        default: str | None = None,
        provider_name: str | None = None,
    ) -> str | None:
        """按 FirstCoder 配置优先级读取 provider 字段。

        优先级：环境变量 / `.env` > 项目 `firstcoder.toml` > 全局配置 > 默认值。
        """

        if env:
            env_value = self.get_env(env)
            if env_value:
                return env_value
        value = self._provider_config_raw_value(name, provider_name=provider_name)
        return str(value) if value is not None else default

    def get_provider_bool(
        self,
        name: str,
        *,
        env: str | None = None,
        default: bool | None = None,
        provider_name: str | None = None,
    ) -> bool | None:
        """按配置优先级读取 provider 布尔字段。"""

        if env:
            env_value = self.get_env(env)
            if env_value:
                return _bool_value_from_raw(env_value)
        value = self._provider_config_raw_value(name, provider_name=provider_name)
        if value is not None:
            return _bool_value_from_raw(value)
        return default

    def get_config_value(self, name: str, *, default: str | None = None) -> str | None:
        """读取顶层配置字段，项目配置覆盖全局配置。"""

        for config in (self.project_config, self.global_config):
            value = _string_value(config, name)
            if value is not None:
                return value
        return default

    def mcp_config(self) -> dict[str, Any]:
        """返回按服务器名合并的原始 MCP 配置，项目配置完整覆盖同名全局配置。"""

        merged: dict[str, Any] = {}
        for config in (self.global_config, self.project_config):
            if not config or "mcp" not in config:
                continue
            raw_mcp = config["mcp"]
            if not isinstance(raw_mcp, dict):
                raise ValueError("[mcp] 配置必须是表")
            for name, server_config in raw_mcp.items():
                merged[name] = deepcopy(server_config)
        return merged

    def model_catalog(self) -> ModelCatalog:
        """返回合并后的多模型目录。"""

        return build_model_catalog(
            global_config=self.global_config,
            project_config=self.project_config,
        )

    @property
    def loaded_config_paths(self) -> list[Path]:
        """已经存在并被加载的配置文件路径。"""

        return [path for path in (self.global_config_path, self.project_config_path) if path is not None]

    def _provider_config_raw_value(
        self,
        name: str,
        *,
        provider_name: str | None,
    ) -> Any | None:
        for config in (self.project_config, self.global_config):
            value = _provider_raw_value(config, name, provider_name=provider_name or self.provider_name)
            if value is not None:
                return value
        return None


def load_config(
    provider_name: str | None = None,
    *,
    project_root: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> AppConfig:
    """从配置文件、`.env` 和系统环境变量加载应用配置。

    provider 选择优先级：
    1. 函数参数 `provider_name`
    2. 环境变量 / `.env` 的 `FIRSTCODER_PROVIDER`
    3. 项目 `firstcoder.toml`
    4. 全局配置
    5. 默认 `openai`

    这个函数只做“读取和收拢配置”，不负责判断 provider 是否支持，也不负责校验
    API key 是否存在；这些 provider 相关规则仍然交给 provider factory。
    """

    load_dotenv()
    env_snapshot = dict(os.environ if env is None else env)
    root = Path(project_root or os.getcwd()).resolve()
    global_path = default_global_config_path()
    project_path = root / PROJECT_CONFIG_NAME
    global_config = _read_toml_file(global_path)
    project_config = _read_toml_file(project_path)

    provider_override = provider_name or env_snapshot.get("FIRSTCODER_PROVIDER")
    selected_provider = provider_override.lower() if provider_override else _provider_name_from_config(project_config) or _provider_name_from_config(global_config) or "openai"

    return AppConfig(
        provider_name=selected_provider,
        env=env_snapshot,
        project_config=project_config,
        global_config=global_config,
        project_config_path=project_path if project_config is not None else None,
        global_config_path=global_path if global_config is not None else None,
    )


def default_global_config_path() -> Path:
    """返回当前平台的全局 FirstCoder 配置路径。"""

    config_home = os.getenv("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "firstcoder" / "config.toml"
    return Path.home() / ".config" / "firstcoder" / "config.toml"


def project_config_path(project_root: Path | str | None = None) -> Path:
    """返回项目级配置文件路径。"""

    return Path(project_root or os.getcwd()).resolve() / PROJECT_CONFIG_NAME


def render_default_config() -> str:
    """生成可直接写入全局配置的默认模板。"""

    return "\n".join(
        [
            '# FirstCoder global configuration. Project-level "./firstcoder.toml" can override it.',
            'default_model = "yurenapi/gpt-5.5"',
            "",
            "[providers.yurenapi]",
            'type = "openai-compatible"',
            'base_url = "https://yurenapi.cn/v1"',
            'api_key_env = "YURENAPI_API_KEY"',
            "parallel_tool_calls = true",
            "",
            '[models."yurenapi/gpt-5.5"]',
            'label = "GPT-5.5"',
            "",
            "[permissions]",
            'mode = "ask"',
            "",
            "[ui]",
            'theme = "default"',
            "",
        ]
    )


def _read_toml_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data


def _provider_name_from_config(config: dict[str, Any] | None) -> str | None:
    default_model = _string_value(config, "default_model")
    if default_model and "/" in default_model:
        return default_model.split("/", 1)[0]
    return None


def _provider_raw_value(config: dict[str, Any] | None, name: str, *, provider_name: str | None) -> Any | None:
    if not config or not provider_name:
        return None
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return None
    provider = providers.get(provider_name)
    return provider.get(name) if isinstance(provider, dict) else None


def _string_value(config: dict[str, Any] | None, name: str) -> str | None:
    if not config:
        return None
    value = config.get(name)
    if value is None:
        return None
    return str(value)


def _bool_value_from_raw(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法解析布尔配置值：{value}")
