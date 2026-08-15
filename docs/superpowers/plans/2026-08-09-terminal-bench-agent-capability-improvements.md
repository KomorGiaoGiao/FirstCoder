# FirstCoder Terminal-Bench 代理能力提升实施计划

> **执行方式：** 按阶段实施。每个阶段先补测试，再做最小完整改动，最后运行专项回归。不要把 Terminal-Bench、Aider Polyglot 和 TUI 的交互协议混为一谈。

**目标：** 在不更换底层模型、不引入题目特定答案的前提下，补齐 FirstCoder 在非交互基准中的观察、决策、执行、验证和恢复闭环，提高有效环境中的任务通过率，并把基础设施失败与代理能力失败分开统计。

**架构原则：** 复用现有 provider-neutral 消息、工具结果、附件、上下文压缩、后台任务和 session resume 能力；新增行为通过明确运行模式和策略进入 `AgentLoop`，不把 benchmark 特例散落到具体工具或 Textual widget 中。

**技术栈：** Python 3.11+、pytest、append-only JSONL、Harbor、OpenAI-compatible provider、现有 `AgentLoop` / `ToolRegistry` / `ContextWindowManager`。

---

## 一、分析基线

### 1.1 日志范围与结果

主要分析目录：

```text
benchmark/runs/harbor/tb2-gpt-5.6-luna-proxy-retry3/2026-07-26__17-49-00
```

该运行包含 267 个 trial。现有分析得到：

- 217 个异常记录，只有 54 个任务取得明确 reward；
- reward-only 为 22 通过、32 失败，通过率约 40.74%；
- Harbor 端到端通过率约 8.24%；
- 异常主要来自 Docker/环境、依赖安装、Python bootstrap、网络和超时；
- 无基础设施异常的小样本运行达到过 4/8 与 6/8；
- 失败任务平均 provider/tool 调用次数和耗时明显高于通过任务，存在低效重试和收尾不足。

这些结果说明模型和基础工具链能够解决一部分任务，但成功率同时受到基础设施可靠性、工具反馈质量、循环预算和代理控制策略影响。

### 1.2 版本边界

主要日志使用 FirstCoder 0.1.9；当前工作区为 0.1.12，提交 `3fae7a6`。当前版本已经具备：

- 子进程进程组和超时后的进程树回收；
- 工具输出 archive/retrieve；
- provider 调用、工具轮次和总时间上限；
- 上下文自动压缩及未消费工具结果保护；
- 后台任务、子代理、TaskPlan 和 MCP 按需暴露；
- TUI 图片附件及 OpenAI `image_url` 转换；
- benchmark 专用 system prompt；
- Aider Polyglot 可选 verifier-feedback 插件。

实施前后必须使用当前版本建立新基线，不能把旧日志中的每个现象都当成当前仍存在的缺陷。

### 1.3 评测协议边界

- **TUI：** 允许真人输入，保留 `ask_user`。
- **Terminal-Bench：** 单次非交互任务，不允许等待真人，也不允许 verifier 反馈修复轮。
- **Aider Polyglot：** 首轮仍是非交互执行；仅显式启用插件后，允许一次由 Harbor 注入的 verifier 输出修复轮。该机制不是 `ask_user`。
- **多模态：** 非交互不等于纯文本。任务明确引用的图片可以由 adapter 在首轮作为附件注入。

---

## 二、失败模式与改进机会

