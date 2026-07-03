<p align="center">
  <img src="assets/firstcoder-logo.png" alt="FirstCoder logo" width="156">
</p>

<h1 align="center">FirstCoder</h1>

<p align="center">
  <strong>一个把 coding agent 内部机制摊开给你看的本地 Python 项目。</strong>
</p>

<p align="center">
  <a href="#快速开始"><img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white"></a>
  <a href="#tui"><img alt="Textual TUI" src="https://img.shields.io/badge/Textual-TUI-5B5BD6?style=flat-square"></a>
  <a href="#provider"><img alt="OpenAI compatible" src="https://img.shields.io/badge/OpenAI-Compatible-111827?style=flat-square"></a>
  <a href="#开发"><img alt="pytest" src="https://img.shields.io/badge/pytest-tested-0A9EDC?style=flat-square&logo=pytest&logoColor=white"></a>
</p>

<p align="center">
  <a href="README.md">English</a>
  · 简体中文
</p>

<p align="center">
  <a href="#为什么做-firstcoder">为什么做</a>
  · <a href="#快速开始">快速开始</a>
  · <a href="#tui">TUI</a>
  · <a href="#任务感知压缩">压缩层</a>
  · <a href="#命令">命令</a>
  · <a href="#架构">架构</a>
</p>

---

FirstCoder 是一个学习导向的本地 coding agent。它不是想用一个聊天壳去替代成熟工具，而是想回答一个更具体的问题：

> 一个 coding agent 在流式输出、调用工具、申请权限、压缩上下文、恢复会话的时候，内部到底发生了什么？

它是一个能真实运行的 agent：有 Textual TUI、工具调用、权限系统、会话持久化、OpenAI-compatible provider 和上下文压缩层。代码也刻意保持清晰的模块边界，方便你按子系统阅读、调试、讲解，并把它作为简历或作品集里的工程项目展示。

![FirstCoder TUI](docs/images/tui-chat.png)

> [!NOTE]
> FirstCoder 是学习项目和作品集项目。它可以本地使用，但目标是让 agent 内部机制变得可读、可跑、可解释，而不是宣称替代成熟 coding agent。

## 为什么做 FirstCoder

很多 coding-agent demo 展示的是表面：输入一个需求，然后模型改代码。FirstCoder 更关心中间那条链路。

| 你想学什么 | 可以看哪里 |
| --- | --- |
| 模型回复怎么变成工具调用 | `firstcoder/agent`、`firstcoder/providers` |
| 工具如何读取文件、执行 shell、操作 git、访问网络 | `firstcoder/tools` |
| agent 为什么不能随便执行危险动作 | `firstcoder/permissions` |
| 长会话怎么保存、压缩和恢复 | `firstcoder/context`、`firstcoder/session` |
| 终端 UI 怎么展示流式输出和运行状态 | `firstcoder/app` |
| 怎么用一个小 benchmark 验证 agent 工作流 | `benchmark/local_pytest` |

这个项目最值得看的实验是 **任务感知上下文压缩**：FirstCoder 不只是等上下文窗口快满了再总结历史，而是尝试识别语义上的任务边界，生成程序侧拥有的任务哈希，再用这个哈希决定什么时候压缩旧任务内容。

## 快速开始

推荐安装：

```sh
pipx install firstcoder
```

如果不用 `pipx`：

```sh
python -m pip install firstcoder
```

开发源码安装：

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

启动 TUI：

```sh
firstcoder
```

不打开 TUI，直接跑一轮消息：

```sh
firstcoder --message "用一段话介绍这个仓库"
```

使用行式交互模式：

```sh
firstcoder --interactive
```

Windows PowerShell：

```powershell
py -m pip install firstcoder
firstcoder
```

## 配置

创建初始配置：

```sh
firstcoder config init
firstcoder config path
firstcoder config show
```

默认配置路径：

```text
global:  ~/.config/firstcoder/config.toml
project: ./firstcoder.toml
```

配置示例：

```toml
model = "yurenapi/gpt-5.5"

[provider]
type = "openai-compatible"
name = "yurenapi"
base_url = "https://example.com/v1"
api_key_env = "FIRSTCODER_API_KEY"

[permissions]
mode = "ask"

[ui]
theme = "default"
```

密钥建议放在环境变量里，不要写进仓库：

```sh
export FIRSTCODER_API_KEY="your-api-key"
```

配置优先级：

```text
CLI --provider
> 环境变量 / .env
> 项目 firstcoder.toml
> 全局 ~/.config/firstcoder/config.toml
> 默认值
```

## TUI

FirstCoder 的 TUI 不是为了把内部过程藏起来，而是为了把 agent loop 展示出来。你能看到当前 session、provider/model、权限模式、activity 状态、流式输出、工具调用、工具结果和权限请求。

空会话：

![FirstCoder empty TUI](docs/images/tui-empty.png)

工具调用会出现在对话流中：

![FirstCoder tool calls](docs/images/tui-tools.png)

权限请求会暂停 agent，等待用户决定：

![FirstCoder permission request](docs/images/tui-permission.png)

