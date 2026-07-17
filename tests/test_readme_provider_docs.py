from pathlib import Path

from firstcoder.providers.anthropic_provider import AnthropicProvider
from firstcoder.providers.base import ChatProvider
from firstcoder.providers.openai_compatible import OpenAICompatibleProvider


def test_readme_provider_scope_matches_current_openai_compatible_mainline() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert OpenAICompatibleProvider.astream is not ChatProvider.astream
    assert AnthropicProvider.astream is not ChatProvider.astream

    for keyword in [
        "OpenAI Chat Completions-compatible",
        "Anthropic Messages API",
        "tool_choice",
        "PROMPT_TOO_LONG",
        "OpenAI Responses API",
        "multimodal",
    ]:
        assert keyword in readme