| 编号 | 失败模式 | 当前差距 | 优先级 | 预计收益 |
| --- | --- | --- | --- | --- |
| A1 | 非零命令退出后模型只看到退出码 | `stdout` / `stderr` 已在 `ToolResult.data`，但 provider 只看到 `content` | P0 | 高 |
| A2 | 超长输出截掉尾部错误 | 通用截断只保留开头，编译器和测试摘要常在尾部 | P0 | 高 |
| A3 | Harbor 传入较高工具轮数但 provider 上限仍较低 | `_benchmark_limits()` 只覆盖 `max_tool_rounds` | P0 | 高 |
| A4 | benchmark 暴露 `ask_user` | 工具默认无条件注册，模型理论上可暂停等待不存在的真人 | P0 | 中 |
| A5 | 模型过早给出最终答案 | AgentLoop 没有 benchmark 完成门禁 | P0 | 高 |
| A6 | 相同命令或错误反复出现 | 没有通用停滞指纹和切换策略提示 | P0 | 中高 |
| A7 | 图片任务靠模型手工解析文件字节 | TUI 附件链存在，但 Harbor adapter 只传 `--message` | P0 | 高（图片题） |
| A8 | 安装、编译、服务启动使用短超时 | `shell` 默认 30 秒，模型需自行判断和覆盖 | P1 | 中高 |
| A9 | 后台状态高频轮询 | 后台能力存在，但没有最低轮询间隔或退避策略 | P1 | 中 |
| A10 | 工具 schema 和控制字段偏多 | benchmark 默认继承 TUI 全工具集、TaskPlan、delegate 和后台控制 | P1 | 中 |
| A11 | 每次 shell 是新进程 | `cd` / `export` / venv 激活不会跨调用保留 | P1 | 中 |
| A12 | 长期服务缺少 readiness 抽象 | 可用后台 shell 实现，但缺少结构化 start/status/logs/stop | P1 | 中高 |
| A13 | 精确文本编辑失败后缺少安全建议 | `edit` / `apply_patch` 只做精确匹配 | P1 | 中 |
| A14 | provider 瞬态失败恢复有限 | 已有错误分类和有限重试，但同步/流式策略不完全统一 | P1 | 中 |
| A15 | 长上下文中验收状态容易稀释 | 有 TaskPlan 和压缩摘要，但没有 benchmark 执行证据账本 | P1 | 中 |
| A16 | 环境安装占用多数端到端失败 | 需要 cache、wheelhouse、镜像预拉和网络预检 | Infra | 很高 |

---

## 三、目标设计

### 3.1 运行能力而非 benchmark 分支散落

第一阶段使用构造参数控制能力，最终可收敛为轻量配置：

```python
@dataclass(frozen=True, slots=True)
class AgentRuntimeCapabilities:
    allow_user_input: bool = True
    enable_completion_gate: bool = False
    enable_stagnation_guard: bool = False
    enable_delegate_tool: bool = True
    expose_planning_tools: bool = True
```

这些能力由 app/factory 统一装配，具体工具不能读取全局 `--benchmark` 状态。

### 3.2 模型可见的命令结果

命令类工具失败时统一生成：

```text
命令执行失败（exit_code=1）

stdout:
...

stderr:
...
```

约束：

- `ToolResult.data` 继续保存结构化原值；
- `ToolResult.content` 提供足够诊断；
- 超时、取消和启动失败同样附带已产生输出；
- 空 stdout/stderr 不产生空标题；
- 避免 error 和正文重复；
- 截断默认保留头部和尾部，并标记省略字符数。

### 3.3 统一预算

Benchmark 预算必须满足：

```text
max_provider_calls >= max_tool_rounds + completion/compaction/retry reserve
```

第一版建议：

```python
max_provider_calls = max(base.max_provider_calls, max_tool_rounds + 40)
```

总时间应早于 Harbor 硬超时，并为日志导出和 verifier 留出安全余量。若 adapter 无法取得 Harbor timeout，先保持现有内部上限，并由运行配置保证外部超时更大。

### 3.4 完成门禁

仅 benchmark 模式启用。门禁不猜测隐藏测试，只基于本轮可观察事实决定是否追加一次收尾调用：

- 是否执行过写入工具；
- 是否执行过命令或诊断验证；
- 最近一次验证是否成功；
- 是否存在未完成后台任务；
- 本轮失败工具的最新摘要；
- 当前 completion gate 触发次数，以及门禁后是否又出现了新的修改证据。

当模型准备结束但有修改未验证、最近验证失败或后台任务未结束时，追加 runtime instruction。单轮默认只触发一次；若门禁后又出现新的修改证据，可重新触发一次，总上限为两次，避免造成无限循环。

