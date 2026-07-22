# FirstCoder 上下文控制面重构设计

日期：2026-07-22

## 背景

FirstCoder 已经具备 append-only JSONL、`SessionView`、provider 投影、L1-L4 压缩、archive/retrieve、task boundary 和恢复重放。现有问题不在于这些能力完全缺失，而在于预算、触发、可压缩性、checkpoint 提交和 replay 状态使用了互相矛盾的判断口径。

当前所有模型共享固定的 32K 自动阈值、24K 目标和 4,096 token 输出预留；固定 system prefix 与工具 schema 本身可能已接近 24K。L1-L3 用完整账本估算，AgentLoop 用 provider 请求估算，`/context` 又只看 checkpoint summary 与 tail。工具结果在下一次模型请求前就会触发压缩，未知成功工具默认被视为 derived，导致 MCP 源码和 `load_skill` 正文可能在模型第一次完整看到前被有损替换。L4 则先写 checkpoint，再验证压缩结果；失败状态不能可靠重放，真实会话已经出现数百次 checkpoint。

本次重构保留已有事实存储和投影边界，替换上下文控制面，使“窗口多大、何时压缩、什么能压、压缩是否成功”只存在一套可解释、可测试、可恢复的答案。

## 目标

1. 为每个模型显式描述 context window 和默认输出上限，并据此计算有效输入预算。
2. 把固定请求开销、可压缩历史和输出预留分开计量。
3. 自动压缩只在确实即将发生 provider 请求时执行，不在最终回复后做无用途 checkpoint。
4. 可装入单次请求的工具结果至少完整进入一次成功的 provider 请求后，才允许进入有损 L2/L3 或 L4 覆盖范围；先天超窗结果必须从产生时就使用可恢复的大对象协议。
5. 未知工具和 MCP 工具默认保守保留；只有显式能力声明允许把结果分类为 derived/source/ephemeral。
6. task boundary 只清理已确认的旧任务内容，不再放大成多次通用 AUTO compact。
7. L4 checkpoint 在内存中生成并验证，只有满足验收条件后才原子提交。
8. 成功、失败、熔断和 checkpoint 状态能从事件日志精确重放。
9. 工具注册与本轮 schema 暴露分离，降低大量 MCP schema 的固定窗口占用。
10. 使用 breaking schema change，不为旧会话增加兼容分支。

## 非目标

- 不删除或破坏性改写 JSONL 中的原始消息事实。
- 不重写 `ContextBuilder` 的 provider 消息职责。
- 不替换内容寻址 archive，也不改变 `retrieve_archive` 的 session 隔离。
- 不引入向量数据库、跨会话长期记忆、embedding 检索或外部摘要服务。
- 不尝试自动发现任意 provider 的真实窗口；未知模型必须配置窗口或使用明确的保守默认值。
- 不在本次重构中改变 task boundary 分类模型的语义分类算法。
- 不让模型自行声明某个结果已经被消费或可以丢弃。
- 不创建旧事件到新事件的运行时兼容层；旧会话由 schema gate 明确拒绝。

## 核心不变量

重构后必须始终满足：

1. 原始会话事实和 archive 原文 append-only。
2. provider 投影中的 assistant tool call 与 tool result 始终成对且顺序合法。
3. 未被任何成功 provider 请求完整携带过的普通结果不可被有损压缩或 checkpoint 覆盖；先天超窗结果的完整 manifest 必须先被成功消费。
4. 固定前缀自身超出低水位时，不得通过反复压缩历史制造 checkpoint 风暴。
5. AUTO 压缩成功的定义是：候选投影低于当前请求的低水位，而不是“摘要模型返回了文本”。
6. 一次 provider 请求准备过程最多提交一个 checkpoint。
7. task boundary 不得压缩当前新任务的未消费工作集。
8. resume 后的预算、latest checkpoint、失败计数和任务候选状态与退出前一致。
9. `/context` 展示的总预算与真正 provider 请求使用同一估算对象。
10. 最新 checkpoint 在尚未成功投影给模型时，AUTO 不得用另一个 checkpoint 覆盖它。

## 总体架构

