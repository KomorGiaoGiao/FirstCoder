# FirstCoder × Harbor：Aider Polyglot 测评报告

> 本文对应一次已完成的本地 Harbor 运行，结果文件为同目录的 `result.json`。它记录的是 **FirstCoder agent 在隔离容器中完成 Exercism 风格编程任务** 的结果，不等同于某个底层模型的通用能力排行榜成绩。

## 1. 结论速览

| 项目 | 结果 |
| --- | --- |
| 测评集 | Aider Polyglot（本地缓存版本） |
| 总任务数 | 225 |
| 已完成 | 225 / 225 |
| 获得 verifier reward 的任务 | 221 |
| reward = 1（通过） | 213 |
| reward = 0 | 8 |
| 有 reward 的通过率 | **213 / 221 = 96.38%** |
| Harbor 端到端 Mean | **94.67%** |
| 无 reward 的异常任务 | 4 |
| 运行并发 | 4 |
| Harbor 自动/人工恢复累计重试 | 77 |
| 运行时间 | 2026-07-26 12:07:28 至 2026-07-27 12:12:09（约 24 小时 5 分） |

`96.38%` 是 **已获得真实 verifier reward 的 pass@1**：`213 / (213 + 8)`。

`94.67%` 是 Harbor 当前的端到端 Mean：`213 / 225`。它会把没有生成 reward 的基础设施异常按 0 计入，因此适合反映“这次整套评测从启动到验证的成功率”，但不宜单独作为模型代码能力分数。

> [!IMPORTANT]
> 不应把 94.67% 与 96.38% 混为同一指标。前者包含容器、网络和 verifier 脚本问题；后者只统计拿到明确 0/1 判分的任务。

## 2. 运行对象与配置

### Agent 与模型

- Agent：`benchmark.harbor.firstcoder_agent:FirstCoderHarborAgent`
- 模型标识：`gpt-5.6-luna`
- Provider：OpenAI-compatible / Yuren
- 推理强度：`high`
- 单题最大工具轮数：120
- 全局 Skills：关闭，以减少非任务相关上下文带来的波动

### Harbor 执行配置

- 数据集路径：`.local/harbor-datasets/aider-polyglot`
- 并发：4
- 单次自动重试：最多 2 次，仅覆盖 `NonZeroAgentExitCodeError`
- agent setup timeout multiplier：3
- 额外恢复：在运行中针对网络、非零退出与 verifier 超时进行定向恢复
- 评分方式：每题由数据集提供的独立 verifier 生成 `reward.txt`；成功为 `1.0`，失败为 `0.0`

### Aider 式反馈恢复

此次运行使用了 FirstCoder 的 Harbor feedback plugin。对于首次得到明确 `reward=0` 的任务，plugin 会把 verifier 输出反馈给同一 FirstCoder session，再执行一次修复和复验。它模拟了 Aider benchmark 中“先测试、再基于失败输出修复”的交互式工作流，而不是将每题限定为一次静态代码生成。

## 3. 与 Aider 公开榜单的关系

### 3.1 方法学上的对应关系

Aider 的公开 benchmark 关注的不是“回答是否看起来像代码”，而是模型能否根据自然语言需求修改现有实现，并最终通过单元测试。Aider 公开说明中，其早期 code-editing benchmark 使用 133 道 Exercism Python 练习；每题提供说明、初始实现文件和独立测试。首轮失败后，Aider 只向模型返回测试错误输出，再给一次修复机会；最终柱状成绩是两次编码尝试后的结果。

本次 FirstCoder 运行采用相同的核心闭环：**初始实现 → 独立 verifier → 失败输出反馈 → 同 session 修复 → 再验证**。因此，`Aider Polyglot` 与 Aider 官方榜单在“可执行代码编辑 + 黑盒 verifier feedback”的评测思想上具有可比性。

### 3.2 为什么不能直接把 96.38% 映射为官方榜单排名

