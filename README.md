<p align="center">
  <img src="assets/firstcoder-logo.png" alt="FirstCoder logo" width="220">
</p>

<h1 align="center">FirstCoder</h1>

<p align="center">
  <strong>一个可拆开学习、可逐步长大的本地 coding agent。</strong>
</p>

<p align="center">
  <a href="#快速开始"><img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white"></a>
  <a href="#当前-tui-命令"><img alt="Textual TUI" src="https://img.shields.io/badge/Textual-TUI-5B5BD6?style=flat-square"></a>
  <a href="#provider"><img alt="OpenAI compatible" src="https://img.shields.io/badge/OpenAI-Compatible-111827?style=flat-square"></a>
  <a href="#测试"><img alt="pytest" src="https://img.shields.io/badge/pytest-tested-0A9EDC?style=flat-square&logo=pytest&logoColor=white"></a>
</p>

<p align="center">
  <a href="#快速开始">快速开始</a>
  · <a href="#为什么是-firstcoder">为什么是 FirstCoder</a>
  · <a href="#当前-tui-命令">命令</a>
  · <a href="#架构">架构</a>
  · <a href="#路线图">路线图</a>
</p>

---

FirstCoder 是一个用 Python 写的本地 coding agent 学习项目。它不是一个黑盒产品，而是一套能看清楚内部流动的 agent 骨架：模型怎么思考、工具怎么调用、权限怎么拦截、上下文怎么压缩、session 怎么恢复，都尽量放在清晰的模块里。

```text
$ firstcoder

FirstCoder  local coding agent
project     /your/project
provider    deepseek / deepseek-chat
session     fc_20260621_...

You > 修一下 failing test，然后解释你改了什么

AI  > 我先看测试失败点和相关实现。
Tool call: grep {"pattern": "failing test", "path": "tests"}
Tool call: view {"path": "firstcoder/..."}
Tool call: apply_patch {"path": "firstcoder/..."}
Tool call: diagnostics {"command": ".venv/bin/python -m pytest ..."}

AI  > 已修复，并通过聚焦测试。改动点是 ...
```

> [!NOTE]
> 当前底层 agent/session/context/provider/tool 能力已经成型；完整的 `python -m firstcoder` 直接启动体验和更自然的 CLI/TUI 命令集仍在推进中。

## 为什么是 FirstCoder

| 如果你想要 | FirstCoder 关注的是 |
| --- | --- |
| 学一个 coding agent 到底怎么跑 | 保持 agent loop、tool calling、context、permission 的边界清楚 |
| 改造自己的本地 agent | provider、tools、session、TUI 都是可替换模块 |
| 理解 Claude Code / OpenCode / MiniCode 这类工具的骨架 | 用 Python 复现终端 agent 的核心路径，而不是直接堆功能 |
| 做 benchmark 或实验 | 已有 SWE-bench Lite 预测生成入口和 agent loop 护栏计划 |

FirstCoder 的气质更接近“可读、可改、可实验”的 coding agent harness。它不是要第一天就塞满插件和云同步，而是先把一个真实 agent 最重要的几条链路打通。

## 已经具备

- 本地 Textual TUI 外壳与 slash command 路由。
- OpenAI Chat Completions-compatible provider 主线，覆盖 OpenAI、DeepSeek、Qwen、Moonshot、Zhipu、OpenRouter、Ollama 等接入方式。
- function tool calling、OpenAI-compatible 流式文本输出和 function tool call delta 拼装。
- 内置文件、搜索、编辑、patch、shell、diagnostics、git、web search、todo、think 等工具。
- project-scoped 权限系统，支持 `ALLOW`、`ASK`、`DENY` 和长期授权记录。
- append-only JSONL session store，支持 session catalog、resume、rename、share/export。
- 分层上下文管理：checkpoint、archive、工具结果压缩、LLM compact 和上下文过长恢复。
- `PROMPT_TOO_LONG` 错误触发一次上下文压缩恢复，压缩成功后再重试请求。
- SWE-bench Lite 预测生成入口，用于后续 coding benchmark 实验。

## 快速开始

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

配置一个 provider。默认 provider 是 `openai`：

```sh
export OPENAI_API_KEY="your-api-key"
export OPENAI_MODEL="gpt-4.1-mini"
```

切到 DeepSeek：

```sh
export FIRSTCODER_PROVIDER="deepseek"
export DEEPSEEK_API_KEY="your-api-key"
export DEEPSEEK_MODEL="deepseek-chat"
```

接入任意 OpenAI-compatible 服务：

```sh
export FIRSTCODER_PROVIDER="openai-compatible"
export FIRSTCODER_API_KEY="your-api-key"
export FIRSTCODER_BASE_URL="https://example.com/v1"
export FIRSTCODER_MODEL="your-model"
```

本地 Ollama：

```sh
export FIRSTCODER_PROVIDER="ollama"
export OLLAMA_BASE_URL="http://localhost:11434/v1"
export OLLAMA_MODEL="qwen2.5-coder:7b"
```

> [!TIP]
> 配置可以放进 `.env`。当前项目还没有专门的 JSON 配置文件，LLM 配置主线仍是环境变量和 `.env`。

## 当前 TUI 命令

当前命令先覆盖 session、context 和权限模式：

| 命令 | 作用 |
| --- | --- |
| `/sessions` | 列出历史 session 摘要 |
| `/session <session_id>` | 查看指定 session 详情 |
| `/resume <session_id>` | 恢复指定 session |
| `/share [session_id] [--tool-results]` | 导出 Markdown transcript |
| `/rename <title>` | 重命名当前 session |
| `/context` | 查看当前上下文状态 |
| `/compact status` | 查看 compact 状态 |
| `/compact` | 手动触发 compact |
| `/mode` | 查看当前权限模式 |
| `/mode conservative\|standard\|aggressive` | 切换权限模式 |