```text
ModelContextPolicy
  -> ProviderRequestEnvelope
  -> ContextBudgetSnapshot
       fixed_tokens
       protected_history_tokens
       compactable_history_tokens
       output_reserve_tokens
       effective_window_tokens
       high_watermark_tokens
       low_watermark_tokens
  -> CompactionScheduler
       pre-request only
       once per request preparation
       growth and hysteresis gates
  -> DeterministicCompactionPlanner
       task trim
       lifecycle-safe L2/L3
       consumed-result guard
  -> CheckpointCandidate
       generate in memory
       project in memory
       validate budget and tool sequence
  -> CompactionCommitted event
  -> ContextBuilder
  -> provider request
  -> successful response
  -> mark projected parts consumed
```

`ContextWindowManager` 不再同时承担估算、触发、程序化压缩、L4 提交、fallback 和熔断的全部职责。它保留为编排入口，但各项判断由结构化对象负责，避免再次出现三套 token 口径。

## 模型窗口策略

每个模型配置新增 `context` 段，至少包含：

```text
window_tokens
default_max_output_tokens
auto_compact_ratio
usable_window_ratio
minimum_hysteresis_tokens
minimum_compaction_gain_tokens
minimum_retry_growth_tokens
auto_failure_threshold
auto_circuit_cooldown_seconds
```

语义固定为：

- `window_tokens`：provider 声明的总上下文窗口。
- `default_max_output_tokens`：主请求未显式配置 `max_tokens` 时使用的输出预留。
- `usable_window_ratio`：为 tokenizer 误差、provider envelope 和隐藏开销保留安全边际，默认 0.95。
- `auto_compact_ratio`：在有效窗口中的高水位比例，默认 0.90。
- `minimum_hysteresis_tokens`：高、低水位之间的最小绝对间隔，默认 4,096。
- `minimum_compaction_gain_tokens`：候选 checkpoint 相对当前有效投影必须节省的最小 token，默认 1,024。
- `minimum_retry_growth_tokens`：一次不可提交的 L4 之后，允许 AUTO 再试前必须新增的可覆盖 token，默认动态值 `max(2,048, floor(input_capacity * 0.03))`。
- `auto_failure_threshold`：连续多少次最终 L4 故障后打开 AUTO 熔断，默认 3。
- `auto_circuit_cooldown_seconds`：AUTO 熔断持续时间，默认 300 秒。

计算规则：

```text
effective_window = floor(window_tokens * usable_window_ratio)
output_reserve = request.max_tokens or default_max_output_tokens
input_capacity = effective_window - output_reserve
high_watermark = floor(input_capacity * auto_compact_ratio)
```

低水位不能再写死为 24K。它应从当前请求动态得出，并至少与高水位保持可配置的滞回间隔。初始规则固定为：

```text
low_watermark = min(
    floor(high_watermark * 0.80),
    high_watermark - minimum_hysteresis_tokens
)
```

所有默认值都可以被模型配置显式覆盖。所有比例与整数必须在配置解析时校验，禁止出现负输入容量、低水位不低于高水位、输出预留大于有效窗口，或五个整数参数小于 1。`minimum_hysteresis_tokens` 必须小于该模型算出的高水位；小窗口模型必须在 catalog 中显式配置更小值，不能静默 clamp 成另一套策略。

未知模型不从名称猜窗口。若 catalog 没有配置，使用集中定义的保守默认策略：`window_tokens=32,768`、`default_max_output_tokens=4,096`，并在 `/model` 与 `/context` 中显示为 `assumed`；不得悄悄回退到散落的常量。这个 fallback 只服务未知模型，catalog 中的 128K、200K 等模型必须使用自己的窗口。

## 统一预算快照

引入不可变的 `ContextBudgetSnapshot`，一次请求准备只计算一次，所有决策和可观测性共享该对象。它至少记录：

```text
model_ref
window_tokens
effective_window_tokens
fixed_tokens
history_tokens
protected_history_tokens
compactable_history_tokens
input_tokens
output_reserve_tokens
total_reserved_tokens
high_watermark_tokens
low_watermark_tokens
over_high_watermark
fixed_over_low_watermark
protected_over_low_watermark
estimator_name
estimate_confidence
```