| 维度 | 本次 FirstCoder × Harbor 运行 | Aider 公开榜单 |
| --- | --- | --- |
| 任务集 | 本地 Aider Polyglot，225 题、6 种语言 | 官方榜单的具体任务集与版本由 Aider 发布页定义；早期编辑榜为 133 道 Python Exercism 题 |
| Agent | FirstCoder Harbor adapter | Aider 自身的编辑循环与 edit format |
| 模型路由 | `gpt-5.6-luna` 经 OpenAI-compatible provider | 官方榜单记录的是特定模型、模型版本、edit format 与当时成本 |
| 交互 | verifier feedback plugin，同 session 二轮修复 | Aider 对首轮测试失败返回错误输出并给一次修复机会 |
| 异常计分 | Harbor 的端到端 Mean 会将无 reward 异常按 0；另提供 reward-only 通过率 | 官方榜单侧重“completed correctly”和 edit format 正确率 |
| 基础设施 | Docker/Colima、网络代理、语言包下载均进入实际运行 | 官方结果使用其固定 harness，不能假设具有相同网络/缓存条件 |

因此，**213/221 = 96.38% 不是 Aider 官方 leaderboard 分数，也不能据此声称超过、等同于或排名在任何公开模型之前**。它的正确表述是：在本地锁定的 Aider Polyglot + Harbor + FirstCoder 配置下，获得有效 reward 的题目通过率为 96.38%。

### 3.3 从公开榜单可以得到的工程启发

1. **结果必须包含编辑落盘与测试通过**：Aider 将“完成代码任务”定义为解题成功且修改正确应用到文件；本次也以独立 verifier 的 reward 作为最终依据，而不是 agent 自述。
2. **二轮反馈是评测协议的一部分**：公开说明明确将首轮失败后的 test error feedback 纳入最终结果。本次的 feedback plugin 复现了这一行为，所以应同时报告是否启用该插件。
3. **编辑协议会影响成绩与成本**：Aider 的公开榜还单列 edit format 正确率，并指出不同 edit format 会影响可靠性和 token 成本。FirstCoder 以工具调用直接修改工作区，不能拿官方的 edit-format 指标来替代 FirstCoder 的工具执行成功率。
4. **公开榜重视可复现性，而非单次百分比**：模型版本、题目版本、提示词、尝试次数、测试反馈策略、超时和环境条件都应固化。本报告保留 `config.json`、`lock.json`、`result.json` 以及单题日志，供之后复跑或横向比较。

### 3.4 公开资料