Activity 行是刻意保留的。模型正在思考、正在输出、正在跑工具、等待权限、或者读完工具结果继续组织回答时，界面都应该让用户知道 agent 还在工作。

## 任务感知压缩

很多 agent 会在 token 压力变高时摘要或截断历史。FirstCoder 也处理 token 压力，但更有意思的是语义触发路径：

```text
用户消息
  -> 模型调用 task_boundary(decision, basis_message_id)
  -> 程序生成 candidate task_hash
  -> 稳定窗口确认任务切换
  -> TASK_HASH_CHANGED 触发压缩
  -> 旧任务内容被 micro-compact
  -> session event 保留这次切换，方便 resume
```

模型不会自己发明 hash。它只能提交：

```json
{
  "decision": "same | new | uncertain",
  "basis_message_id": "msg_xxx"
}
```

程序再根据 session id、basis message id 和任务边界策略版本生成稳定 hash。稳定窗口会防止模型偶尔误判一次 `new` 就立刻切换任务。

这件事有几个价值：

- **减少旧任务污染**：新任务开始时，不必继续携带上一件事的全部原文。
- **比单纯按时间截断更准**：内容是因为属于旧任务才被压缩，不只是因为它更早。
- **与 provider 解耦**：任务身份在运行时状态和事件日志里，不依赖某个模型厂商的 prompt 格式。
- **对 resume 友好**：任务边界观察会写入事件，恢复会话时可以重放 active task 状态。

如果你已经理解了基础 tool calling，这一层是 FirstCoder 最值得深入看的地方。

## 核心能力

| 能力 | 展示了什么 |
| --- | --- |
| Agent loop | 多轮模型调用、工具调用、最终回答和循环限制 |
| 流式输出 | OpenAI-compatible 流式文本、tool-call delta 拼装、基础 `reasoning_delta` 转发 |
| 工具系统 | 文件读写、shell、git、diagnostics、web fetch/search、todo、ask_user |
| 权限系统 | 本地 `ALLOW / ASK / DENY` 决策和长期授权 |
| Session | append-only JSONL 事件、catalog、resume、rename、share/export |
| Context | checkpoint、archive、task hash、L1-L4 压缩、`PROMPT_TOO_LONG` 恢复 |
| TUI | Markdown 渲染、实时 activity、工具条目、权限提示和 slash commands |
| Evaluation | 用本地 pytest 小任务检查 agent 是否能完成基本编码流程 |

## Provider

当前主线是 **OpenAI Chat Completions-compatible**。这条路径支持普通消息、function tools、流式文本、tool-call delta 拼装，以及兼容 provider 发出时的基础 `reasoning_delta` 事件。

当 provider 返回 `PROMPT_TOO_LONG` 时，FirstCoder 会尝试压缩上下文，并重试一次请求。

常见 provider 环境变量：

| Provider | API key | Model | 默认模型 |
| --- | --- | --- | --- |
| `openai` | `OPENAI_API_KEY` | `OPENAI_MODEL` | `gpt-4.1-mini` |
| `deepseek` | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` | `deepseek-chat` |
| `qwen` | `DASHSCOPE_API_KEY` | `QWEN_MODEL` | `qwen-plus` |
| `moonshot` | `MOONSHOT_API_KEY` | `MOONSHOT_MODEL` | `moonshot-v1-8k` |
| `zhipu` | `ZHIPUAI_API_KEY` | `ZHIPU_MODEL` | `glm-4-flash` |
| `openrouter` | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` | `openai/gpt-4.1-mini` |
| `ollama` | `OLLAMA_API_KEY` | `OLLAMA_MODEL` | `qwen2.5-coder:7b` |
| `anthropic` | `ANTHROPIC_API_KEY` | `ANTHROPIC_MODEL` | `claude-sonnet-4-5` |

DeepSeek 示例：

```sh
export FIRSTCODER_PROVIDER="deepseek"
export DEEPSEEK_API_KEY="your-api-key"
export DEEPSEEK_MODEL="deepseek-chat"
```

任意 OpenAI-compatible 服务：

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

Anthropic support 目前是 experimental / 实验性。它还不覆盖完整的 Anthropic 原生 thinking/cache/streaming 行为。FirstCoder 当前也不承诺 OpenAI Responses API、完整 reasoning 持久化/展示，或 multimodal / 多模态输入输出。

## 命令

CLI：

| 命令 | 说明 |
| --- | --- |
| `firstcoder` | 在交互终端中启动 TUI |
| `firstcoder --tui` | 显式启动 Textual TUI |
| `firstcoder --message "..."` | 跑一轮用户消息 |
| `firstcoder --interactive` | 启动行式 REPL |
| `firstcoder --project <path>` | 指定项目根目录 |
| `firstcoder --data-root <path>` | 指定 session / permission 数据目录 |
| `firstcoder --session-id <id>` | 创建或复用指定 session |
| `firstcoder --provider <name>` | 覆盖 provider |
| `firstcoder --auto-approve` | REPL 模式下自动用 `allow_once` 回答权限请求 |
| `firstcoder --max-tool-rounds <n>` | 覆盖单轮最大工具轮数 |
| `firstcoder config init` | 创建初始全局配置 |
| `firstcoder config path` | 查看配置路径 |
| `firstcoder config show` | 查看生效 provider 配置，不输出密钥 |