分类规则：

- `fixed_tokens`：system prefix、当前真正暴露的工具 schema、不可压缩 runtime instruction 和 provider envelope。
- `history_tokens`：checkpoint summary 与有效 tail 的全部模型可见历史。
- `protected_history_tokens`：当前不可由 L2/L3/L4 有损减少的历史，例如未消费工具结果、受保护的当前任务和必须保留的 tool transaction。
- `compactable_history_tokens`：当前 policy 允许成为候选覆盖对象的历史；`history_tokens = protected_history_tokens + compactable_history_tokens`。
- `output_reserve_tokens`：本次主请求实际使用的 `max_tokens` 或模型默认值。
- `input_tokens = fixed_tokens + history_tokens`。
- `total_reserved_tokens = input_tokens + output_reserve_tokens`。

AUTO 是否触发只看 `input_tokens` 与高水位；压缩目标只允许作用于 `compactable_history_tokens`。`fixed_tokens > low_watermark` 时返回 `fixed_over_budget`；`fixed_tokens + protected_history_tokens > low_watermark` 时返回 `protected_over_budget`。两者都不进入 L4，因为即使把所有可压缩历史降为零也不可能通过验收；前者交给工具暴露策略或用户可见错误，后者等待受保护内容成功消费或任务状态推进。预算快照必须先按当前 lifecycle 计算这一下界，不能让 L4 通过试错发现“无解”。

文本估算保留 provider-neutral fallback，但增加 estimator 接口。已知 provider 可以提供 tokenizer-aware estimator；未知 provider 使用保守估算并标记较低 confidence。图片和 rich content 必须进入估算，不能再只统计 `ChatMessage.content`。provider 返回的真实 input usage 只记录估算偏差用于诊断，不在本阶段做自适应安全系数，避免同一事件日志因隐藏运行时校准得到不同决策。

## 工具注册与本轮暴露

工具 registry 继续保存所有可调用工具，但 provider request 不再默认暴露全部 registry definition。

新增 `ToolExposurePlan`：

- 核心本地工具保持稳定暴露。
- session 必需控制工具按运行状态暴露，例如 `retrieve_archive`、task plan 和后台控制工具。
- MCP 初始只暴露一个稳定的目录搜索控制工具和各 server 的短摘要，不依赖另一模型或语义分类器猜测“任务相关”。
- 用户显式写出完整 server/tool 名称时可以精确激活；否则模型先调用目录搜索工具，结果按稳定相关度与名称排序返回至多 8 个候选，下一次 provider 请求只激活这批候选的具体 schema。
- session 保存显式激活集合；任务切换后清除未被 pending transaction 使用的临时激活项。用户配置的常驻项不受此清理影响。
- 同一轮工具调用后的 follow-up 必须继续暴露完成当前 transaction 所需的工具。
- 权限系统仍对最终工具调用做程序化校验，schema 未暴露不能成为绕过权限的方式。

预算快照只统计 `ToolExposurePlan` 中本轮真正发送的 schema。目录搜索失败时保留控制工具并返回可见错误，不回退为暴露全部 schema；单个 MCP server 不可因工具数量过大自动把全部 schema 重新注入。

这项调整与压缩控制面同属一个重构，因为固定 schema 是否进入窗口直接决定压缩是否有解。但不在本次设计中重写 MCP 连接、调用协议或权限适配器。

迁移必须分成两个可独立验证的阶段。第一阶段只引入 `ToolExposurePlan`，输出仍与当前“全部暴露”完全等价，用来证明 provider、权限和工具循环没有行为变化；第二阶段才启用 MCP 目录发现与按需暴露。checkpoint 主链不得依赖第二阶段完成，关闭按需暴露时仍能使用统一预算与 pre-request scheduler，只是可能明确得到 `fixed_over_budget`。

## Provider 请求准备与触发时机

引入单一的 `prepare_provider_request()` 边界：

