# Provider 设计

[English](PROVIDERS_DESIGN.md)

## 边界

Provider 是协议 adapter。agent loop 只说 FirstCoder 的内部类型；每个 provider 负责把它们变成厂商请求，再把响应还原回来。这样 OpenAI SDK 对象、Anthropic block、厂商错误字符串都不会漏进编排层。

## 一次请求与响应

```text
ContextBuilder + session registry
  -> ChatRequest(messages, tools, tool_choice, ...)
  -> ChatProvider.complete() 或 astream()
  -> 厂商请求
  -> 厂商响应/chunk
  -> ChatResponse 或 ChatStreamEvent
  -> AgentLoop 与 TUI
```

`ChatRequest.tools` 是内部唯一的工具定义载体。OpenAI-compatible 会把它变成 `tools=[{"type":"function", ...}]`；Anthropic 则是 `tools=[{"name", "description", "input_schema"}]`。因此 schema 不应该再复制进 prompt 正文。

## 共享类型

`providers/types.py` 定义稳定边界：

| 类型 | 作用 |
| --- | --- |
| `ChatMessage` | 规范化 system/user/assistant/tool message |
| `ToolDefinition` / `ToolCall` | 发送的定义 / 返回的调用 |
| `ChatRequest` | adapter 的完整输入，含厂商无关工具定义 |
| `ChatResponse` | 完整标准结果和 finish reason |
| `ChatStreamEvent` | 标准化 text/reasoning/tool-call 流事件 |
| `ProviderCapabilities` | 工具、强制选择、流、reasoning 等能力开关 |
| `ProviderDiagnostics` / `TokenUsage` | 和 assistant 正文分离的诊断/用量 |

`ChatProvider` 必须实现 `complete`；默认异步路径把同步调用放在线程里，支持流的 provider 才覆盖 `astream`。

## 配置与工厂

`load_config` 解析设置，`create_provider_from_config` 构造 provider。静态 preset 包含 OpenAI、DeepSeek、Qwen、Moonshot、Zhipu、OpenRouter、Ollama、Anthropic。当前没有运行时 provider plugin registry、实例缓存或通用 health-check 服务。

凭证/base URL 留在配置和环境层，不要让 agent loop 自己读。`/model` 切换会重建 provider，并同步更新 L4 context compaction 所用的 summarizer。

## OpenAI-Compatible 主路径

`OpenAICompatibleProvider` 构造 Chat Completions 参数时会处理：

- context 投影出的 messages；
- 仅在 `supports_tools` 为真时发送 tools；
- capability 允许时转换 tool choice；
- 选择 token 参数并合并 extra body；
- 流式路径增加 `stream=True`。

它会保守解析 tool call。arguments JSON 不合法时整批危险调用会被丢弃；若 `finish_reason="length"` 同时带 tool call，也会丢弃，因为参数可能只有半截。这是“宁可少做、不做半截危险动作”的明确选择。

原始 stream chunk 在 adapter 内聚合，直到 tool call 完整；上层拿到的是稳定的 `ChatStreamEvent`，不是 SDK 的一坨原始对象。

## Anthropic 路径：与 OpenAI-compatible 契约对齐

`AnthropicProvider` 与 OpenAI-compatible 主线共享同一套内部契约：非流式 `complete`、
异步 `astream`（`text_delta` / tool-call 事件 / `message_completed`）、tools、
forced `tool_choice`、并行工具开关、usage，以及错误归类。system message 会挪到
Anthropic 独立 `system` 字段，schema 走 `input_schema`。连续的 `tool` 消息会合并成
同一条 user 里的 `tool_result` 列表。prompt cache 等 Anthropic 专有增强仍是可选项，
不是 agent loop 对齐的最低门槛。


## 错误与恢复契约

adapter 将失败归类为 `ProviderErrorKind`（如 unsupported、prompt-too-long、auth/configuration、timeout/network、rate limit）。loop 因此可根据统一类别做决定，特别是 prompt-too-long 的有界 context recovery，而无需猜各家错误文案。

## 验证

```sh
.venv/bin/python -m pytest tests/test_providers.py tests/test_provider_errors.py \
  tests/test_readme_provider_docs.py -q
```

新增 adapter 时，用 fake client 断言出站 wire 参数、规范化 tool response、承诺了就测 streaming、错误归类。核心测试不要依赖真实 API key。

## 扩展清单

1. 如实声明 capability。
2. 每个共享 request 字段都转换，或明确拒绝不支持项。
3. system、tool call、tool result 都要双向转换。
4. 规范化 error，不能把 SDK 专属异常泄出 adapter。
5. 给畸形与截断 tool call 加 fake-client 测试。

关联：[架构说明](ARCHITECTURE.zh-CN.md)、[工具设计](TOOLS_DESIGN.zh-CN.md)、[上下文管理](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)。
