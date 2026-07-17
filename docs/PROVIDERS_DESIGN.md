# Provider Design

[中文版本](PROVIDERS_DESIGN.zh-CN.md)

## Boundary

Providers are protocol adapters. The agent loop speaks FirstCoder's internal
types; each provider converts those types to a vendor request and converts the
response back. This keeps OpenAI SDK shapes, Anthropic blocks, and vendor error
strings out of orchestration code.

## Request/Response Story

```text
ContextBuilder + session registry
  -> ChatRequest(messages, tools, tool_choice, ...)
  -> ChatProvider.complete() or astream()
  -> vendor request
  -> vendor response/chunks
  -> ChatResponse or ChatStreamEvent
  -> AgentLoop and TUI
```

`ChatRequest.tools` is the sole internal carrier of tool definitions. For an
OpenAI-compatible backend it becomes `tools=[{"type":"function", ...}]`; for
Anthropic it becomes `tools=[{"name", "description", "input_schema"}]`.
This is why schemas should not be copied into prompt text.

## Shared Types

`providers/types.py` defines the stable boundary:

| Type | Purpose |
| --- | --- |
| `ChatMessage` | normalized system/user/assistant/tool message |
| `ToolDefinition` / `ToolCall` | definition sent vs call returned |
| `ChatRequest` | all input to an adapter, including native-tool-independent definitions |
| `ChatResponse` | complete normalized result and finish reason |
| `ChatStreamEvent` | normalized text/reasoning/tool-call stream event |
| `ProviderCapabilities` | gates tools, forced selection, streaming, reasoning, etc. |
| `ProviderDiagnostics` / `TokenUsage` | metadata kept separate from assistant content |

`ChatProvider` requires `complete`. Its default async route runs synchronous
completion in a thread; streaming providers override `astream`.

## Configuration and Factory

`load_config` resolves settings and `create_provider_from_config` constructs a
provider. Static presets cover OpenAI, DeepSeek, Qwen, Moonshot, Zhipu,
OpenRouter, Ollama, and Anthropic. There is no runtime provider-plugin registry,
instance cache, or general health-check service today.

Keep provider credentials/base URLs in configured environment or config paths;
do not inspect them in agent-loop code. A runtime `/model` switch rebuilds the
provider and updates the same summarizer used for L4 context compaction.

## OpenAI-Compatible Path

`OpenAICompatibleProvider` builds Chat Completions parameters with:

- projected messages;
- tools only when `supports_tools` is true;
- converted tool choice when the capability permits it;
- configured token parameter and merged extra body;
- optional `stream=True` for the streaming path.

It parses tool calls defensively. Invalid JSON arguments cause the unsafe call
batch to be discarded. If `finish_reason="length"` arrives alongside tool
calls, they are also discarded because parameters may be truncated. This is an
intentional correctness-over-optimism choice.

Streaming chunks are accumulated inside the adapter until a complete tool call
exists; callers receive stable `ChatStreamEvent` values rather than raw SDK
chunks.

## Anthropic Path: Contract Parity With OpenAI-Compatible

`AnthropicProvider` implements the same internal contracts as the OpenAI-compatible
adapter: non-stream `complete`, async `astream` (`text_delta` / tool-call events /
`message_completed`), tools, forced `tool_choice`, parallel-tool gating, usage, and
error classification. It moves system messages to Anthropic's dedicated `system`
field and maps schemas through `input_schema`. Consecutive `tool` messages are
merged into one user `tool_result` block list. Anthropic-only extras such as prompt
caching remain optional and are not required for agent-loop parity.


## Error and Recovery Contract

Adapters classify failures into `ProviderErrorKind` (for example unsupported,
prompt-too-long, auth/configuration, timeout/network, rate limit). The loop can
make policy decisions from this normalized category—most importantly a bounded
context recovery path for prompt-too-long—without parsing every vendor message.

## Verification

```sh
.venv/bin/python -m pytest tests/test_providers.py tests/test_provider_errors.py \
  tests/test_readme_provider_docs.py -q
```

When adding an adapter, fake the vendor client in tests and assert the outgoing
wire parameters, normalized tool response, streaming behavior (if promised),
and error classification. Never require a live API key for core tests.

## Extension Checklist

1. Declare honest capabilities.
2. Convert every shared request field or explicitly reject unsupported fields.
3. Convert system, tool calls, and tool results in both directions.
4. Normalize errors; do not leak SDK-only exceptions past the adapter.
5. Add fake-client tests for malformed and truncated tool calls.

Related: [Architecture](ARCHITECTURE.md), [Tools](TOOLS_DESIGN.md), and [Context Management](CONTEXT_MANAGEMENT_DESIGN.md).