1. 收集待追加的后台通知和 runtime instruction。
2. 生成 `ToolExposurePlan`。
3. 构建候选 provider envelope 并计算预算快照。
4. scheduler 判断是否需要 compact。
5. 最多执行一次确定性 compact、一次默认 L4 候选，以及仅对指定验收失败允许的一次 stronger 候选。
6. 重新构建最终 envelope 与预算快照。
7. 预留 provider call 并发送请求。
8. 请求成功返回后记录本次投影消费状态。

删除以下触发方式：

- 最终 assistant 回复落盘后的 AUTO compact。
- 每个工具结果落盘后立即独立 AUTO compact。
- `AUTO -> TASK_HASH_CHANGED -> AUTO` 链。

工具循环仍会在下一次模型请求前自然进入 `prepare_provider_request()`；因此大结果仍会及时整理，但不会早于模型第一次消费，也不会在没有下一次请求时产生 checkpoint。

`PROMPT_TOO_LONG` 保留为 provider 明确拒绝后的阻塞式恢复入口。它复用同一准备边界，但使用 blocking policy，并且最多重试一次主请求。它不允许绕过最终预算验收。

## 工具结果消费状态

新增可重放事件，记录一次成功 provider 请求中新进入消费状态的持久输入 part，而不是每次重复记录整份投影，也不是记录模型声称看过什么。事件语义固定为：

```text
provider_projection_consumed
  request_id
  projection_fingerprint
  newly_consumed_part_ids
  projected_checkpoint_id
  model_ref
  input_usage
```

只有 provider 请求成功返回完整 `ChatResponse` 或 streaming 的 `message_completed` 后才能写入。失败、超时、取消和只有局部 stream delta 的请求不算消费成功。`newly_consumed_part_ids` 包含本次首次成功投影的用户输入、工具结果、archive retrieval 和其他持久输入；若集合为空则不写该事件。成功响应中新生成的 assistant part 视为 born-consumed，因为模型正是它的生成者。真实 usage 可以留在非持久运行指标中，避免为了重复记录整份投影而扩大 JSONL。

### 先天超窗结果

如果某个新工具原始 payload 加上本次 fixed tokens、当前其他 protected working set 和不可删除的最小 transaction envelope 后就超过 `input_capacity`，完整投影一次在物理上不可能完成。该判断必须在工具结果持久化前使用同一个 estimator；并行工具结果由 settlement 层按稳定 call 顺序累计做 admission，不能先写普通 result 再以压缩名义偷裁。

此时使用显式 `oversized_artifact_result`：

- 原始 bytes 先按内容 hash 原子写入 `ToolResultArchive`，不得丢失；
- 持久 tool result 本身是完整的结构化 manifest，包含工具名、规范化参数、MIME/编码、原始 byte/token 估算、hash、确定性头尾预览、截断声明和 session-scoped retrieval handle；
- manifest 是模型需要先成功消费的 canonical result，原始 payload 被定义为 backing artifact，而不是一份随后被静默替换的普通 result；
- `retrieve_archive` 必须支持按安全分块读取，取回的每个 chunk 继续遵守 consumed protection；
- unknown/MCP 结果也使用同一协议，不能因来源未知直接丢弃或生成不可恢复摘要。

阈值必须基于“本结果加入当前 request 是否仍可发送”动态判断，不另设与模型窗口无关的固定字符上限。测试要覆盖刚好可装入、刚好超出、多字节文本和二进制 payload 四种边界。

对于 tool result：

- `unconsumed`：尚未进入成功请求，L2/L3 和新 checkpoint 都必须保护。
- `consumed`：至少进入一次成功请求，可以继续由 lifecycle 决定是否压缩。
- `retrieved`：`retrieve_archive` 返回的内容沿用现有 turn protection，并且也必须先消费一次。

该状态由 replay 从事件构建，不写入自然语言内容。checkpoint candidate 的覆盖边界不得跨过任何 unconsumed part。

## 生命周期与 L1-L3

生命周期分类从“未知即 derived”改为显式能力声明：

```text
source_read
derived
mutation_result
control
unknown
```

规则如下：

