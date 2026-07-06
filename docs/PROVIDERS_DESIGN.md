# Providers Design

[中文版本](PROVIDERS_DESIGN.zh-CN.md)

## Overview

The provider layer isolates the agent loop from vendor-specific APIs. FirstCoder normalizes provider requests and responses through shared internal types, then adapts them to the wire format of each backend.

The current implementation supports two provider families:

- OpenAI-compatible chat completions
- an experimental Anthropic provider

## Key Files

- `firstcoder/providers/base.py`: `ChatProvider` abstract base class
- `firstcoder/providers/types.py`: shared request, response, stream, usage, and capability types
- `firstcoder/providers/factory.py`: provider creation from resolved config
- `firstcoder/providers/presets.py`: static provider presets
- `firstcoder/providers/openai_compatible.py`: OpenAI-compatible implementation
- `firstcoder/providers/anthropic_provider.py`: experimental Anthropic implementation
- `firstcoder/providers/tool_adapters.py`: provider-specific tool schema conversion
- `firstcoder/providers/errors.py`: provider error taxonomy

## Core Abstraction

`ChatProvider` is an abstract base class, not a protocol. The essential interface is:

- `name`
- `model`
- `complete(request)`

Optional wrappers include:

- `acomplete(request)`
- `astream(request)`

The default async completion path wraps the synchronous provider call with `asyncio.to_thread(...)`. Providers that support streaming override `astream(...)`.

## Shared Types

The provider layer uses concrete shared dataclasses from `firstcoder/providers/types.py`.

Important ones are:

- `ChatMessage`
- `ToolDefinition`
- `ToolCall`
- `ChatRequest`
- `ChatResponse`
- `ChatStreamEvent`
- `ProviderCapabilities`
- `ProviderDiagnostics`
- `TokenUsage`

This normalization allows the rest of the runtime to work without branching on vendor-specific response formats.

## Provider Factory

Provider creation is function-based, not class-based.

The real construction flow is:

1. resolve config through `load_config(...)`
2. call `create_provider_from_config(config)`
3. select either:
   - an OpenAI-compatible preset or custom endpoint
   - the Anthropic provider

There is currently:

- no dynamic provider plugin registry
- no provider instance cache
- no separate health-check subsystem

Supported static presets are defined in `firstcoder/providers/presets.py` and include:

- `openai`
- `deepseek`
- `qwen`
- `moonshot`
- `zhipu`
- `openrouter`
- `ollama`
- `anthropic`

## OpenAI-Compatible Provider

`firstcoder/providers/openai_compatible.py` is the mainline implementation.

Key behavior:

- uses the `openai` Python SDK
- translates internal `ChatRequest` into Chat Completions parameters
- sends tools only when provider capabilities allow them
- supports streaming
- merges preset-level and request-level `extra_body`
- parses tool calls defensively

Notable defensive behavior:

- invalid tool-call argument JSON causes the tool-call batch to be dropped
- `finish_reason="length"` combined with tool calls is treated conservatively

Streaming is bridged into internal `ChatStreamEvent` values so the TUI and loop can handle streaming uniformly.

## Anthropic Provider

`firstcoder/providers/anthropic_provider.py` is explicitly experimental.

Current behavior:

- supports normal completion
- supports tool use in a limited form
- extracts system prompts into Anthropic's separate `system` field
- does not currently implement the same full streaming path as the OpenAI-compatible provider

It should be treated as a narrower adapter than the OpenAI-compatible path.

## Tool Adaptation

Providers do not consume tool executors directly. They receive model-visible tool schemas through adapter functions in `firstcoder/providers/tool_adapters.py`.

The real flow is:

1. tools are exposed as internal `ToolDefinition`
2. the provider adapter converts them to provider-native schema
3. tool calls return to the agent loop as normalized internal `ToolCall` values

This keeps provider-specific formatting out of the agent loop.

## Error Model

Provider errors are represented by `ProviderError` and `ProviderErrorKind` in `firstcoder/providers/errors.py`.

The error model covers cases such as:

- unsupported features
- prompt too long
- auth/configuration failures
- timeouts and network errors
- rate limiting

This matters because prompt-too-long failures can trigger context compaction and retry behavior in the agent loop.

## Design Notes

- Provider capabilities are explicit and are used to gate tool exposure and streaming behavior.
- The provider layer is richer in diagnostics than a minimal “request in / text out” adapter.
- Extensibility today is static-factory based, not plugin-registry based.
