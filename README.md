<p align="center">
  <img src="assets/firstcoder-logo.png" alt="FirstCoder logo" width="168">
</p>

<h1 align="center">FirstCoder</h1>

<p align="center">
  <strong>一个从零拆开 coding agent 的 Python 学习项目。</strong>
</p>

<p align="center">
  <a href="#快速开始"><img alt="Python" src="https://img.shields.io/badge/Python-3.11%2B-3776AB?style=flat-square&logo=python&logoColor=white"></a>
  <a href="#tui-体验"><img alt="Textual TUI" src="https://img.shields.io/badge/Textual-TUI-5B5BD6?style=flat-square"></a>
  <a href="#provider-配置"><img alt="OpenAI compatible" src="https://img.shields.io/badge/OpenAI-Compatible-111827?style=flat-square"></a>
  <a href="#开发"><img alt="pytest" src="https://img.shields.io/badge/pytest-tested-0A9EDC?style=flat-square&logo=pytest&logoColor=white"></a>
</p>

<p align="center">
  <a href="#项目定位">项目定位</a>
  · <a href="#快速开始">快速开始</a>
  · <a href="#tui-体验">TUI</a>
  · <a href="#命令">命令</a>
  · <a href="#架构">架构</a>
  · <a href="#开发">开发</a>
</p>

---

FirstCoder 是一个本地 coding agent 学习项目。它不是要把成熟工具重新包装一遍，而是把一个终端编码代理拆成能读、能跑、能改的工程样本：provider 怎么接入、agent loop 怎么驱动工具、权限怎么暂停、上下文怎么压缩、session 怎么恢复、TUI 怎么把状态展示出来。

如果你想在简历或作品集中展示“我真的理解 coding agent 怎么工作”，FirstCoder 就是为这个目标做的。它可以实际运行、真实调用工具、保存会话、处理权限，也刻意保留足够清晰的模块边界，让后来的人能顺着代码把一条 agent 链路读完。

> [!NOTE]
> 这是学习和实验项目，不是成熟产品替代品。README 里的截图来自真实 TUI 运行，但功能和交互仍在持续迭代。

## 项目定位

FirstCoder 的核心卖点不是“我又做了一个聊天壳”，而是把 coding agent 里最容易被黑盒吞掉的部分摊开：

| 你想学的问题 | FirstCoder 里可以看的地方 |
| --- | --- |
| 模型怎么从“回复”变成“调用工具” | `firstcoder/agent`、`firstcoder/providers` |
| 工具调用如何落到文件读写、shell、git、网络请求 | `firstcoder/tools` |
| 为什么 agent 不能随便删文件或跑命令 | `firstcoder/permissions` |
| 长对话怎么压缩，resume 后怎么继续 | `firstcoder/context`、`firstcoder/session` |
| TUI 怎么显示流式输出、工具状态和权限请求 | `firstcoder/app` |
| 怎么用小 benchmark 验证 agent loop | `benchmark/local_pytest`、`firstcoder/eval` |

适合放在简历里的看点：

- **完整 agent loop**：不是只接 LLM API，而是包含 tool calling、权限、上下文和 session。
- **真实工程边界**：provider、tools、permissions、context、TUI 分层清楚，方便解释架构取舍。
- **可运行演示**：可以在本地启动 TUI，展示流式输出、工具调用、权限暂停和历史会话。
- **学习友好**：代码不是为了炫技压缩到不可读，而是为了把关键链路讲清楚。

它不承诺：

- 完整替代成熟 coding agent。
- 所有 provider 的高级特性都已覆盖。
- TUI 和权限体验已经最终定型。
- 全量 benchmark 表现稳定。

## TUI 体验

默认运行 `firstcoder` 会进入 Textual TUI。界面刻意保持朴素，方便观察 agent 状态，而不是隐藏内部过程。

![FirstCoder empty TUI](docs/images/tui-empty.png)

你能在界面里直接看到：

- 顶部栏：项目名、当前 activity、session、provider/model、权限模式、当前项目。
- 对话区：用户消息、FirstCoder 回复、工具调用、工具结果、权限通知。
- Activity 行：`thinking`、`streaming`、`running tool`、`waiting permission`、`idle` 等状态。
- 输入框：普通消息和 slash command 共用。

普通对话会以 Markdown 渲染输出：

![FirstCoder chat](docs/images/tui-chat.png)

工具调用不是藏在最后的日志里，而是直接出现在对话流中。工具完成后，如果模型还在读取工具结果，activity 会继续显示动画：

```text
thinking [.  ] reading shell result
thinking [.. ] reading shell result
thinking [...] reading shell result
```

![FirstCoder tool calls](docs/images/tui-tools.png)

权限请求会暂停 agent，等待用户选择：

![FirstCoder permission request](docs/images/tui-permission.png)