- 内置工具在 ToolDefinition 或相邻 capability registry 中声明结果类别和可压缩策略。
- MCP adapter 可以根据 server/tool 配置声明类别；没有声明时为 `unknown`。
- `unknown`、失败结果和结构不完整的结果默认 fresh/protected，不能进入有损 L2/L3。
- `load_skill` 的完整正文至少在首次消费前受保护；消费后可以按明确的 `instruction_document` 策略归档，但不能只因长度超过 800 token 就自动视作普通 derived log。
- source read 的 stale/superseded 判定继续基于结构化路径与 mutation 事实。
- derived 结果仍可使用 typed L2 compressor 和 archive-backed L3。

L1-L3 的预算输入必须是 latest checkpoint 之后的有效投影，不再对完整 raw 账本求和。所有 level metric 同时记录 effective before/after，不再输出看似 468K、实际不进入模型的账本数字。

L2 必须先生成候选、确认严格更小，再写 archive；避免产生未被 replacement 引用的孤儿 archive。L2 与 L3 在同一次 plan 中可以连续作用于同一 part，但持久化时折叠为从当前有效 part 到最终 replacement 的单一变更，简化 replay 幂等性。`oversized_artifact_result` 是工具执行边界产生的 canonical result，不再重复经过这条“普通结果转 L3”的路径。

## Task boundary 语义

task boundary 保留“模型判断、程序生成 hash、状态机确认”的现有模式，但不再直接调用通用压缩管理器。

确认新任务后只产生一个 `TaskTransitionCleanupPlan`：

- 下一次 `prepare_provider_request()` 中，L1 可以裁剪已确认属于旧任务且不含 tool transaction 的普通文本。
- L2/L3 只处理旧任务中已经 consumed 且 lifecycle 明确允许的工具结果。
- 当前新任务消息、候选新任务起点、当前新任务工具结果全部保护。
- 清理不以 16K 固定目标强迫 L4。
- 如果清理后下一次主请求仍超过高水位，由正常 pre-request AUTO scheduler 进行一次预算驱动压缩。

task boundary 工具只提交任务状态变化，不直接调用压缩管理器。下一次 `prepare_provider_request()` 读取 pending transition，把 cleanup 与正常预算判断合并成一个 deterministic plan；如果还需要 L4，也在同一次 preparation 和同一个 `compaction_committed` 中完成。因此不再存在独立的 `TASK_HASH_CHANGED` compact，也不会因 transition 在同一 preparation 前后各跑一次 AUTO。cleanup 是否发生、改变了哪些旧任务 part，由该原子提交事件记录。

`candidate_task_basis_message_id` 必须随 pending candidate 一起持久化和恢复；确认后的第一条候选消息和后续 same 消息都要被重新标记为新 active hash。

## L4 checkpoint 候选与提交

L4 拆成三个阶段：

### 1. 构建 source

source 只包含当前有效 checkpoint summary 与可覆盖的 consumed tail。它必须保留最新用户目标、未消费结果和合法 tool transaction。source fingerprint 描述待覆盖的事实内容，不把新生成 checkpoint ID 作为导致天然变化的输入。

如果 latest checkpoint 自提交后尚未出现在成功 provider 请求中，AUTO 返回 `checkpoint_unconsumed`，复用该 checkpoint 构建请求，不再调用 summarizer。只有 provider 已用 `PROMPT_TOO_LONG` 明确拒绝这份投影时，blocking recovery 才能在一次重试额度内生成更强候选并原子标记被取代的 checkpoint；原始消息事实仍留在账本中。

### 2. 生成候选

summarizer 返回结构化 handoff 正文；tail boundary 由本地策略决定。摘要输入必须包含工具名、规范化参数、结果状态和内容，不能只拼 `part.content`。长消息采用头尾、错误块和结构化 metadata 的确定性预处理，不再无条件只取前 4,000 字符。

`stronger` 模式必须具有真实差异：更小的摘要上限、更积极但仍合法的 consumed tail 边界，以及明确的目标 token budget。它只允许在默认候选结构合法但结果为 `still_over_budget` 或 `insufficient_gain` 时执行；timeout、`no_summary`、provider error、非法 tool sequence 和覆盖 unconsumed part 不做同次 stronger 重试。若没有可进一步覆盖的 consumed 增量，直接返回 `insufficient_growth`，不调用摘要模型。

