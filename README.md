# FirstCoder

FirstCoder 是一个使用 Python 构建的本地 AI coding agent 学习项目。

项目目标不是直接复制一个成熟 agent，而是通过逐步实现 UI、模型调用、tool calling、文件操作和 agent 主循环，理解一个 coding agent 从输入到执行再到反馈的完整工作方式。

## 项目定位

- 本地运行的终端应用。
- 使用 Textual 构建 CLI/TUI 交互界面。
- 支持多模型 provider 的扩展方向。
- 以 tool calling 作为核心能力之一。
- 以学习和迭代为主，不追求第一版功能完整。

## 当前阶段

当前项目处于骨架阶段。旧的 agent / memory / context 试验实现已经清理，当前工作区先保留稳定的 provider、tool、config 和 utils 基础层：

- `firstcoder/providers`：模型 provider 抽象和具体 provider 实现。
- `firstcoder/tools`：工具定义、注册、执行和结果结构。
- `firstcoder/config`：配置加载和环境变量读取。
- `firstcoder/utils`：JSON、schema、sandbox、git 等基础工具函数。
- `tests`：测试代码。

后续会按 `docs/context-compact-plan.md` 重新实现 agent 主循环、会话存储、上下文投影和压缩恢复逻辑。

## 计划能力

- 在终端中输入任务并查看 agent 回复。
- 接入不同模型 provider。
- 让模型通过 tool calling 调用本地工具。
- 展示工具调用过程和执行结果。
- 后续支持受控的文件读取、编辑和命令执行。
- 后续支持分层上下文压缩：轻量压缩旧工具输出、按内容类型路由压缩大段输出、归档超大工具结果，并在上下文压力过高时总结旧历史。

## 上下文压缩规划

FirstCoder 的上下文压缩计划采用分层策略，而不是只做简单截断或只依赖 LLM 总结：

- `L1 Micro Compact`：低成本压缩旧工具输出，保留工具名、规模和关键元信息。
- `L2 Content-Routed Compress`：根据内容类型选择 `grep`、`log`、`diff`、`json`、`text` 等专用压缩策略。
- `L3 Archive + Placeholder`：把特别大的工具输出保存到本地 session store，在上下文中放入可追踪占位符。
- `L4 Session Summary Compact`：当上下文压力仍然过高时，用 LLM 总结旧历史，并保留最近几轮原文。
- `L5 Manual Compact`：未来提供手动触发入口，方便观察和学习压缩前后的上下文变化。

这些逻辑属于上下文管理，计划重新放在 `firstcoder/context` 中，不绑定具体 provider，也不写进 Textual widget。

## Provider 接入

当前已经实现 provider 抽象层，agent 后续只需要依赖统一的 `ChatProvider` 接口。

已预留的接入方式：

- `openai`：OpenAI 官方接口。
- `deepseek`：DeepSeek OpenAI-compatible 接口。
- `qwen`：阿里云 DashScope 兼容模式。
- `moonshot`：Moonshot OpenAI-compatible 接口。
- `zhipu`：智谱 OpenAI-compatible 接口。
- `openrouter`：OpenRouter OpenAI-compatible 接口。
- `ollama`：本地 Ollama OpenAI-compatible 接口。
- `anthropic`：Anthropic Messages API。

默认通过环境变量 `FIRSTCODER_PROVIDER` 选择 provider。如果不设置，则默认使用 `openai`。

示例：

```powershell
$env:FIRSTCODER_PROVIDER = "deepseek"
$env:DEEPSEEK_API_KEY = "your-api-key"
$env:DEEPSEEK_MODEL = "deepseek-chat"
```

也可以使用完全自定义的 OpenAI-compatible 接口：

```powershell
$env:FIRSTCODER_PROVIDER = "openai-compatible"
$env:FIRSTCODER_API_KEY = "your-api-key"
$env:FIRSTCODER_BASE_URL = "https://example.com/v1"
$env:FIRSTCODER_MODEL = "your-model"
```

## 本地环境

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## 依赖说明

当前依赖通过 `requirements.txt` 管理。项目使用 `pip + venv`，不使用 Poetry 或 uv。

## 项目规则

项目级规则和开发记忆放在本地的 `AGENTS.md` 中。该文件不会上传到 git。

README 只描述项目本身；具体开发偏好、架构约束和长期记忆以 `AGENTS.md` 为准。
