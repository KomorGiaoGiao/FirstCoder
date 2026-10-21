"""pytest 公共配置。

当前工作区只保留 provider、tool、config 和 utils 的可运行骨架。
旧 agent/memory/context 实现已经删除，后续按新计划重新实现时再补对应测试。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from firstcoder.providers.base import ChatProvider
from firstcoder.providers.cassette import CassetteProvider, resolve_cassette_path

CASSETTE_ROOT = Path(__file__).parent / "fixtures" / "cassettes"


@pytest.fixture
def cassette_provider():
    """cassette provider 工厂：默认目录 ``tests/fixtures/cassettes/``。

    mode 为 None 时由 ``CassetteProvider`` 读 ``FIRSTCODER_CASSETTE_MODE``
    （默认 auto）。测试结束自动 ``close()``：record 落盘，replay 检查未消费交互。
    """

    providers: list[CassetteProvider] = []

    def factory(name: str, inner: ChatProvider | None = None, mode=None) -> CassetteProvider:
        provider = CassetteProvider(inner=inner, path=resolve_cassette_path(CASSETTE_ROOT, name), mode=mode)
        providers.append(provider)
        return provider

    yield factory

    for provider in providers:
        provider.close()