### 3. 内存验收与原子提交

在写事件前构造临时 `SessionView`，使用同一个 `ContextBuilder`、`ToolExposurePlan` 和预算 estimator 生成候选 provider 请求。必须同时满足：

- tool sequence 合法；
- checkpoint 边界前进；
- 不覆盖 unconsumed part；
- 最终 input tokens 小于等于低水位；
- 相比当前投影节省至少 `minimum_compaction_gain_tokens`；
- 本次 request preparation 尚未提交 checkpoint。

满足后只追加一个新的 `compaction_committed` 事件。该事件包含最终 L1-L3 replacements、可选 checkpoint、before/after budget、trigger、request preparation ID 和策略版本；只有执行 L4 时 checkpoint 才存在，单独 L1-L3 足以达标时仍用同一事件提交 replacements。replay 原子应用整个事件；不再依赖分离的 `checkpoint_created` 与 `llm_compaction_completed` 表达一次事务。

未通过验收时只追加一个 `compaction_attempted` 诊断事件，不改变有效 checkpoint，也不清零失败计数。由熔断、固定/保护下界或增长门槛直接拦截的后续 request preparation 不算新 attempt，不能逐请求重复追加相同诊断事件；只有 gate 原因或基线变化时才追加新的状态事件。

## 失败、重试与熔断

AUTO 一次 request preparation 最多：

```text
一次确定性 plan
一次默认 L4
一次真正 stronger L4（仅允许指定错误）
```

每次 L4 retry 都必须重新进行完整候选验收。摘要模型返回成功但仍超低水位，状态仍是 `still_over_budget`，不得转成 success。

连续失败计数只由最终 attempt 结果更新一次：

- `compaction_committed`：清零。
- 最终 `still_over_budget`、timeout、no_summary、provider_error、invalid_tool_sequence、protected_overlap、boundary_not_advanced：加一。
- `fixed_over_budget`：不计入 L4 连续失败，也不打开熔断；它阻止本次 AUTO L4，并要求先缩减固定开销、减少工具暴露或向用户报告窗口不可解。
- `protected_over_budget`：不计入 L4 连续失败，也不打开熔断；它阻止本次 AUTO L4，直到受保护结果被成功消费或当前任务保护范围发生变化。
- `checkpoint_unconsumed`：不计入 L4 连续失败，也不打开熔断；AUTO 复用现有 checkpoint，不生成新候选。
- `insufficient_growth`：不计入 L4 连续失败，也不打开熔断；它保存本次可覆盖 source 的 token 基线。在新增可覆盖 token 少于 `minimum_retry_growth_tokens` 时，后续 AUTO 直接复用该结果，不调用 summarizer；达到增长门槛后才允许新 attempt。
- stronger 后仍为 `insufficient_gain` 时，按 `insufficient_growth` 的同一增长基线处理，不计入熔断；不能在输入未增长时反复请求另一份近似摘要。
- circuit open 时，AUTO 不调用 L4，但确定性的安全 L1-L3 仍可执行。

达到 `auto_failure_threshold` 后，circuit 从最终失败事件的持久时间起打开 `auto_circuit_cooldown_seconds`。熔断截止时间、失败次数、最终原因必须进入可重放事件。manual 与 blocking recovery 可以忽略 AUTO 熔断，但仍必须经过候选验收，不能写入无效 checkpoint；它们成功时清零连续失败，失败时不延长 AUTO circuit。

## Replay 与 schema

本次使用新的 context event schema 版本，旧会话不兼容。session 创建事件写入新版本；resume、fork 和 share 在解析消息前先执行 schema gate。

replay 需要恢复：

- latest committed checkpoint；
- 最终 replacements；
- consumed part/message IDs；
- active 与 candidate task hash；
- `candidate_task_basis_message_id`；
- MCP 常驻、显式和临时激活集合及所属 task/transaction；
- 连续 AUTO 失败计数、最终原因和熔断截止时间；
- 最近预算和压缩诊断事件。

