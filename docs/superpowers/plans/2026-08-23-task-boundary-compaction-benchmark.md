# 任务边界压缩基准测试实施计划

> **面向执行型 Agent：** 实施本计划时，必须按任务逐项使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans`。步骤以复选框（`- [ ]`）记录进度。

**目标：** 新增一个仅用于 benchmark、可复现的三臂测试 runner，在不修改 FirstCoder 生产行为、全局配置或正常会话数据的前提下，测量任务边界压缩在受控上下文与真实、有 verifier 的编码任务中的因果效果。

**架构：** benchmark 会为每个 `(case, arm, repetition)` 创建一次性项目副本和 JSONL 数据根目录。测试专用的 `BenchmarkAgentLoop` 继承 `AgentLoop`：分别实现不做边界分类、记录分类但只屏蔽 `TASK_HASH_CHANGED` 压缩、或保持正常循环不变。所有组都继续保留普通的请求前 `AUTO` 压缩。同步 Provider 装饰器记录主请求、隐藏分类请求和 L4 请求各自的 token 用量与延迟。受控 case 将完成的任务 A 对话注入至高水位的固定比例；真实 case 将历史上真实的 FirstCoder 改动还原为任务 B，并使用该改动引入的测试文件验证，且此前注入同一份隔离的旧任务对话。

**技术栈：** Python 3.12、pytest、已有的 FirstCoder session/context API、JSON/JSONL、标准库 `subprocess` 与本地 Git 历史。

---

## 不变量与实验契约

- 这是仅位于 `benchmark/` 的功能。不得增加运行时 feature flag、CLI `--context-window` 参数或生产配置字段。
- 不得写入 `~/.config/firstcoder/config.toml`、任何项目的 `firstcoder.toml`，也不得使用普通 `.firstcoder/` 数据根。每次 benchmark 都必须在运行目录下获得显式创建的全新 `data_root`。
- `project/` 与 `data_root` 仅可在 trial 执行期间存在。提取指标后必须删除它们；最终产物只能保留白名单化的 `events.json`、token/耗时、verifier 退出码和 stdout/stderr 哈希。不得保留 JSONL、prompt、模型正文、工具参数、工具结果或凭证，也不得在 `result.json` 指向已删除的私有会话文件。
- benchmark runner 仅把 `context_window` 直接传给测试专用的 `AgentLoop`。试运行可以使用 `32_768`；这不会改变用户界面应用配置的窗口。报告必须将它标为 `simulated_budget_window`，不得称作生产窗口结果。
- 每个 arm 必须使用相同的 provider/model、温度、推理强度、工具限制、项目快照、用户消息和上下文窗口值。运行必须使用非流式模式，以稳定收集 provider 的 `usage`。
- 三个 arm 的含义固定如下：

  | Arm | 隐藏分类器 | `TASK_HASH_CHANGED` 压缩 | 普通 `AUTO` |
  | --- | --- | --- | --- |
  | `auto_only` | 关闭 | 关闭 | 开启 |
  | `classifier_only` | 开启 | 屏蔽 | 开启 |
  | `full` | 开启 | 开启 | 开启 |

- 受控正例在 B 的续接消息之前必须低于 AUTO 高水位。对 32,768 窗口及 4,096 输出预留，目标为约 24,329 token 高水位的 75–88%。如果预期的边界事件之前发生 `auto` 压缩，应将该 trial 标记为 `confounded_auto`，并从主因果比较中排除。
- 每个正例都遵循三轮链路：`旧任务 A -> 分析新任务 B（new）-> 继续任务 B（same）`。benchmark 的隔离执行使用 Bypass 权限，因此当前生产 `task_boundary` 的稳定阈值为 1：预期的 `task_hash_changed` 在 B 的 `new` 消息（第二段链路）出现；后续 `same` 用于验证压缩后的同任务续接。负例是任务 A 的继续，绝不能触发该事件。
- 将 `C - B` 报告为已控制分类器成本后的压缩效果，`B - A` 为分类器额外成本，`C - A` 为整套功能的净效果。绝不能仅凭 token 节省声称质量有收益。

## 文件清单

- 新建：`benchmark/task_boundary_compaction/__init__.py`
- 新建：`benchmark/task_boundary_compaction/models.py`
- 新建：`benchmark/task_boundary_compaction/provider_observer.py`
- 新建：`benchmark/task_boundary_compaction/loop.py`
- 新建：`benchmark/task_boundary_compaction/seed.py`
- 新建：`benchmark/task_boundary_compaction/cases.py`
- 新建：`benchmark/task_boundary_compaction/runner.py`
- 新建：`benchmark/task_boundary_compaction/report.py`
- 新建：`benchmark/task_boundary_compaction/README.md`
- 新建：`benchmark/task_boundary_compaction/fixtures/controlled_cases.json`
- 新建：`benchmark/task_boundary_compaction/fixtures/historical_cases.json`
- 新建：`tests/test_task_boundary_compaction_models.py`
- 新建：`tests/test_task_boundary_compaction_loop.py`
- 新建：`tests/test_task_boundary_compaction_seed.py`
- 新建：`tests/test_task_boundary_compaction_provider_observer.py`
- 新建：`tests/test_task_boundary_compaction_runner.py`
- 新建：`tests/test_task_boundary_compaction_report.py`

不修改 `firstcoder/` 下的任何文件。以下已有行为测试仍作为运行时回归套件：`tests/test_context_task_boundary.py`、`tests/test_task_boundary_tool.py`、`tests/test_context_triggers.py`、`tests/test_agent_context_loop.py`、`tests/test_agent_e2e.py`。

### 任务 1：定义 benchmark 数据与结果 schema

**文件：**
- 新建：`benchmark/task_boundary_compaction/__init__.py`
- 新建：`benchmark/task_boundary_compaction/models.py`
- 测试：`tests/test_task_boundary_compaction_models.py`

- [ ] **步骤 1：先编写会失败的 schema 测试**

```python
from benchmark.task_boundary_compaction.models import Arm, BenchmarkCase, TurnSpec


