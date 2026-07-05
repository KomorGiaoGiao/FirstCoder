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
  <a href="#provider"><img alt="OpenAI Compatible" src="https://img.shields.io/badge/OpenAI-Compatible-111827?style=flat-square"></a>
  <a href="#开发"><img alt="pytest" src="https://img.shields.io/badge/pytest-tested-0A9EDC?style=flat-square&logo=pytest&logoColor=white"></a>
  <a href="https://deepwiki.com/KomorGiaoGiao/FirstCoder"><img alt="Ask DeepWiki" src="https://img.shields.io/badge/Ask-DeepWiki-0F7BBF?style=flat-square&labelColor=2B2B2B"></a>
</p>

<p align="center">
  <a href="README.md">English</a>
  · 简体中文
</p>

<p align="center">
  <a href="#为什么做-firstcoder">为什么做</a>
  · <a href="#快速开始">快速开始</a>
  · <a href="#tui">TUI</a>
  · <a href="#核心实验">创新点</a>
  · <a href="#技能系统">技能系统</a>
  · <a href="#命令">命令</a>
  · <a href="#架构">架构</a>
  · <a href="#本地-benchmark">Benchmark</a>
  · <a href="#开发">开发</a>
  · <a href="#架构文档">文档</a>
  · <a href="#路线图">路线图</a>
</p>

---

FirstCoder 是一个能真实运行的本地 coding agent，带有 Textual TUI、工具调用、权限系统、会话持久化、OpenAI-compatible provider 和上下文压缩层。代码刻意保持清晰的模块边界，方便你按子系统阅读——无论你是日常使用还是研究它的内部机制。

| 如果你想… | FirstCoder 会展示… |
| --- | --- |
| 学习 coding agent 到底怎么工作 | Agent loop、工具调用、权限系统、上下文压缩 |
| 构建你自己的本地 agent | 可直接复用的 provider、工具、session 和 TUI 模块 |
| 理解 agent 架构 | 清晰的模块边界，面试时可以讲清楚 |

![FirstCoder TUI](docs/images/tui-chat.png)

## 为什么做 FirstCoder

大多数 coding-agent 演示展示的是表面：一个 prompt 进去，代码改完出来。FirstCoder 关注的是中间的机械结构——并且让每一步都可检查。

| 你关心的问题 | 可以看哪里 |
| --- | --- |
| 模型回复怎么变成工具调用 | `firstcoder/agent`、`firstcoder/providers` |
| 工具如何读取文件、执行 shell、操作 git 和网络 | `firstcoder/tools` |
| agent 为什么不能随便执行危险动作 | `firstcoder/permissions` |
| 可复用工作流指令怎么被发现和加载 | `firstcoder/skills` |
| 长会话怎么保存、压缩和恢复 | `firstcoder/context`、`firstcoder/session` |
| 终端 UI 怎么展示状态而不隐藏 loop | `firstcoder/app` |
| 怎么在本地评估一个 coding agent 工作流 | `benchmark/local_pytest` |

## 快速开始

推荐安装：

```sh
pipx install firstcoder
```

<details><summary>其他安装方式</summary>

```sh
# 不使用 pipx
python -m pip install firstcoder

# 从源码安装（开发模式）
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"

# Windows PowerShell
py -m pip install firstcoder
```

</details>

## 快速上手

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

## 配置

创建初始配置：

```sh
firstcoder config init
firstcoder config path
firstcoder config show
```

默认配置路径：

```text
全局:  ~/.config/firstcoder/config.toml
项目:  ./firstcoder.toml
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

密钥建议放在环境变量里：

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

## 核心实验

**任务边界触发的上下文压缩层** 是 FirstCoder 目前最有辨识度的创新点。

很多 agent 会在 token 压力变高时摘要或截断历史。FirstCoder 也处理 token 压力，但更有意思的是语义触发路径：当用户进入新任务时，agent 可以先整理旧任务上下文，避免上一件事的细节污染下一件事。

```text
用户消息
  -> 模型调用 task_boundary(decision, basis_message_id)
  -> 程序生成 candidate task_hash
  -> 稳定窗口确认任务切换
  -> TASK_HASH_CHANGED 触发压缩
  -> 旧任务内容被 micro-compact
  -> session event 保留这次切换，方便 resume