### 3.5 停滞检测

每个工具结果生成指纹：

```text
tool_name + normalized_arguments + ok + error/exit_code + output_tail_digest
```

第一版策略：

- 完全相同的失败连续出现 2 次：下一次请求提示不要原样重试；
- 连续 3 次：拒绝第四次完全相同调用，返回结构化停滞错误；
- `background_status` 对同一运行中 job 的重复查询单独计数；
- 文件内容或 git diff 变化会重置相关停滞计数；
- 仅 benchmark 默认启用，TUI 保持兼容。

### 3.6 多模态附件

复用 `UserAttachment`、`attach_path()`、session attachment store 和 provider image projection。

新增：

- CLI 支持可重复 `--attachment PATH`；
- Harbor adapter 从 instruction 中提取明确引用的本地图片路径；
- 只允许工作区内的常见图片文件；
- 路径必须存在且符合现有数量、大小限制；
- 未声明 `supports_vision` 时明确报错，不静默丢图；
- Aider 第二轮不重复附加首轮图片，session 已保留首轮消息。

第一版不递归扫描整个工作区，避免误附加无关图片或 verifier 资源。

### 3.7 工具路由

第二阶段再精简工具面：

- benchmark 不暴露 `ask_user`；
- 简单单任务默认不暴露 delegate；
- TaskPlan 工具按任务复杂度暴露；
- `think` 可从 benchmark 工具集中移除；
- MCP 保持现有按需激活；
- background 控制只附加到适合长任务的工具。

### 3.8 执行上下文与长期进程

优先支持显式 `cwd/env`，每次调用仍是独立进程。服务类任务后续新增结构化 `process_start/process_status/process_logs/process_stop`，复用现有后台任务和进程树回收，不在第一阶段引入任意 PTY。

---

## 四、分阶段实施

### Phase 0：当前版本基线

**任务：**

- [ ] 运行工具、CLI、Harbor adapter、多模态、AgentLoop 专项测试；
- [ ] 单并发复测一个快速文本任务；
- [ ] 记录环境成功率、reward-only 和端到端结果；
- [ ] 保存 commit、模型配置和 Harbor 参数，不记录 secret。

**专项命令：**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_execution_tools.py tests/test_cli.py `
  tests/test_harbor_adapter.py tests/test_multimodal_input.py tests/test_agent_context_loop.py -q
```

### Phase 1：工具反馈和预算一致性

**文件：**

- Modify: `firstcoder/utils/text.py`
- Create: `firstcoder/tools/command_result.py`
- Modify: `firstcoder/tools/shell.py`
- Modify: `firstcoder/tools/python_exec.py`
- Modify: `firstcoder/tools/diagnostics.py`
- Modify: `firstcoder/agent/loop_limits.py`
- Modify: `firstcoder/cli.py`
- Test: `tests/test_execution_tools.py`
- Test: `tests/test_cli.py`

**任务：**

- [x] 增加头尾截断；
- [x] 统一命令结果格式化；
- [x] 非零退出、超时和取消向模型展示 stdout/stderr；
- [x] benchmark 覆盖工具轮数时同步提高 provider 调用上限；
- [x] 保持结构化 `data` 兼容。

**预计：** 8–10 个文件，180–300 行 diff。

### Phase 2：非交互能力和工具面

**文件：**

- Modify: `firstcoder/tools/builtin.py`
- Modify: `firstcoder/app/factory.py`
- Modify: `firstcoder/cli.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_app_factory.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_harbor_adapter.py`

**任务：**

- [x] `allow_user_input` 控制 `ask_user` 注册；
- [x] benchmark 禁用真人输入；
- [x] TUI 保持默认行为；
- [x] Aider feedback 继续通过插件 resume 同一 session；
- [x] 为 delegate/planning 路由保留明确装配点。

**预计：** 6–8 个文件，100–180 行 diff。

### Phase 3：Harbor 多模态首轮附件

**文件：**