latest checkpoint 按 append sequence 选择，sequence 由 store 应用事件时覆盖或由提交事件显式携带，不能用秒级时间加随机 ID 决胜。

旧的 `compaction_completed`、`checkpoint_created`、`llm_compaction_completed` 和 `compaction_skipped` 不在新 session 中继续写入。由于明确拒绝旧 schema，生产 replay 不保留双事件路径。

## `/context` 与诊断

`/context` 直接展示最近一次或即时计算的 `ContextBudgetSnapshot`：

```text
model window
effective window
fixed tokens
  system
  tool schemas
  runtime instructions
history tokens
  protected
  compactable
output reserve
high / low watermark
unconsumed result count and tokens
latest checkpoint
last compaction outcome
circuit state
estimator and confidence
```

compaction 事件的 before/after 使用相同快照摘要。固定前缀过大时明确展示最大贡献者，不能只告诉用户“仍超预算”。工具 schema 诊断按核心工具和 MCP server 聚合，帮助识别 GitHub MCP 这类高占用来源。

## 并发与持久化边界

`compaction_committed` 是一次完整 JSON 行；写入前不改变 runtime latest checkpoint。可写 runtime 必须在整个 session 生命周期持有 OS 级独占 writer lease；同一进程内再用 session lock 串行化 append。每次 append 在锁内分配单调 sequence、写完整 JSON 行、flush 并 `fsync`，成功后才更新内存状态。第二个进程无法取得 lease 时以 session locked 失败，不能依赖 service 层的先检查后写。检测到 sequence 间隙、重复或 schema 不一致时明确报 session corrupt，而不是猜测合并。

archive 文件继续使用内容寻址与原子 rename，但 JSONL 与 archive 之间不宣称具有跨文件原子事务。提交顺序固定为：先把候选 archive 写入最终内容地址，`fsync` 文件并在 rename 后 `fsync` 父目录，再追加引用它们的 `compaction_committed`。若第二步失败，最多留下不可达的内容寻址 blob，replay 不会看到半提交 checkpoint；后台或显式维护命令可按 JSONL 引用集合安全回收孤儿 blob。绝不能先写事件再提升 archive，否则崩溃可能留下指向缺失原文的有效事件。

## 迁移顺序

重构必须通过短小、可回退的提交完成，每个提交后测试集保持可运行：

1. 先增加新行为的失败测试和真实风暴 fixture，不改生产行为。
2. 引入模型窗口配置与统一预算快照，并让 `/context` 先读取新快照。
3. 引入 ToolExposurePlan，但保持等价的“全部暴露”，用现有 provider、权限与工具循环测试锁定行为。
4. 把 AgentLoop 切到单一 pre-request preparation，删除 post-turn/tool-result 直接 AUTO。
5. 在等价路径稳定后，单独启用 MCP 目录发现与按需 schema 暴露；保留关闭开关直到重构完成。
6. 增加 projection consumed 事件和 replay，先建立保护状态。
7. 收紧 lifecycle 默认值和 load_skill/MCP 结果策略。
8. 修正 effective-tail L1-L3 估算与 replacement/archive 事务。
9. 将 task transition cleanup 从通用 manager 中拆出。
10. 引入内存 CheckpointCandidate 验收，再切换到单事件原子提交。
11. 切换失败/熔断 replay，删除旧 fallback 和双事件路径。
12. 启用新 schema gate，删除旧会话兼容代码和旧配置常量。
13. 更新中英文架构、上下文、provider、MCP 和代码阅读文档。

具体文件、测试名称和每个提交的代码步骤由用户审阅本设计后，在独立实施计划中固定。

## 测试策略

实施采用 TDD。测试验证外部行为和持久状态，不锁定私有 helper 的具体形状。

### 预算与窗口

- 32K、128K、200K 模型从各自窗口计算不同水位。
- `request.max_tokens` 改变输出预留和输入容量。
- fixed/protected/compactable 分账之和与最终 provider envelope 一致。
- 中文、tool schema、tool call 参数、图片进入估算。
- 固定前缀超过低水位时零次 L4，返回 `fixed_over_budget`。
- 固定前缀加未消费结果超过低水位时零次 L4，返回 `protected_over_budget`。