正在补齐的体验：

- `python -m firstcoder` 启动入口。
- `/help` 命令说明。
- `/config` 查看 provider、model、project root、data root、session 和 LLM readiness。
- `/new` 新建 session。
- `/resume` 无参数时打开可上下选择的历史会话列表。
- `/permissions` 查看长期授权，`/permissions revoke <grant_id>` 撤销长期授权。

## Provider

Provider 层的主线是 OpenAI Chat Completions-compatible 协议。agent 和 TUI 只依赖统一的 `ChatProvider` 接口，厂商差异尽量收敛在 `firstcoder/providers` 内。

| Provider | API key 环境变量 | Model 环境变量 | 默认模型 |
| --- | --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` | `gpt-4.1-mini` |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` | `deepseek-chat` |
| `qwen` | `DASHSCOPE_API_KEY` | `QWEN_MODEL` | `qwen-plus` |
| `moonshot` | `MOONSHOT_API_KEY` | `MOONSHOT_MODEL` | `moonshot-v1-8k` |
| `zhipu` | `ZHIPUAI_API_KEY` | `ZHIPU_MODEL` | `glm-4-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` | `openai/gpt-4.1-mini` |
| `ollama` | `OLLAMA_API_KEY` | `OLLAMA_MODEL` | `qwen2.5-coder:7b` |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` | `claude-sonnet-4-5` |

Anthropic provider 目前是实验性实现。项目暂不承诺 Anthropic 原生 thinking/cache/streaming、OpenAI Responses API、reasoning 内容保存展示和多模态输入输出。

## 架构

```text
user input
   |
   v
Textual TUI / command router
   |
   +--> slash commands
   |       session / context / compact / permission mode
   |
   +--> AgentLoop
           |
           +--> ChatProvider
           |       OpenAI-compatible / Anthropic experimental
           |
           +--> ToolRegistry
           |       view / grep / edit / shell / diagnostics / git / ...
           |
           +--> PermissionManager
           |       ALLOW / ASK / DENY / long-lived grants
           |
           +--> ContextWindowManager
                   checkpoint / archive / compact / recovery
```

项目结构：

```text
firstcoder/
  agent/        agent loop、运行期 session、用户输入恢复、循环护栏
  app/          Textual TUI、命令路由、运行期组装
  config/       环境变量和 .env 配置加载
  context/      session event log、上下文投影、checkpoint、archive、compact
  eval/         SWE-bench Lite adapter、patch 提取和预测生成
  permissions/  权限策略、长期授权、项目级 permission manager
  providers/    模型 provider 抽象和厂商适配
  session/      session catalog、resume、transcript、share、redaction
  tools/        内置工具、schema、执行结果和权限登记
  utils/        JSON、schema、sandbox、subprocess、git 等基础工具
tests/          pytest 测试
```

## Session 与上下文

FirstCoder 使用 append-only JSONL session event log 保存会话事实，再由 context 层重建投影：

- session catalog 从事件日志生成历史会话列表。
- resume service 通过 session id 恢复运行期 session。
- transcript/share service 可以导出只读 Markdown。
- context builder 负责构造发给模型的消息窗口。
- context manager 负责 checkpoint、archive、compact 和上下文过长恢复。

上下文压缩采用分层策略：

- L1：压缩旧工具输出，保留工具名、规模和关键元信息。
- L2：把超大工具结果归档到本地 store，在上下文中保留可追踪占位符。
- L3：按内容类型压缩 grep、log、diff、json、text 等结果。
- L4：上下文压力仍然过高时，用 LLM 总结旧历史并保留最近几轮原文。
- L5：手动 compact 入口，方便观察压缩前后的变化。

## 权限模型

权限系统是工具执行前的安全闸门。模型可以请求工具，但工具真正执行前会经过 permission manager 判断：

```text
ALLOW -> 直接执行
ASK   -> 暂停并询问用户
DENY  -> 拒绝执行
```

当前支持三种权限模式：

- `conservative`：更保守，更多操作需要确认。
- `standard`：默认平衡模式。
- `aggressive`：更主动，适合受控实验环境。

长期授权来自用户选择持续允许，保存到项目数据目录下的 `.firstcoder/permissions.json`。后续 `/permissions` 命令会用于查看和撤销这些授权。

## SWE-bench Lite

FirstCoder 已有 SWE-bench Lite 预测生成入口，用于把 agent 产出的代码改动转换为官方 harness 可消费的 `predictions.jsonl`：

```sh
python -m firstcoder.eval.swebench \
  --instances data/swebench_lite_instances.jsonl \
  --repos-root /tmp/firstcoder-swe-lite/repos \
  --out runs/firstcoder_swe_lite_predictions.jsonl \
  --provider openai \
  --model-name firstcoder \
  --max-instances 1 \
  --print-harness-command
```

## 测试

```sh
.venv/bin/python -m pytest
```

聚焦测试示例：

```sh
.venv/bin/python -m pytest tests/test_context_store.py -q
```

项目尽量避免测试依赖真实 API key 或网络。provider、tool、agent loop、context recovery、permission 和 eval 相关行为应优先使用 fake、fixture 或本地临时目录覆盖。

## 路线图

- 补齐 `python -m firstcoder` 和基础 CLI/TUI 上手路径。
- 增加 `/help`、`/config`、`/new`、交互式 `/resume` 和 `/permissions`。
- 完善 agent loop 护栏：验证成功后收工、provider 调用次数上限、单轮耗时上限和更合理的工具轮数预算。
- 继续打磨 SWE-bench Lite adapter，让 FirstCoder 可以稳定跑小规模 benchmark。
