from pathlib import Path

from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider


def test_readme_provider_scope_matches_current_openai_compatible_mainline() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert OpenAICompatibleProvider.astream is not ChatProvider.astream
    assert AnthropicProvider.astream is ChatProvider.astream

    for keyword in ["OpenAI Chat Completions-compatible", "OpenAI-compatible 流式", "PROMPT_TOO_LONG"]:
        assert keyword in readme
    for keyword in ["实验性", "Anthropic 原生 thinking/cache/streaming"]:
        assert keyword in readme
    for keyword in ["OpenAI Responses API", "reasoning", "多模态"]:
        assert keyword in readme