- [Aider LLM Leaderboards](https://aider.chat/docs/leaderboards/)：公开榜入口；页面将旧 code-editing leaderboard 标注为已被更具挑战性的 polyglot leaderboard 替代。
- [Aider Code Editing Leaderboard](https://aider.chat/docs/leaderboards/edit.html)：早期 133 道 Exercism Python benchmark、完成率与 edit format 指标说明。
- [Aider Benchmark Notes](https://aider.chat/docs/leaderboards/notes.html)：说明“completed correctly”与“correct edit format”的官方口径。
- [Aider Benchmark Methodology](https://aider.chat/docs/benchmarks.html#the-benchmark)：说明初始实现、独立测试、错误输出反馈及第二次修复的流程。

## 4. 测评集是什么

Aider Polyglot 是一组按语言划分的 Exercism 风格编程练习。每个任务会提供：

1. 题目说明与初始代码；
2. 受限的可修改文件；
3. 独立的测试/verifier；
4. 对外 API、边界行为和测试通过要求。

它主要评估 agent 是否能读懂已有代码库、保持接口兼容、实现算法或业务规则，并通过真实语言工具链验证结果。题目不是开放式聊天题，也不是只要求给出思路；必须在容器工作区实际修改文件并通过测试。

## 5. 题目语言分布

| 语言 | 题目数 | 占比 |
| --- | ---: | ---: |
| JavaScript | 49 | 21.8% |
| Java | 47 | 20.9% |
| Go | 39 | 17.3% |
| Python | 34 | 15.1% |
| Rust | 30 | 13.3% |
| C++ | 26 | 11.6% |
| **总计** | **225** | **100%** |

## 6. 题目能力分布

这不是单一“刷算法题”集合。225 题覆盖的能力可以按下面几类理解；同一题可能同时属于多类。

| 能力方向 | 代表任务 | 主要考察点 |
| --- | --- | --- |
| 字符串、文本与格式化 | `pig-latin`、`wordy`、`ocr-numbers`、`markdown`、`grep` | 解析、格式精确性、边界字符、异常输入 |
| 数学、数论与组合搜索 | `alphametics`、`all-your-base`、`change`、`perfect-numbers`、`knapsack` | 回溯、动态规划、进制与数值边界 |
| 数据结构与迭代器 | `binary-search-tree`、`simple-linked-list`、`zipper`、`circular-buffer` | 公共 API、泛型、所有权/生命周期、状态维护 |
| 并发、异步与状态机 | `parallel-letter-frequency`、`promises`、`react`、`bank-account` | 并发安全、回调/事件、可变状态和错误处理 |
| 网格、图与空间搜索 | `word-search`、`connect`、`maze/mazy-mice`、`queen-attack` | 多方向搜索、坐标、遍历与路径逻辑 |
| 业务规则与领域建模 | `ledger`、`book-store`、`poker`、`bowling`、`food-chain` | 规则组合、排序、金额/日期/国际化、可读的领域模型 |
| 协议、编码与序列化 | `rest-api`、`variable-length-quantity`、`sgf-parsing`、`paasio` | 数据格式、接口契约、字节处理与错误分支 |
| 语言特有能力 | Rust `doubly-linked-list`、C++ `clock`、Java `zipper` | 所有权与 unsafe、模板/API、JVM/Gradle 工具链 |

从工程视角看，最容易失分的并不总是算法本身，而是：**没有完全遵守题目既定 API、遗漏输出格式细节、只做局部编译而没运行完整测试、以及语言依赖工具链的网络可用性。**

## 7. 结果如何解读

### 7.1 有效判分结果

| 结果 | 数量 | 含义 |
| --- | ---: | --- |
| `reward=1` | 213 | verifier 完整通过 |
| `reward=0` | 8 | verifier 给出失败判分 |
| 合计 | 221 | 可用于计算 96.38% 通过率 |

8 个 `reward=0` 中有 6 个同时带有 `NonZeroAgentExitCodeError`，因此不能简单地把 8 个全部归为“纯代码错误”。其中已经明确确认的模型实现问题包括：

- `java_bottle-song`：核心实现正确，但输出缺少测试要求的末尾换行，7 个字符串断言失败；
- `rust_doubly-linked-list`：agent 因 provider 网络错误中断，`lib.rs` 中仍保留 `not yet implemented`，未完成实现；
- `java_word-search`：最终获得了 `reward=0`，应以该题 verifier 记录为准；需要单题日志复盘后才应进一步归因。

### 7.2 无 reward 的 4 个异常

这 4 个均为 C++ 题。它们的 verifier 在 CMake 编译测试时失败，脚本未写 `reward.txt`，于是 Harbor 记录为 `RewardFileNotFoundError`。这不是要求模型写 `reward.txt`，而是 verifier 的错误分类不完整。

| 题目 | 直接原因 |
| --- | --- |
| `cpp_clock` | 漏实现测试要求的静态工厂 `clock::at(hour, minute)` |
| `cpp_allergies` | 测试传入字符串 `"eggs"`，实现仅接收枚举 `Allergen` |
| `cpp_binary-search-tree` | 测试期望 `binary_tree<T>`，实现公开类型名不匹配 |
| `cpp_parallel-letter-frequency` | 测试传 `vector<string_view>`，实现接口仅接收 `vector<string>` |

语义上它们属于接口实现错误，而不是 Docker 或网络错误；但由于没有 reward 文件，Harbor 会把它们作为异常处理，并拉低端到端 Mean。

### 7.3 基础设施异常

最终仍有 6 个 `NonZeroAgentExitCodeError`。它们在本次结果中还带有 `reward=0`，应单独保留异常标签，不应在没有查看具体 trial 日志前全部归因给模型。

本次运行中已出现并处理过的基础设施问题包括：

- Docker/Colima 默认地址池耗尽：多次取消和重跑产生大量 Harbor 环境网络；恢复前以 `docker network prune -f` 清理未使用网络；
- Java Gradle Wrapper 下载：Java verifier 容器需要下载 `gradle-8.7-bin.zip`，最初出现连接/读取超时；后续在本地缓存的 Java verifier 中将 Docker `HTTP(S)_PROXY` 转发为 Gradle JVM 的 `systemProp.http/https.proxy*`；
- Provider 网络波动：导致部分 agent 非零退出，因此启用了有限重试和定向恢复。

> [!NOTE]
> Java Gradle 代理补丁仅位于 Git 忽略的本地数据集缓存 `.local/harbor-datasets/aider-polyglot`，不属于 FirstCoder 生产代码，也没有提交到仓库。

## 8. 本次评测的工程改进

1. **二轮 verifier feedback**：对明确测试失败的任务，将 verifier 输出送回同一 session，形成“实现 → 验证 → 修复 → 再验证”的闭环。
2. **错误分层**：区分 `reward=0`、`RewardFileNotFoundError`、网络错误、agent 非零退出和 verifier 超时，避免把基础设施错误直接解释为模型能力。
3. **Java 网络治理**：将 Docker 代理显式传给 Gradle JVM，避免 Wrapper 不读取 `HTTP_PROXY` 时重复下载失败。
4. **可恢复执行**：以 Harbor `job resume` 定向恢复异常题，保留通过题和原始 artifacts，不重跑整套 225 题。
5. **容器资源清理**：恢复前清理未使用 Docker 网络，避免地址池耗尽导致环境在 agent 启动前失败。

## 9. 复现与恢复

### 环境变量

凭据应保存在 Git 忽略的 `.env.harbor`。不要把 API key 写入 README、命令历史或提交。

```sh
set -a
source .env.harbor
set +a
```

### 启动新的本地 Aider Polyglot 运行

```sh
PYTHONPATH="$PWD" .venv/bin/harbor run \
  -p .local/harbor-datasets/aider-polyglot \
  -a benchmark.harbor.firstcoder_agent:FirstCoderHarborAgent \
  -m gpt-5.6-luna \
  -n 4 -k 1 \
  --ak max_tool_rounds=120 \
  --ak reasoning_effort=high \
  --agent-setup-timeout-multiplier 3 \
  --ae "FIRSTCODER_PROVIDER=$FIRSTCODER_PROVIDER" \
  --ae "FIRSTCODER_PROVIDER_NAME=$FIRSTCODER_PROVIDER_NAME" \
  --ae "FIRSTCODER_MODEL=$FIRSTCODER_MODEL" \
  --ae "FIRSTCODER_BASE_URL=$FIRSTCODER_BASE_URL" \
  --ae "FIRSTCODER_API_KEY=$FIRSTCODER_API_KEY" \
  --ae "FIRSTCODER_DISABLE_GLOBAL_SKILLS=$FIRSTCODER_DISABLE_GLOBAL_SKILLS" \
  -o benchmark/runs/harbor/aider-polyglot-<date> \
  -y
```

### 定向恢复常见基础设施异常

先确认没有另一个 `harbor run` / `harbor job resume` 正在运行，再执行：

```sh
docker network prune -f

PYTHONPATH="$PWD" .venv/bin/harbor job resume \
  -p benchmark/runs/harbor/aider-polyglot-feedback-retry-20260726/2026-07-26__12-07-27 \
  -f NonZeroAgentExitCodeError \
  -f NetworkConnectionError \
  -f AgentTimeoutError \
  -f VerifierTimeoutError \
  --plugin benchmark.harbor.aider_feedback_plugin:AiderFeedbackPlugin
```

`RewardFileNotFoundError` 需要先看 verifier 日志；本次四个 C++ 例子是实际 API 兼容性错误，不应与网络错误混为一谈。

## 10. 证据位置

| 证据 | 位置 |
| --- | --- |
| 聚合结果 | [`result.json`](./result.json) |
| 固化运行配置 | [`config.json`](./config.json) |
| 固化任务与重试锁 | [`lock.json`](./lock.json) |
| Harbor 主日志 | [`job.log`](./job.log) |
| 单题 agent / verifier 日志 | `<trial-name>/agent/firstcoder.txt`、`<trial-name>/verifier/test-stdout.txt` |
| 本地数据集 | `../../../../../.local/harbor-datasets/aider-polyglot`（相对仓库根目录） |

---

生成时间：2026-07-27。统计以本目录 `result.json` 的最终状态为准。
