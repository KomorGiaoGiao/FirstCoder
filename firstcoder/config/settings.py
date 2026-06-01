"""应用配置加载。

当前项目还处在学习和骨架阶段，所以配置系统先保持简单：
只负责从 `.env` 和系统环境变量读取运行所需配置，并把这些配置收拢成
`AppConfig` 对象。这样 provider、agent、UI 后续都可以依赖同一个配置入口，
而不是在各个模块里直接调用 `os.getenv()`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True, slots=True)
class AppConfig:
    """FirstCoder 的应用级配置。

    这里暂时只放 provider 选择和原始环境变量快照。后续如果加入工作区路径、
    日志级别、工具权限开关、TUI 设置等，也应该优先扩展这个对象。
    """

    provider_name: str
    env: dict[str, str]

    def get_env(self, name: str, default: str | None = None) -> str | None:
        """读取配置中的环境变量值。

        provider factory 通过这个方法读取 API key、model、base_url 等字段。
        这样 factory 不需要知道配置来自 `.env`、系统环境变量，还是未来的配置文件。
        """

        return self.env.get(name, default)


def load_config(provider_name: str | None = None) -> AppConfig:
    """从 `.env` 和系统环境变量加载应用配置。

    provider 选择优先级：
    1. 函数参数 `provider_name`
    2. 环境变量 `FIRSTCODER_PROVIDER`
    3. 默认 `openai`

    这个函数只做“读取和收拢配置”，不负责判断 provider 是否支持，也不负责校验
    API key 是否存在；这些 provider 相关规则仍然交给 provider factory。
    """

    load_dotenv()

    selected_provider = (provider_name or os.getenv("FIRSTCODER_PROVIDER") or "openai").lower()

    return AppConfig(
        provider_name=selected_provider,
        env=dict(os.environ),
    )