def test_case_requires_new_then_same_for_a_positive_boundary_case() -> None:
    case = BenchmarkCase(
        case_id="controlled-parser",
        kind="controlled",
        turns=(
            TurnSpec("任务B：分析 parser bug", "new"),
            TurnSpec("继续任务B：修复并验证", "same"),
        ),
        verify_command=(".venv/bin/python", "-m", "pytest", "tests/test_parser.py", "-q"),
        expected_boundary=True,
    )

    assert case.turns[-1].expected_decision == "same"
    assert Arm.FULL.value == "full"
```

- [ ] **步骤 2：运行聚焦测试并确认它失败**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_models.py -q`
预期：收集测试失败，因为 `benchmark.task_boundary_compaction` 尚不存在。

- [ ] **步骤 3：实现不可变 schema 与校验**

将 `Arm` 定义为只有 `AUTO_ONLY`、`CLASSIFIER_ONLY`、`FULL` 三个成员的 `StrEnum`。将 `TurnSpec`、`BenchmarkCase`、`ProviderCallMetric`、`CompactionMetric`、`TrialResult` 和 `ComparisonRow` 定义为 `frozen=True, slots=True` 的 dataclass。`BenchmarkCase` 必须拒绝空 ID、少于两轮 B 消息、未知的预期决策、空 verifier 命令，以及最后一个预期决策不是 `same` 的正例。`TrialResult.to_dict()` 必须包含 arm、case ID、model、预算窗口、状态、verifier 结果、token、耗时、边界事件、压缩事件与产物路径；绝不能序列化 API key 或原始 provider 对象。

- [ ] **步骤 4：加入往返序列化与拒绝路径测试**

测试一条完整结果的 `to_dict()`/`from_dict()` 往返、非法 arm、`new -> new` 的正例序列，以及被标成预期边界的负例。断言非法数据抛出包含对应字段名的 `ValueError`。

- [ ] **步骤 5：运行聚焦测试**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_models.py -q`
预期：全部通过。

- [ ] **步骤 6：提交**

```bash
git add benchmark/task_boundary_compaction/__init__.py \
  benchmark/task_boundary_compaction/models.py \
  tests/test_task_boundary_compaction_models.py
git commit -m "Add task-boundary benchmark schemas"
```

### 任务 2：新增仅用于 benchmark 的三臂循环

**文件：**
- 新建：`benchmark/task_boundary_compaction/loop.py`
- 测试：`tests/test_task_boundary_compaction_loop.py`

- [ ] **步骤 1：先编写会失败的 arm 行为测试**

复用 `tests/test_agent_context_loop.py` 中的 `FakeProvider` 和 `FakeContextManager` 模式。将已注入的会话依次经过 B 的 `new` 与 B 的 `same` 两轮。断言 `auto_only` 不产生后续 `task_boundary_observed` 事件；`classifier_only` 记录已确认的变化但不会调用 `TASK_HASH_CHANGED` 的 manager；`full` 记录该变化并恰好调用一次 `ContextWindowTrigger.TASK_HASH_CHANGED`。

- [ ] **步骤 2：运行聚焦测试并确认它失败**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_loop.py -q`
预期：收集测试失败，因为 `BenchmarkAgentLoop` 尚不存在。