TUI slash commands：

| 命令 | 说明 |
| --- | --- |
| `/sessions` | 列出 session 摘要 |
| `/session <session_id>` | 查看一个 session |
| `/resume <session_id>` | 恢复一个 session |
| `/share [session_id] [--tool-results]` | 导出 Markdown transcript |
| `/rename <title>` | 重命名当前 session |
| `/context` | 查看上下文状态 |
| `/compact status` | 查看压缩状态 |
| `/compact` | 手动触发上下文压缩 |
| `/mode` | 查看当前权限模式 |
| `/mode conservative` | 使用更谨慎的权限策略 |
| `/mode standard` | 使用默认平衡策略 |
| `/mode aggressive` | 更主动允许常见项目内开发操作 |
| `/mode bypass` | 跳过策略检查，适合受控本地实验 |

计划中的体验包括 `/help`、`/new`、选择器式 `/resume`、长期授权查看和撤销。

## 权限

FirstCoder 把“模型想做什么”和“程序允许做什么”分开。

权限动作包括：

- `read_path`
- `write_path`
- `delete_path`
- `execute_shell`
- `network_request`
- `git_operation`
- `read_env`

决策结果：

```text
ALLOW -> 直接执行
ASK   -> 暂停并询问用户
DENY  -> 阻止动作
```

权限模式：

| 模式 | 行为 |
| --- | --- |
| `conservative` | 更多确认，更谨慎的默认值 |
| `standard` | 默认平衡模式 |
| `aggressive` | 更愿意执行常见项目内操作 |
| `bypass` | 跳过策略检查，用于受控实验 |

当用户选择 `allow_always_same_scope` 时，会生成长期授权。授权记录保存在当前 data root 下的 `permissions.json`。

## Session

FirstCoder 用 append-only JSONL 事件保存 session 事实。Checkpoint 和 compaction event 会改变下一轮发给 provider 的 effective context，但不会替换底层事件日志。

默认数据目录：

```text
<project-root>/.firstcoder/
```

里面会保存：

- session event log
- session catalog 数据
- context checkpoint 和 archive
- compaction event
- 长期权限授权
- 导出的 transcript

Resume 会从事件日志重建状态，包括任务边界观察和 active task hash。

## 架构

```text
user input
   |
   v
Textual TUI / CLI
   |
   +--> slash commands
   |       sessions / context / compact / permission mode
   |
   +--> AgentChatRunner
           |
           +--> AgentLoop
                   |
                   +--> ChatProvider
                   |       OpenAI-compatible / Anthropic experimental
                   |
                   +--> ToolRegistry
                   |       file / shell / git / web / todo / ask_user
                   |
                   +--> PermissionManager
                   |       allow / ask / deny / grants
                   |
                   +--> ContextWindowManager
                           checkpoint / archive / compact / recovery
```

项目结构：

```text
firstcoder/
  agent/        agent loop、运行时 session、用户输入恢复、循环限制
  app/          Textual TUI、命令路由、运行时组装
  config/       配置文件、.env、环境变量加载
  context/      event log、上下文投影、checkpoint、archive、compaction
  eval/         benchmark adapter、patch 提取、预测生成
  permissions/  权限策略、长期授权、项目级 permission manager
  providers/    provider 抽象和厂商适配
  session/      catalog、resume、transcript、share、redaction
  tools/        内置工具、schema、结果和权限 metadata
  utils/        JSON、schema、sandbox、subprocess、git helper
benchmark/      本地 pytest benchmark 和实验入口
docs/           设计记录、实施计划和截图
tests/          pytest 测试
```

## 本地 Benchmark

轻量本地 pytest benchmark 位于：

```text
benchmark/local_pytest/
```

它会创建小型 Python 任务仓库，让 FirstCoder 修改代码，再用 pytest 判分。它不是排行榜，而是一个本地探针，用来观察 agent loop 能不能走完：

```text
读任务 -> 看文件 -> 改代码 -> 跑测试 -> 收工
```

运行 smoke benchmark：

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

更多说明见 [docs/LOCAL_PYTEST_BENCHMARK.md](docs/LOCAL_PYTEST_BENCHMARK.md)。

## 开发

安装开发依赖：

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

运行全部测试：

```sh
.venv/bin/python -m pytest
```

运行聚焦测试：

```sh
.venv/bin/python -m pytest tests/test_app_tui.py -q
```

构建包：

```sh
python -m pip install build
python -m build
```

本地测试全局安装：

```sh
pipx install --force .
firstcoder
```

测试应尽量避免依赖真实 API key 和网络。Provider、tool、context、permission、session 和 benchmark 行为优先使用 fake、fixture 或临时目录覆盖。

## 路线图

- 做好 `/help`、`/new` 和选择器式 `/resume`。
- 增加长期授权列表和撤销命令。
- 继续打磨 TUI 的流式 Markdown 展示。
- 强化 agent loop 在验证、运行时间和工具轮数上的护栏。
- 增加本地 coding task benchmark 覆盖。
- 继续完善任务感知上下文压缩。