- Modify: `firstcoder/cli.py`
- Modify: `benchmark/harbor/shared/firstcoder_agent.py`
- Modify: provider/model capability 配置
- Test: `tests/test_cli.py`
- Test: `tests/test_harbor_adapter.py`
- Test: `tests/test_multimodal_input.py`

**任务：**

- [x] CLI 接收多个附件；
- [x] adapter 解析 instruction 中明确引用的工作区图片；
- [x] 首轮把图片传入 `run_user_turn()`；
- [x] 限制路径、扩展名、数量和文件大小；
- [x] 视觉能力声明不匹配时明确失败；
- [x] resume feedback turn 不重复附件。

**预计：** 5–7 个文件，180–320 行 diff。

### Phase 4：完成门禁和停滞检测

**文件：**

- Create: `firstcoder/agent/execution_evidence.py`
- Create: `firstcoder/agent/stagnation.py`
- Modify: `firstcoder/agent/loop.py`
- Modify: `firstcoder/agent/tool_execution.py`
- Modify: `firstcoder/context/prompts/benchmark_agent_instructions.md`
- Test: `tests/test_agent_context_loop.py`
- Test: `tests/test_background_jobs.py`

**任务：**

- [x] 观察所有工具结果；
- [x] 累积修改、验证、失败和后台状态证据；
- [x] 最终答复前最多触发一次完成门禁；
- [x] 相同失败两次提示切换策略；
- [x] 相同失败三次后拒绝原样第四次调用；
- [x] 后台轮询使用独立规则；
- [x] 新用户回合、resume 和任务边界正确重置。

**预计：** 6–8 个文件，350–600 行 diff。

### Phase 5：工具路由和执行增强

**任务：**

- [x] benchmark 精简无关工具和控制字段；
- [x] 长命令超时档位与后台建议；
- [x] 安全编辑失败建议及 diff/no-op 反馈；
- [x] 结构化长期进程工具；
- [x] 显式环境变量覆盖，继续过滤敏感环境；
- [x] provider 瞬态失败统一退避；
- [x] 预算化 tester/reviewer。

**预计：** 15–25 个文件，1,100–2,000 行 diff。

### Phase 6：遥测和 Harbor 基础设施

**任务：**

- [x] 记录失败分类、重复调用、首次修改、验证次数和停止原因；
- [x] 自动汇总环境成功率、reward-only 与端到端通过率；
- [x] wheelhouse/cache 挂载；
- [x] 镜像预拉与 provider/network preflight；
- [ ] 以固定任务集完成一次阶段 A/B（任务集和运行器已固化，待 smoke 后执行）。

**预计：** 8–14 个文件，500–900 行 diff。

---

## 五、测试矩阵

### 5.1 单元测试

- 非零退出同时含 stdout/stderr；
- 超时含部分输出；
- 头尾截断保留尾部错误；
- benchmark tool/provider 预算关系；
- benchmark 无 `ask_user`，普通 app 有；
- Aider feedback resume 行为不变；
- CLI 单个和多个附件；
- 图片路径越界、缺失、非图片和数量超限；
- completion gate 默认仅触发一次，门禁后出现新修改证据时最多重启一次；
- 修改后未验证会触发门禁；
- 成功验证后正常结束；
- 重复失败指纹和参数变化；
- 后台 job 轮询不误判其他工具。

### 5.2 回归测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_execution_tools.py tests/test_tools.py `
  tests/test_cli.py tests/test_app_factory.py tests/test_multimodal_input.py `
  tests/test_harbor_adapter.py tests/test_agent_context_loop.py `
  tests/test_background_jobs.py -q

