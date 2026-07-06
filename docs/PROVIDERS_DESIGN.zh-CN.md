# Providers 设计

[English Version](PROVIDERS_DESIGN.md)

## 概述

provider 层负责把 agent loop 和具体厂商 API 隔离开。FirstCoder 先把请求和响应规范化成一套内部类型，再由适配器转换成不同后端的实际协议。

当前实现主要支持两类 provider：

- OpenAI-compatible chat completions
- 实验性的 Anthropic provider

## 关键文件

- `firstcoder/providers/base.py`：`ChatProvider` 抽象基类
- `firstcoder/providers/types.py`：共享的 request、response、stream、usage 和 capability 类型
- `firstcoder/providers/factory.py`：根据解析后的配置创建 provider
- `firstcoder/providers/presets.py`：静态 provider presets
- `firstcoder/providers/openai_compatible.py`：OpenAI-compatible 实现
- `firstcoder/providers/anthropic_provider.py`：实验性的 Anthropic 实现
- `firstcoder/providers/tool_adapters.py`：provider 侧的工具 schema 转换
- `firstcoder/providers/errors.py`：provider 错误模型

## 核心抽象

`ChatProvider` 当前是抽象基类，不是 protocol。核心接口包括：

- `name`
- `model`
- `complete(request)`

可选异步包装包括：

- `acomplete(request)`
- `astream(request)`

默认异步 completion 路径通过 `asyncio.to_thread(...)` 包装同步 provider 调用。支持流式输出的 provider 会自行实现 `astream(...)`。

## 共享类型

provider 层使用 `firstcoder/providers/types.py` 里的具体 dataclass。

关键类型包括：

- `ChatMessage`
- `ToolDefinition`
- `ToolCall`
- `ChatRequest`
- `ChatResponse`
- `ChatStreamEvent`
- `ProviderCapabilities`
- `ProviderDiagnostics`
- `TokenUsage`

这些内部统一类型让上层逻辑不需要直接处理厂商响应差异。

## Provider Factory

当前 provider 创建是函数式的，不是类式工厂。

真实流程是：

1. 通过 `load_config(...)` 解析配置
2. 调用 `create_provider_from_config(config)`
3. 选择：
   - OpenAI-compatible preset 或自定义 endpoint
   - Anthropic provider

目前没有：

- 动态 provider 插件注册表
- provider 实例缓存
- 独立的健康检查子系统

静态 preset 定义在 `firstcoder/providers/presets.py` 中，当前包括：

- `openai`
- `deepseek`
- `qwen`
- `moonshot`
- `zhipu`
- `openrouter`
- `ollama`
- `anthropic`

## OpenAI-Compatible Provider

`firstcoder/providers/openai_compatible.py` 是当前主线路径。

关键行为：

- 使用 `openai` Python SDK
- 把内部 `ChatRequest` 转成 Chat Completions 参数
- 只有在 provider capabilities 允许时才发送 tools
- 支持 streaming
- 合并 preset 级和 request 级的 `extra_body`
- 以防御性方式解析 tool calls

几个重要的防御性处理：

- tool call 参数 JSON 非法时会直接放弃这批 tool calls
- 当 `finish_reason="length"` 且伴随 tool calls 时也会走保守路径

流式输出会被转换成内部 `ChatStreamEvent`，从而让 TUI 和 agent loop 用统一方式消费。

## Anthropic Provider

`firstcoder/providers/anthropic_provider.py` 当前是明确的实验性实现。

现状包括：

- 支持普通 completion
- 以较有限形式支持 tool use
- 会把 system prompt 抽取到 Anthropic 独立的 `system` 字段
- 目前没有实现与 OpenAI-compatible 路径同级别的完整流式输出能力

因此它更适合被看作一个窄一点的适配器，而不是当前主线路径。

## 工具适配

provider 不会直接消费 tool executor。它只接收模型可见的工具 schema，这层转换在 `firstcoder/providers/tool_adapters.py` 中完成。

真实流程是：

1. tools 在内部以 `ToolDefinition` 暴露
2. provider adapter 把它们转成厂商原生 schema
3. tool calls 再被规范化回内部 `ToolCall`

这样 provider-specific 的格式差异不会渗透进 agent loop。

## 错误模型

provider 相关错误通过 `firstcoder/providers/errors.py` 中的 `ProviderError` 和 `ProviderErrorKind` 表示。

这个错误模型覆盖了：

- 不支持的能力
- prompt 过长
- 认证和配置失败
- timeout 和网络错误
- rate limit

这很重要，因为 prompt-too-long 错误会直接影响 context compaction 和 retry 路径。

## 设计说明

- provider capabilities 是显式建模的，并用于控制工具暴露和 streaming 行为。
- provider 层比最小的“请求进 / 文本出”适配器更重视 diagnostics。
- 当前扩展方式是静态工厂 + preset，而不是插件式注册表。