- [ ] **步骤 3：实现子类，但不修改 `firstcoder/`**

```python
class BenchmarkAgentLoop(AgentLoop):
    def __init__(self, *args, arm: Arm, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.arm = arm

    def _classify_task_boundary(self, basis_message_id: str) -> None:
        if self.arm is not Arm.AUTO_ONLY:
            super()._classify_task_boundary(basis_message_id)

    def _compact_if_needed(self, *, trigger, runtime_instruction=None):
        if self.arm is Arm.CLASSIFIER_ONLY and trigger is ContextWindowTrigger.TASK_HASH_CHANGED:
            return None
        return super()._compact_if_needed(
            trigger=trigger,
            runtime_instruction=runtime_instruction,
        )
```

在全部 arm 中，把 `AUTO`、`PROMPT_TOO_LONG` 和手动调用都委托给父类。`TaskBoundaryClassifier` 保存的回调必须解析到这个 override；因此正常构造该子类，不能替换分类器内部逻辑，也不能 monkeypatch 已绑定的方法。

- [ ] **步骤 4：测试正常 AUTO 保留与负例**

在每个 arm 都加入一个超过 AUTO 高水位的预算测试，断言 `AUTO` 仍会到达 context manager。再增加一个同任务负例序列，断言 full arm 不存在 `task_hash_changed` 压缩事件。

- [ ] **步骤 5：运行聚焦与关联回归测试**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_loop.py tests/test_agent_context_loop.py -q`
预期：全部通过。

- [ ] **步骤 6：提交**

```bash
git add benchmark/task_boundary_compaction/loop.py \
  tests/test_task_boundary_compaction_loop.py
git commit -m "Add benchmark-only task-boundary arms"
```

### 任务 3：记录所有 provider 成本，包括隐藏请求

**文件：**
- 新建：`benchmark/task_boundary_compaction/provider_observer.py`
- 测试：`tests/test_task_boundary_compaction_provider_observer.py`

- [ ] **步骤 1：先编写会失败的 observer 测试**

构造三个 `ChatRequest`：一个带工具的普通请求、一个由 `TaskBoundaryClassifier.build_request()` 构造的请求、一个由 `ProviderLlmCompactSummarizer` 发出的精确两条消息的 L4 请求。返回带有 `TokenUsage(input_tokens=10, output_tokens=4, total_tokens=14)` 的 `ChatResponse`。断言指标将调用分别归为 `main`、`classifier`、`l4`，缺失 usage 时保留 `None`，且耗时非负。

- [ ] **步骤 2：运行聚焦测试并确认它失败**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_provider_observer.py -q`
预期：收集测试失败，因为 `RecordingProvider` 尚不存在。

- [ ] **步骤 3：实现同步委托型 provider**

实现 `RecordingProvider`，将 `name`、`model`、`capabilities` 与 `complete()` 委托给被包装的非流式 provider。使用 `time.perf_counter()` 计时 `complete()`，然后追加一条 `ProviderCallMetric`。分类器请求须同时满足三项稳定特征：`tools == []`、`tool_choice == "none"`、`max_tokens == CLASSIFICATION_MAX_TOKENS`；L4 请求须满足第一条 system 消息以 `你是 FirstCoder 的上下文压缩器。` 精确前缀开头、`tools == []`、`tool_choice == "none"`、`max_tokens == 1200`。其余请求一律归为 `main`。不得记录 prompt、工具参数、响应内容、headers 或 `raw` 对象。

- [ ] **步骤 4：增加安全的聚合函数**

暴露 `usage_totals(metrics)`，返回 `main`、`classifier`、`l4` 与 `all` 各自的 input/output/total 计数。当 provider 缺失任一 usage 字段时，该小计必须保留 `None`；在 trial 结果写入 `usage_complete=false`，不得把缺失值当作零。

- [ ] **步骤 5：运行聚焦测试**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_provider_observer.py -q`
预期：全部通过。

- [ ] **步骤 6：提交**

```bash
git add benchmark/task_boundary_compaction/provider_observer.py \
  tests/test_task_boundary_compaction_provider_observer.py