### 触发与消费

- 最终 assistant 回复后不 compact。
- 工具结果写入后，在第一次成功 follow-up 请求前保持完整。
- 失败或取消的 provider 请求不把结果标记 consumed。
- streaming 只有 `message_completed` 才提交 consumed 事件。
- 同一 request preparation 最多一个 committed checkpoint。
- provider 请求失败后，下一次 AUTO 复用尚未消费的 checkpoint，不再生成一个新 checkpoint。

### 生命周期与 MCP/skill

- 未声明的 MCP 工具结果默认为 unknown/protected。
- 显式 derived MCP 输出在 consumed 后可压缩。
- GitHub source result 不因长度被压成普通 derived preview。
- 大 `load_skill` 正文完整出现在模型下一次请求中。
- retrieved archive 内容至少成功投影一次后才能再次压缩。
- 单个先天超窗结果从产生时持久化为 backing artifact 加完整 manifest，并能分块恢复原文。

### Task boundary

- confirmed task switch 只清旧任务。
- 当前新任务和 candidate 起始消息不被 L2/L3 处理。
- task boundary 不直接 compact；它的 cleanup 与下一次 pre-request AUTO 合并，单次 preparation 只产生一个最终提交。
- resume 后 pending candidate 的 basis message 可以被正确重新标记。

### L4 与 replay

- 候选仍超低水位时不写有效 checkpoint。
- `still_over_budget` 不会被摘要模型 success 覆盖。
- 连续三次最终失败后熔断，resume 后仍保持。
- 新 checkpoint 的 source fingerprint 不因随机 checkpoint ID 天然变化。
- 同秒产生候选时 latest 仍由 append sequence 决定。
- 进程在候选生成与提交之间退出，不改变有效投影。
- provider 明确以 `PROMPT_TOO_LONG` 拒绝未消费 checkpoint 时，blocking recovery 最多替换一次并仍需通过统一验收。
- replay 一次或多次结果相同。

### 风暴回归

使用缩小后的真实事件 fixture 构造：固定前缀接近目标、每轮新增少量 tool result、连续多轮请求。断言：

- 大前缀本身不导致每轮 checkpoint；
- checkpoint 之后没有足够增长时不再次调用 summarizer；
- 五分钟窗口内 committed checkpoint 数受请求增长和低水位约束，而不是与工具轮数一一对应；
- `/context` 数字与发送给 fake provider 的 envelope 预算一致。

先运行聚焦测试，再串行运行：

```sh
.venv/bin/python -m pytest tests
```

根目录 `pytest` 会采集 benchmark/runs，不作为本重构的完成证据。

## 验收标准

- 不同模型按各自窗口和实际输出预留触发压缩。
- 预算快照能解释 system、schema、历史和输出分别占多少。
- 52 个 MCP schema 不再无条件全部常驻每次主请求。
- 任意工具结果在模型第一次成功消费前保持完整。
- 未声明 MCP/source 与大 skill 不再被默认当作 derived log。
- task boundary 不再触发重复通用 compact，也不压当前新任务。
- 无效 L4 候选不进入 effective checkpoint；fallback 不会假成功。
- 熔断状态可重放，真实 checkpoint 风暴回归测试通过。
- `/context` 与真正 provider 请求共享同一预算口径。
- 新 session 使用单一原子 compaction event，旧 context 事件生产路径和兼容分支被删除。
- 聚焦测试和完整 `tests` 测试集串行通过。

## 明确保留的现有边界

- `JsonlSessionStore` 继续作为 append-only 事实存储，只增强 schema、锁和原子事件语义。
- `SessionView` 继续作为 replay 后的有效事实视图。
- `ContextBuilder` 继续独占 provider 消息投影和 tool sequence 校验。
- `ToolResultArchive` 继续保存可恢复原文。
- `retrieve_archive` 继续是 session-scoped、安全的恢复入口。
- provider adapters 继续负责各自 wire format；上下文层只依赖统一 envelope、能力和 estimator 接口。

这次是控制面重构，不是把已经稳定的事实层和投影层推倒重写。