这几个界面不是为了把 FirstCoder 包装成“成品 IDE”，而是为了让学习者能看到：一个 coding agent 在每一步到底处于什么状态。

## 工程亮点

| 亮点 | 为什么值得看 |
| --- | --- |
| OpenAI-compatible provider 主线 | 大多数模型供应商都能通过同一套 provider 适配，便于学习协议边界 |
| 流式输出和工具事件分离 | 文本流、reasoning delta、tool started/finished 都有独立 UI 路径 |
| 权限系统 | `ALLOW / ASK / DENY`、权限模式、长期授权都在本地实现 |
| append-only session store | 会话事实用 JSONL 事件保存，resume 和 share 都从事件重建 |
| 分层 context compaction | L1-L4 压缩策略让长上下文处理更像真实 agent |
| 本地 pytest benchmark | 用小任务快速验证 agent 是否会读题、改代码、跑测试、停止 |

当前 provider 主线是 **OpenAI Chat Completions-compatible** 协议。OpenAI-compatible 流式文本、function tool call delta 拼装和基础 `reasoning_delta` 转发都在这条路径上实现；当 provider 抛出 `PROMPT_TOO_LONG` 时，FirstCoder 会尝试触发上下文压缩恢复，再重试一次请求。

## 快速开始

最短路径是本地开发安装。FirstCoder 目前更适合作为学习项目从源码运行：

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
```

启动 TUI：

```sh
firstcoder
```

或者显式启动：

```sh
firstcoder --tui
```

单轮消息：

```sh
firstcoder --message "用一句话介绍这个项目"
```

行式交互：

```sh
firstcoder --interactive
```

从源码运行：

```sh
.venv/bin/python -m firstcoder --tui
```

## Provider 配置

FirstCoder 不把 provider 配置写死在代码里。你可以用全局配置、项目配置、`.env` 或环境变量控制模型来源。推荐先创建全局配置：

```sh
firstcoder config init
firstcoder config path
firstcoder config show
```

默认全局配置路径：

```text
~/.config/firstcoder/config.toml
```

项目级配置路径：

```text
./firstcoder.toml
```

配置示例：

```toml
model = "yurenapi/gpt-5.5"

[provider]
type = "openai-compatible"
name = "yurenapi"
base_url = "https://yurenapi.cn/v1"
api_key_env = "FIRSTCODER_API_KEY"

[permissions]
mode = "ask"

[ui]
theme = "default"
```

API key 建议放环境变量，不要写进仓库：

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

### 常见 provider

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

Anthropic provider 目前是实验性实现，暂不覆盖完整的 Anthropic 原生 thinking/cache/streaming 能力。项目也暂不承诺 OpenAI Responses API、reasoning 内容完整保存展示和多模态输入输出。

## 命令

FirstCoder 目前有两层入口：外层 CLI 负责启动、配置和单轮调用；TUI 内部 slash command 负责 session、context 和权限模式。

### CLI

| 命令 | 作用 |
| --- | --- |
| `firstcoder` | 在交互终端中默认启动 TUI |
| `firstcoder --tui` | 启动 Textual TUI |
| `firstcoder --message "..."` | 跑单轮消息 |
| `firstcoder --interactive` | 启动行式 REPL |
| `firstcoder --project <path>` | 指定项目根目录 |
| `firstcoder --data-root <path>` | 指定 session / permissions 数据目录 |
| `firstcoder --session-id <id>` | 创建或复用指定 session |
| `firstcoder --provider <name>` | 覆盖 provider |
| `firstcoder --auto-approve` | REPL 中自动选择 `allow_once` |
| `firstcoder --max-tool-rounds <n>` | 覆盖单轮最大工具轮数 |
| `firstcoder config init` | 创建全局配置 |
| `firstcoder config path` | 查看全局和项目配置路径 |
| `firstcoder config show` | 查看生效 provider 配置，不输出密钥 |

### TUI slash commands

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
| `/mode conservative` | 切换到保守模式 |
| `/mode standard` | 切换到标准模式 |
| `/mode aggressive` | 切换到激进模式 |
| `/mode bypass` | 切换到绕过模式，适合受控本地实验 |

仍在计划中的体验包括 `/help`、`/new`、无参数交互式 `/resume`、长期授权列表和撤销命令。

## 工具

默认 TUI 会注册一组足够覆盖 coding-agent 学习场景的工具。它们不是插件市场，而是为了展示一个 agent 从“想做事”到“真实执行”的完整链路。

| 类别 | 工具 |
| --- | --- |
| 文件浏览 | `ls`、`tree`、`glob`、`view`、`read_multi`、`grep` |
| 文件修改 | `write`、`edit`、`delete`、`apply_patch` |
| 执行与验证 | `shell`、`python_exec`、`diagnostics` |
| Git | `git_status`、`git_diff`、`git_log` |
| 网络 | `fetch`、`web_search` |
| Agent 辅助 | `think`、`todo`、`ask_user` |

工具调用会经过权限系统。模型可以请求工具，但真正执行前会由 permission manager 判断。

## 权限模型

权限模型是这个项目最值得拆读的部分之一：它把“模型想做什么”和“程序允许做什么”分开。模型可以请求读取、写入、执行命令或访问网络，但这些动作不会自动发生。

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
DENY  -> 拒绝执行
```