.\.venv\Scripts\python.exe -m pytest -q
```

### 5.3 基准回归集

优先单并发复测：

- `chess-best-move`：图片附件、目标文件和进程清理；
- `configure-git-webserver`：系统状态、安装和服务验收；
- `compile-compcert`：长编译和错误尾部；
- `qemu-alpine-ssh`：长期进程、锁和 readiness；
- `adaptive-rejection-sampler`：依赖安装和时间预算；
- `tune-mjcf`：文件修改与验证闭环。

每次报告：环境/agent setup 成功率、reward-only 通过率、端到端通过率、provider/tool 调用和耗时，以及失败类别变化。

---

## 六、代码量与提交策略

| 范围 | 文件数 | 预计 diff |
| --- | ---: | ---: |
| Phase 1–2：反馈、预算、非交互 | 12–16 | 300–480 行 |
| Phase 3：多模态 benchmark 接入 | 5–7 | 180–320 行 |
| Phase 4：完成门禁和停滞 | 6–8 | 350–600 行 |
| Phase 5：工具和执行增强 | 15–25 | 1,100–2,000 行 |
| Phase 6：遥测和基础设施 | 8–14 | 500–900 行 |
| **合计（去重）** | **35–50** | **2,500–4,300 行** |

按 Phase 独立提交。Phase 4 如超过 600 行，可拆为“执行证据/完成门禁”和“停滞检测”，但每个提交都必须可运行。

---

## 七、明确不做

- 不为具体 Terminal-Bench 题目硬编码答案或 verifier 规则；
- 不读取、修改或绕过隐藏 verifier；
- 不把 Aider verifier feedback 泛化到 Terminal-Bench；
- 不为提高分数自动切换不同模型；
- 不把 benchmark 特例写进 provider 协议层；
- 不在第一阶段引入完整 PTY 或任意持久 shell；
- 不一次性重写 AgentLoop、工具系统或上下文系统。

---

## 八、完成标准

- P0 功能都有单元测试并通过完整测试套件；
- TUI 交互、Aider feedback 和普通单轮 CLI 无回归；
- 固定基准回归集完成至少一次单并发运行；
- reward-only 与端到端通过率分开报告；
- 停滞检测和完成门禁不会导致无限循环；
- 多模态附件只来自任务明确引用且位于工作区内的图片；
- 文档记录仍未解决的基础设施风险和后续阶段。

---

## 九、评审加固与已知后续项

### 9.1 本轮评审后追加的加固

- 完成门禁现在把成功的 `process_start`/`process_stop` 视为状态修改，启动或停止长期服务后必须再验证目标才能收尾（`firstcoder/agent/execution_evidence.py`）。
- 非交互 benchmark 运行在工具路由层直接隐藏并拒绝 `ask_user`，即使调用方显式传入该工具也不会暴露给模型（`firstcoder/agent/loop.py`）。
- `git_diff` 仍可作为完成门禁的修改证据，但不再被视为实际修改而清空停滞失败链（`firstcoder/agent/execution_evidence.py`）。
- 上述加固均补充了聚焦回归测试（`tests/test_agent_benchmark_guardrails.py`）。

### 9.2 已知后续项（本轮不做，需单独跟进）

- Aider verifier feedback 通过同一 session 恢复时，会把反馈提示作为当前 `benchmark_task`，从而丢失首轮题面派生的路由和验收契约；后续应在会话元数据中保留原始 benchmark task，并把反馈作为追加指令而不是替换任务。
- `fetch` 目前仅靠提示词约束抓取题面已知 URL，尚未在工具层校验目标域名；后续应加入 URL allowlist 校验并补测任意 URL 被拒绝。
- `ProcessManager` 状态仅驻留内存，跨 CLI 进程或 Harbor resume 无法查询/停止上一轮保留的服务，且计数从 `proc_0001` 重新开始；后续应持久化进程注册表。
- 显式 HTTP 目标覆盖判定在出现任意广义测试命令（如 `pytest`）时会被视为已覆盖，可能绕过对题面指定 URL 的最终探测；后续应把广义测试与目标探测分开判定。
- 固定运行器尚未透传 `supports_vision` 关闭开关；为非视觉模型跑含图片题时需要手动配置，后续应在运行器暴露该参数。
- 固定六题 Terminal-Bench A/B 尚未完整执行；本轮只完成本地单元/回归测试和静态检查，不能据此宣称外部 benchmark 分数已经提升。