git commit -m "Record task-boundary benchmark provider costs"
```

### 任务 4：注入确定性的已完成任务 A 上下文

**文件：**
- 新建：`benchmark/task_boundary_compaction/seed.py`
- 新建：`benchmark/task_boundary_compaction/fixtures/controlled_cases.json`
- 测试：`tests/test_task_boundary_compaction_seed.py`

- [ ] **步骤 1：先编写会失败的 seed 测试**

创建一个带明确初始 `active_task_hash` 的会话，调用 `seed_old_task_context(case_id="controlled-parser", target_input_tokens=21_000)`，随后重新构建 view 与 runtime state。断言每个注入片段都带旧 hash，最终估算预算低于高水位且高于高水位的 75%，并且没有任何注入事件是 checkpoint 或 compaction replacement。

- [ ] **步骤 2：运行聚焦测试并确认它失败**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_seed.py -q`
预期：收集测试失败，因为 seed helper 尚不存在。

- [ ] **步骤 3：通过公开 session API 实现确定性注入**

使用 `AgentSession.append_user_message()` 和 `append_assistant_response()`，写入由 `case_id` 与固定段落序列生成的确定性文本块。注入前设置 `session.runtime_state.active_task_hash`，使 `_current_context_metadata()` 将每个片段标记为任务 A。持续增加固定段落数量，直到所给 `estimate_budget(view).input_tokens` 处于 `[floor(high_watermark * 0.75), high_watermark - 1]`；如果固定的 system/tools 部分已无法满足区间，则抛出 `ValueError`。受控 seed 只覆盖 L1 对旧任务文本的裁剪；不得伪造 tool-call/tool-result 对，也不得伪造 `provider_projection_consumed` 事件来使 L2/L3 符合条件。

- [ ] **步骤 4：定义受版本控制的受控 case**

在 `controlled_cases.json` 写入十个正例与三个负例。每个正例有彼此独立的 A/B 标签和明确 verifier 命令；每个负例使用一条续接 B 消息。使用以下固定 ID：

```text
正例：parser-normalization, cache-key, retry-backoff, csv-validation,
      pagination-cursor, config-merge, date-format, path-resolution,
      response-serialization, permission-selection
负例：parser-follow-up, cache-follow-up, retry-follow-up
```

每个 fixture 的 B 仓库都复制到该 case 的临时工作目录，并含有一个小型失败 Python 测试。任务 A 的 seed 绝不写入该仓库，因此全部 arm 的 B 都从相同的工作树开始。

- [ ] **步骤 5：运行聚焦测试**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_seed.py -q`
预期：全部通过。

- [ ] **步骤 6：提交**

```bash
git add benchmark/task_boundary_compaction/seed.py \
  benchmark/task_boundary_compaction/fixtures/controlled_cases.json \
  tests/test_task_boundary_compaction_seed.py
git commit -m "Add controlled task-boundary benchmark contexts"
```

### 任务 5：加入有 verifier 支撑的历史真实任务 case

**文件：**
- 新建：`benchmark/task_boundary_compaction/cases.py`
- 新建：`benchmark/task_boundary_compaction/fixtures/historical_cases.json`
- 测试：`tests/test_task_boundary_compaction_runner.py`

- [ ] **步骤 1：先编写会失败的 manifest 测试**

使用含一个历史 case 的临时 manifest。断言 `load_historical_cases()` 接受完整 base 与 target commit、要求至少一个聚焦测试文件、拒绝 base 不是 target 第一父提交的情况，并为全部 arm 生成相同的 B prompt 与 verifier。

- [ ] **步骤 2：运行聚焦测试并确认它失败**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_runner.py -q`
预期：收集测试失败，因为尚未实现历史 case 加载。

- [ ] **步骤 3：登记四个真实 B 任务改动及其来源**

在 `historical_cases.json` 中写入以下真实 FirstCoder 改动及准确的父快照：

| Case ID | Target commit | Base commit | 聚焦 verifier 文件 |
| --- | --- | --- | --- |
| `compact-before-main` | `b71f1a086d4d1d25b1b155c2d91063d031db99d9` | `de5ce515fbe1eb3b5764f794a2e85c0272cd5016` | `tests/test_agent_context_loop.py`, `tests/test_context_builder_new.py` |
| `dynamic-context-budget` | `de5ce515fbe1eb3b5764f794a2e85c0272cd5016` | `9ee8e8cf96b00e33931bd1cb2289cda698fe92b0` | `tests/test_context_triggers.py`, `tests/test_context_window_manager.py` |
| `protect-unconsumed-context` | `046bc1aedc7bc3b9dce7b158138efedff519bf49` | `94528a16ce1d2e34f4b71b864c82679504984f37` | `tests/test_context_compaction_pipeline.py`, `tests/test_context_store.py` |
| `validate-l4-candidate` | `9ee8e8cf96b00e33931bd1cb2289cda698fe92b0` | `046bc1aedc7bc3b9dce7b158138efedff519bf49` | `tests/test_context_llm_compact.py`, `tests/test_context_window_manager.py` |