权限模式：

| 模式 | 倾向 |
| --- | --- |
| `conservative` | 更保守，更多操作需要确认 |
| `standard` | 默认平衡模式 |
| `aggressive` | 对项目内常见验证命令和普通写入更主动 |
| `bypass` | 绕过默认策略，适合受控本地实验 |

长期授权来自用户选择 `allow_always_same_scope`，会保存到当前 data root 下的 `permissions.json`。

## Session 与上下文

一个 coding agent 如果不能恢复历史、不能控制上下文，很快就会从“能聊天”变成“不可维护”。FirstCoder 用 append-only event log 保存会话事实，再从事件重建上下文视图。

默认数据目录是：

```text
<project-root>/.firstcoder/
```

这里会保存：

- append-only JSONL session event log
- session catalog 所需信息
- context checkpoint / archive / compact 事件
- 权限长期授权
- share/export 产物

Resume 的底层事实来自完整事件日志；checkpoint 只影响下一轮发给 provider 的上下文投影，不是历史存储边界。

上下文压缩采用分层思路：

- L1：压缩旧工具输出，保留工具名、规模和关键元信息。
- L2：把超大工具结果归档到本地 store，在上下文中保留可追踪占位符。
- L3：按内容类型压缩 grep、log、diff、json、text 等结果。
- L4：上下文压力仍然过高时，用 LLM 总结旧历史并保留最近几轮原文。
- L5：手动 compact 入口，方便观察压缩前后的变化。

## 架构

```text
user input
   |
   v
Textual TUI / CLI
   |
   +--> slash commands
   |       session / context / compact / permission mode
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
  agent/        agent loop、运行期 session、用户输入恢复、循环护栏
  app/          Textual TUI、命令路由、运行期组装
  config/       配置文件、.env、环境变量加载
  context/      session event log、上下文投影、checkpoint、archive、compact
  eval/         benchmark adapter、patch 提取和预测生成
  permissions/  权限策略、长期授权、项目级 permission manager
  providers/    模型 provider 抽象和厂商适配
  session/      session catalog、resume、transcript、share、redaction
  tools/        内置工具、schema、执行结果和权限登记
  utils/        JSON、schema、sandbox、subprocess、git 等基础工具
benchmark/      本地 pytest benchmark 和实验入口
docs/           设计记录、实现计划和截图资源
tests/          pytest 测试
```

## 本地 benchmark

轻量本地 pytest benchmark 在：

```text
benchmark/local_pytest/
```

它会生成小型 Python 任务仓库，让 FirstCoder 修改代码并用 pytest 判分。这个 benchmark 不追求榜单分数，更像一个本地探针：观察 agent loop 是否能完成“读题 -> 找文件 -> 修改 -> 跑测试 -> 收工”。

运行示例：

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

更多说明见 [docs/LOCAL_PYTEST_BENCHMARK.md](docs/LOCAL_PYTEST_BENCHMARK.md)。

## 开发

这个仓库的开发目标是：每个关键链路都有可读实现和聚焦测试。改 TUI、权限、provider、工具执行或上下文逻辑时，优先跑对应测试，再考虑全量 pytest。

安装开发依赖：

```sh
python -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

运行全量测试：

```sh
.venv/bin/python -m pytest
```

聚焦测试：

```sh
.venv/bin/python -m pytest tests/test_app_tui.py -q
```

打包入口：

```sh
python -m pip install build
python -m build
```

本地全局安装测试：

```sh
pipx install --force .
firstcoder --tui
```

项目尽量避免测试依赖真实 API key 或网络。provider、tool、agent loop、context recovery、permission 和 eval 相关行为应优先使用 fake、fixture 或本地临时目录覆盖。

## 路线图

- 补齐 `/help`、`/new`、无参数交互式 `/resume`。
- 增加长期授权查看和撤销命令。
- 继续打磨 TUI 的信息密度、状态动画和 markdown 流式渲染。
- 完善 agent loop 护栏：验证成功后收工、provider 调用次数上限、单轮耗时上限和更合理的工具轮数预算。
- 继续完善本地 pytest benchmark 和 SWE-bench Lite 适配。
- 让这个项目继续保持“能跑、能拆、能学习”的节奏。