```

模型不会自己发明 hash。它只能提交一个很小的结构化信号：

```json
{
  "decision": "same | new | uncertain",
  "basis_message_id": "msg_xxx"
}
```

然后程序根据 session id、basis message id 和任务边界策略版本生成稳定 hash。稳定窗口会防止模型偶尔误判一次 `new` 就立刻切换任务。

| 设计点 | 为什么重要 |
| --- | --- |
| 程序侧生成 task hash | 模型不能随便发明或漂移任务身份 |
| 稳定窗口确认 | 一次误判 `new` 不会立刻触发压缩 |
| Append-only 事件日志 | 压缩改变有效上下文但保留完整记录 |

## 技能系统

FirstCoder 在启动时自动发现并加载 skills。有两个层级：

- **全局技能**：安装在 `$HOME/.agents/skills/`——跨项目共享
- **项目技能**：位于当前仓库的 `.agents/skills/`——优先级更高

技能发现会留下审计事件：

```json
{"type": "skill_loaded", "skill_path": "skills/example.md", "content_hash": "..."}
{"type": "skill_required_file_loaded", "file_path": "docs/policy.md", "content_hash": "..."}
```

项目技能优先于全局技能。全局技能可以补充这台机器上的长期能力，但不能覆盖项目规则、权限策略或 sandbox 边界。

## Provider

当前主线是 **OpenAI Chat Completions-compatible**。这条路径支持普通消息、function tools 和流式输出。实验性的 **Anthropic** 路径也已可用。

支持的 provider：

- **OpenAI-compatible**（主线）：兼容任何 OpenAI API 端点（OpenAI、Azure、本地 Ollama、vLLM 等）
- **Anthropic**（实验性）：原生支持 Claude 消息、流式和 thinking/cache 行为

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

## 命令

Slash commands 是与 FirstCoder 交互的主要方式（除了自然语言）：

| 命令 | 说明 |
| --- | --- |
| `/new` | 开启新 session |
| `/resume` | 恢复之前的 session |
| `/compact` | 手动触发上下文压缩 |
| `/permission` | 查看或管理权限授权 |
| `/help` | 显示可用命令 |

CLI 命令：

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
                   +--> SkillRouter / SkillLoader
                   |       discover / route / load / audit
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
  skills/       skill 发现、路由、加载和 session 审计事件
  session/      catalog、resume、transcript、share、redaction
  tools/        内置工具、schema、结果和权限 metadata
  utils/        JSON、schema、sandbox、subprocess、git helper
benchmark/      本地 pytest benchmark 和实验入口
docs/           设计记录、实施计划和截图
tests/          pytest 测试
```

## 本地 Benchmark

FirstCoder 附带多个 benchmark 套件：

| 套件 | 用途 |
| --- | --- |
| `benchmark/local_pytest` | 轻量本地探针：读任务 → 看文件 → 改代码 → 跑测试 |
| `benchmark/evalplus` | EvalPlus 编程挑战评估 |
| `benchmark/atcoder` | AtCoder 竞赛题 |
| `benchmark/harness_fast` | 快速 harness 评估 |
| `benchmark/terminal_bench` | 终端任务评估（含 SWE-Bench-Fast） |
| `benchmark/topic_selfplay` | 自生成题目，专门测任务边界识别能力 |

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
python -m pytest
```

运行聚焦测试：

```sh
python -m pytest tests/test_app_tui.py -q
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

## 架构文档

各子系统的详细设计文档：

- [Agent Loop Guardrails](docs/AGENT_LOOP_GUARDRAILS.md) — 验证、运行时间和工具轮数的护栏
- [CLI / TUI Design](docs/CLI_TUI_DESIGN.md) — 终端 UI 架构和命令路由
- [Skill System](docs/SKILL_SYSTEM_DESIGN.md) — 技能发现、路由和加载
- [Permissions System](docs/PERMISSIONS_DESIGN.md) — 权限策略和长期授权
- [Context Management](docs/CONTEXT_MANAGEMENT_DESIGN.md) — 多层压缩和任务边界
- [Providers](docs/PROVIDERS_DESIGN.md) — Provider 抽象和厂商适配
- [Tools System](docs/TOOLS_DESIGN.md) — 内置工具、schema 和权限集成

## 理念

FirstCoder 试图回答一个大多数 coding agent 没有面对的问题：

> 当一个 agent 流式输出、调用工具、申请权限、压缩上下文、恢复会话的时候，内部到底发生了什么？

它是一个能真实运行的 agent——不是包装器，不是聊天壳。代码组织方式让你能一次读一个子系统，并在面试或学习笔记中讲清楚。

当然，它作为日常工具也很好用。

## 路线图

近期：

- 做好 `/help`、`/new` 和选择器式 `/resume`。
- 增加长期授权列表和撤销命令。
- 继续打磨 TUI 的流式 Markdown 展示。
- 强化 agent loop 在验证、运行时间和工具轮数上的护栏。
- 增加本地 coding task benchmark 覆盖。

长期：

- 把任务感知上下文压缩继续打磨成更可靠的长会话记忆层。
- 深入完善 Anthropic 原生协议支持，包括 streaming、thinking/cache 行为和 provider-specific message 语义。
- 做长期记忆，用来保存稳定的项目知识、用户偏好和可复用任务上下文。
- 探索多代理编排，支持 planner/executor/reviewer 工作流，以及并行 coding task。