每条 B prompt 使用该改动的精确 commit subject，并要求 agent 在不编辑测试的前提下让新引入的聚焦测试通过。把原始测试 diff 拷入 base 快照，因此任务 B 是使用原始 verifier 的真实历史编码改动，而不是人工编造的 bug。

任务 A 仍是任务 4 中只读的已提交 seed。它必须与 B 无关，且绝不修改 B 工作树。

- [ ] **步骤 4：实现隔离的任务物化**

每次运行创建全新的 B 目录，执行 `git archive "$base_commit" | tar -x`，再用 `git show "$target_commit:$test_path"` 仅物化列出的聚焦测试文件。不得从其它 arm 复制任何状态；只将任务 A 的会话 seed 注入该 benchmark 的 `data_root`。在同一个 B 工作目录内运行 B 的 `new` 和 `same` 两轮，并在续接轮后执行列出的聚焦 verifier。保存 verifier exit code、截断后的 stdout/stderr 哈希和产物路径；不得把 verifier 输出写入 provider 上下文。

- [ ] **步骤 5：增加不调用 provider 的历史任务 smoke test**

mock `subprocess.run`、`git archive` 和 `git show`，随后断言同一个历史 case 的三臂 trial 使用相同 verifier 命令、保存到互相独立的根目录，且从不执行任务 A 的变更命令。

- [ ] **步骤 6：运行聚焦测试**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_runner.py -q`
预期：全部通过，且不调用 provider。

- [ ] **步骤 7：提交**

```bash
git add benchmark/task_boundary_compaction/cases.py \
  benchmark/task_boundary_compaction/fixtures/historical_cases.json \
  tests/test_task_boundary_compaction_runner.py
git commit -m "Add historical task-boundary benchmark manifests"
```

### 任务 6：实现 runner、事件提取与报告

**文件：**
- 新建：`benchmark/task_boundary_compaction/runner.py`
- 新建：`benchmark/task_boundary_compaction/report.py`
- 新建：`benchmark/task_boundary_compaction/README.md`
- 测试：`tests/test_task_boundary_compaction_report.py`

- [ ] **步骤 1：先编写端到端 fake-provider 失败测试**

用一个产生 `new` 后产生 `same` 的 fake provider，让一个受控正例在全部三臂运行。断言 runner 在确定性的 `RunConfig.output_root / RunConfig.run_id / case.case_id / arm.value` 路径下恰好为每个 arm 写入一个 `result.json`；full arm 有一个 `task_hash_changed` 事件；classifier-only arm 没有该事件；每份结果均报告隔离的 data root。

- [ ] **步骤 2：运行聚焦测试并确认它失败**

运行：`.venv/bin/python -m pytest tests/test_task_boundary_compaction_report.py -q`
预期：收集测试失败，因为 `run_case()` 和 `build_report()` 尚不存在。

- [ ] **步骤 3：实现 runner**

公开 `run_case(case, *, arm, config) -> TrialResult` 与 `run_matrix(cases, *, arms, repetitions, config) -> list[TrialResult]`。每个 trial 创建一次性 data root 和工作副本根目录。使用既有模型目录创建真实 provider，用 `RecordingProvider` 包装它，将 L4 summarizer 的 provider 也指向同一 wrapper；以 `BenchmarkAgentLoop` 和 `context_window=config.context_window` 运行；只对该一次性会话设定 benchmark 权限；注入任务 A；发送 B 的 `new` 与 `same` 消息；执行 case verifier；从该 trial 的 JSONL 中提取 `task_boundary_observed`、`compaction_completed`、`llm_compaction_completed` 和 `agent_turn_telemetry`。`run_matrix()` 必须根据 `config.random_seed` 确定性打乱 arm 顺序，且 arm 之间绝不复用会话。

- [ ] **步骤 4：实现报告与因果防护**

`build_report()` 必须生成 `summary.json` 与 `summary.md`，包含每 arm 的中位数、通过数、token 总数、classifier/L4 小计、耗时百分位、trigger 数量，以及配对差值 `full_minus_classifier_only`、`classifier_only_minus_auto_only`、`full_minus_auto_only`。在相应聚合中标记并排除 `confounded_auto`、缺失 usage、非法边界、verifier-error 的 trial，但在原始结果中保留它们。若任一正例 full trial 的 `task_hash_changed` 事件为零，或任一负例 full trial 有该事件，则该次运行必须失败。

- [ ] **步骤 5：记录精确命令与安全边界**

在 `README.md` 记录以下命令：

```bash
.venv/bin/python -m benchmark.task_boundary_compaction.runner \
  --suite controlled \
  --model Yuren/gpt-5.6-terra \
  --context-window 32768 \
  --repetitions 1 \
  --output benchmark/runs/task-boundary-compaction/pilot-32k

.venv/bin/python -m benchmark.task_boundary_compaction.runner \
  --suite historical \
  --model Yuren/gpt-5.6-terra \
  --context-window 200000 \
  --repetitions 1 \
  --output benchmark/runs/task-boundary-compaction/real-200k

.venv/bin/python -m benchmark.task_boundary_compaction.report \
  --input benchmark/runs/task-boundary-compaction/pilot-32k \
  --output benchmark/runs/task-boundary-compaction/pilot-32k/summary
```

必须明确说明：第一条命令只改变 benchmark loop 的内存预算，不改变 FirstCoder 正常配置。要求使用被忽略的 `.env.task-boundary-benchmark`，其中只能出现由 shell 已提供的 provider 环境变量名或值；绝不能把凭证写进结果文件。

- [ ] **步骤 6：运行聚焦测试与回归测试**

运行：

```bash
.venv/bin/python -m pytest \
  tests/test_task_boundary_compaction_models.py \
  tests/test_task_boundary_compaction_loop.py \
  tests/test_task_boundary_compaction_seed.py \
  tests/test_task_boundary_compaction_provider_observer.py \
  tests/test_task_boundary_compaction_runner.py \
  tests/test_task_boundary_compaction_report.py \
  tests/test_context_task_boundary.py \
  tests/test_task_boundary_tool.py \
  tests/test_context_triggers.py \
  tests/test_agent_context_loop.py \
  tests/test_agent_e2e.py -q
```

预期：全部通过。任何付费 provider 运行前，先运行一次不调用 provider 的受控 fake-provider matrix，并检查 `summary.json`。

- [ ] **步骤 7：提交**

```bash
git add benchmark/task_boundary_compaction \
  tests/test_task_boundary_compaction_report.py
git commit -m "Add task-boundary compaction benchmark runner"
```

### 任务 7：执行并解读两层测试集

**文件：**
- 仅在运行时新建：`RunConfig.output_root / RunConfig.run_id` 选定的目录
- 读取：该运行目录中的 `summary.json`
- 读取：该运行目录中的 `summary.md`

- [ ] **步骤 1：建立受控试运行**

在 `32_768` 下，将十个正例和三个负例分别在每个 arm 运行一次，共 39 段对话。不得称其具有统计结论性；它用于验证事件链路、AUTO 混淆是否不存在、指标是否完整，以及效果方向。

- [ ] **步骤 2：聚合前阅读原始失败结果**

对每个未通过的结果，只检查其 JSONL 事件类型、verifier exit code 和存储的输出哈希。将其归类为非法边界、AUTO 混淆、provider 失败、verifier 失败或 usage 不完整；没有这些证据，不得重新标为压缩回归。

- [ ] **步骤 3：运行真实任务试运行**

在实际配置的 200,000 窗口下，将四个已提交的历史 B 任务分别在每个 arm 运行一次，共 12 段真实且有 verifier 的对话。任务 A 保持只读并提前注入，保证全部 arm 的 B 重建任务状态一致。

- [ ] **步骤 4：只在试运行有效时扩展**

只有当每个 full 正例都观察到预期边界、没有负例误压缩、B 的 verifier 质量未退化、并且至少 90% provider usage 字段存在时，才以乱序 arm 顺序将两套测试各重跑三次。否则先修复 benchmark harness 或 fixture，再得出产品结论。

- [ ] **步骤 5：应用预先声明的决策规则**

仅当 full 相对 `auto_only` 没有实质 verifier 通过率退化，且在真实长上下文 case 中 full 的全 provider token 总量中位数低于 `classifier_only` 时，才称该功能有收益。即便总 token 改善，也要单独报告分类器成本。若 32K 与 200K 的结果不同，必须同时报告，不能把模拟窗口结果外推为生产结果。
