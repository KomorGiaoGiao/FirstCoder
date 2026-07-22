# FirstCoder 上下文压缩窄范围改造设计

日期：2026-07-22

## 背景

FirstCoder 已经有完整的上下文基础设施：append-only JSONL、`SessionView` 重放、provider 投影、L1-L3、archive/retrieve、L4 checkpoint、task boundary 和 resume。本次不替换这些机制，只修四个明确问题：

1. 所有模型共用固定的 32K 触发阈值、24K 目标和 4,096 输出预留；
2. AUTO 分散在用户消息后、工具结果后和最终回复后，触发过早且重复；
3. 工具结果可能在模型第一次完整看到前进入 L2/L3/L4；
4. L4 先写 checkpoint，再检查是否真的压到目标，不合格 checkpoint 已经落盘。

## 目标

本次只完成四项改造：

1. **动态阈值**：根据当前模型窗口和本次输出预留计算高、低水位；
2. **pre-request AUTO**：普通 AUTO 只在即将调用主 provider 前执行；
3. **首次消费保护**：tool result 完整进入一次成功主请求后才能有损压缩；
4. **L4 两阶段提交**：先在内存中验收候选，合格后才写 checkpoint。

同时让主循环、manager、pipeline 和 `/context` 使用同一个 provider-facing token 估算入口。

## 明确不做

- 不重写 JSONL、`SessionView` 或 `ContextBuilder`；
- 不改 L1/L2/L3 的内容压缩算法；
- 不改 archive/retrieve 格式；
- 不重写 task boundary 分类和状态机；
- 不做 MCP schema 按需暴露；
- 不新增工具 capability 分类系统；
- 不做 oversized artifact 或大对象分块协议；
- 不接各 provider tokenizer；
- 不做 breaking session schema；
- 不移动或重命名整个 `context/` 模块。

固定工具 schema 如果过大，本次只识别和报告，不顺手重构工具系统。

## 必须保持的不变量

1. 原始 user、assistant、tool result 事件继续 append-only。
2. L1-L3 继续通过 `compaction_completed` replacement 改变有效投影。
3. archive 原文继续可以通过 session-scoped `retrieve_archive` 恢复。
4. assistant tool call 与 tool result 始终成对且顺序合法。
5. 未消费 tool result 不得被 L2/L3 替换，也不得被新 checkpoint 覆盖。
6. provider 失败、超时、取消或 stream 未完成时，不得记录消费成功。
7. L4 摘要返回文本不等于成功；候选投影低于低水位才算成功。
8. 不合格 L4 候选不得写 `checkpoint_created`。
9. `/compact` 和 `PROMPT_TOO_LONG` 仍可强制运行，但不能绕过安全不变量。

## 改造后的主链路

```text
即将调用主 provider
  -> 构建本次 provider messages 和 tools
  -> 计算统一预算
  -> input_tokens < high_watermark
       -> 直接发送
  -> input_tokens >= high_watermark
       -> 运行一次 L1-L3
       -> 已低于 low_watermark：发送
       -> 仍超预算：生成 L4 内存候选
            -> 校验未消费边界、tool sequence 和预算
            -> 合格：提交 checkpoint，重建后发送
            -> 不合格：不提交，返回结构化失败
  -> provider 成功完成
  -> 记录本次首次完整投影的 tool-result part ids
```

## 一、动态阈值与统一估算

### 模型配置

现有 `ModelProfile` 新增一个字段：

```toml
[models."yurenapi/gpt-5.5"]
context_window = 128000

[models."yurenapi/gpt-5.5".request]
max_tokens = 8192
```

`request.max_tokens` 已经存在，直接作为输出预留，不再新增重复字段。

取值规则：

- `context_window` 显式配置时使用配置值，来源标记为 `configured`；
- 未配置时使用集中定义的保守默认 `32768`，来源标记为 `assumed`；
- 不根据模型名称猜窗口；
- `request.max_tokens` 未配置时沿用集中默认 `4096`。

`context_window`、`request.max_tokens` 必须是正整数，且输出预留必须小于可用窗口，否则配置解析直接报错。

### 高低水位

第一版固定以下公式，不暴露额外比例配置：

```text
usable_window = floor(context_window * 0.95)
output_reserve = request.max_tokens or 4096
input_capacity = usable_window - output_reserve
high_watermark = floor(input_capacity * 0.90)
low_watermark = floor(input_capacity * 0.72)
```

- 高水位只负责触发普通 AUTO；
- 触发后必须压到低水位才验收成功；
- 高低水位之间的间隔用于避免刚压完又立刻触发。

示例：

| 窗口 | 输出预留 | 输入容量 | 高水位 | 低水位 |
| ---: | ---: | ---: | ---: | ---: |
| 32,768 | 4,096 | 27,033 | 24,329 | 19,463 |
| 128,000 | 8,192 | 113,408 | 102,067 | 81,653 |
| 200,000 | 8,192 | 181,808 | 163,627 | 130,901 |

### 统一预算对象

在 `context/token_budget.py` 增加一个小型结果对象：

```python
@dataclass(frozen=True, slots=True)
class ContextBudget:
    context_window: int
    output_reserve: int
    fixed_tokens: int
    history_tokens: int
    input_tokens: int
    high_watermark: int
    low_watermark: int
    source: str  # configured | assumed
```

估算范围固定为：

```text
fixed_tokens
  = system prefix + runtime instruction + 当前暴露的工具 schema

history_tokens
  = checkpoint summary + effective tail + tool call 参数 + rich content 保守估算

input_tokens
  = fixed_tokens + history_tokens
```

第一版继续使用现有 provider-neutral 字符估算，不引入 tokenizer。图片使用一个集中常量估算，不能在调用点各写不同数值。

`AgentLoop` 为一次主请求构建预算，并把同一个 provider-facing 估算回调传给 manager/pipeline。L1、L2、L3 每完成一级，都重新投影有效上下文再估算；不得再用完整 raw `SessionView` 判断是否达标。checkpoint 已覆盖的旧历史不能重复计入。

`/context` 复用同一个计算入口，至少显示：窗口来源、输出预留、fixed、history、input、高水位和低水位。

### 固定上下文无解

若：

```text
fixed_tokens >= low_watermark
```

manager 返回 `fixed_context_over_budget`，不运行 L4、不写 checkpoint。界面显示 fixed/history 拆分，让用户自行减少 AGENTS.md、skill catalog 或 MCP 工具数量。

## 二、AUTO 只在主请求前执行

同步和异步主 provider 调用统一经过一个窄的请求准备步骤：

```python
prepare_main_provider_request(...)
```

它只负责：

1. 构建本次 messages 和 tool definitions；
2. 计算预算；
3. 必要时调用一次 AUTO compact；
4. compact 后重建最终 `ChatRequest`；
5. 返回本次实际投影的 tool-result part ids，供成功后记录消费。

删除以下普通 AUTO 调用点：

- 用户消息刚写入后；
- 权限恢复 tool result 刚写入后；
- 每轮工具执行完成后；
- 最终 assistant 回复落盘后。

工具循环会在下一次主请求前自然进入准备步骤，因此仍会及时检查，但不会早于模型首次消费。

隐藏的 task-boundary 分类请求和 L4 摘要请求不经过这个入口，避免递归 AUTO。

保留：

- `/compact` 的 manual trigger；
- provider 明确 `PROMPT_TOO_LONG` 后的一次阻塞恢复和一次主请求重试；
- 现有 `TASK_HASH_CHANGED` 强制整理语义，但移除它附近重复的普通 AUTO。

## 三、工具结果首次消费保护

### 消费定义

“已消费”只表示该 tool-result part 的完整当前投影进入过一次成功的主 provider 请求。

算成功：

- 同步 `provider.complete()` 返回完整 `ChatResponse`；
- streaming 收到 `message_completed` 和完整 response。

不算成功：

- provider error、prompt-too-long、timeout 或取消；
- stream 只有部分 delta；
- L4 summarizer 或 task-boundary classifier 看过内容。

### 新事件和重放

新增 append-only 事件：

```json
{
  "type": "provider_projection_consumed",
  "payload": {
    "request_id": "req_...",
    "projection_fingerprint": "...",
    "part_ids": ["part_..."],
    "provider": "...",
    "model": "..."
  }
}
```

只写本次首次变为 consumed 的 tool-result part ids；为空则不写。`runtime_replay.py` 通过事件并集恢复 `consumed_tool_result_part_ids`。

旧会话没有该事件时，已有结果默认未消费。resume 后第一次成功主请求只记录当次真实完整投影中的结果，不伪造 checkpoint 已覆盖结果的消费状态。这是保守兼容，不提升 session schema。

### 压缩保护

- L2/L3 先检查 part id 是否 consumed，未消费直接跳过；
- L1 只处理普通旧任务文本，行为不变；
- L4 tail boundary 不得越过未消费 tool result 所属的 assistant-tool transaction；
- consumed 之后，继续使用现有 `fresh/stale/superseded/derived/duplicate` 规则；
- `retrieve_archive` 新返回的结果同样需要首次消费，现有当前轮保护继续保留。

本次不改 lifecycle 分类器。即使未知成功工具暂时仍被归为 `derived`，首次消费门槛也能防止它第一次回喂模型前被压缩。

若单个结果先天无法装入当前窗口，本次不静默压缩，也不引入大对象协议；最终返回明确的 `unconsumed_result_over_budget`。大对象协议以后单独立项。

## 四、L4 两阶段提交

当前流程：

```text
生成摘要 -> 写 checkpoint -> rebuild -> 发现仍超预算
```

改为：

```python
candidate = l4_service.generate_candidate(request)
# manager 在内存中投影和验收
checkpoint = l4_service.commit_candidate(candidate)
```

`generate_candidate()`：

- 复用现有 L4 source 和 summarizer；
- 选择并校验 tail boundary；
- 遵守未消费 transaction 边界；
- 返回尚未落盘的 `CheckpointCandidate`；
- 不修改 store 和 runtime state。

manager 将 candidate 临时加入克隆的 `SessionView`，然后：

1. 使用现有 `ContextBuilder` 构建候选 provider 投影；
2. 校验 tool-call/result 顺序；
3. 使用统一预算重新计算 `input_tokens`；
4. 要求候选低于当前 `low_watermark`。

全部通过后才 `commit_candidate()`，并且一次准备过程最多写一个 `checkpoint_created`。

未通过时：

- 不写 checkpoint；
- 写失败的 `llm_compaction_completed`；
- reason 为 `invalid_tool_sequence`、`unconsumed_boundary`、`still_over_budget` 或 provider 原始失败原因；
- 继续使用现有有限 fallback 和 AUTO 熔断，不新增调度器。

`/compact` 和 `PROMPT_TOO_LONG` 也必须经过候选验收，不能绕过未消费保护或 tool sequence 校验。

## 文件改动边界

| 区域 | 预期改动 |
| --- | --- |
| `firstcoder/config/models.py` | 解析 `context_window`，复用现有 `request.max_tokens` |
| `firstcoder/context/token_budget.py` | `ContextBudget` 和统一 provider-facing 估算 |
| `firstcoder/agent/loop.py` | pre-request AUTO，成功后记录消费 |
| `firstcoder/context/manager.py` | 动态高低水位和 L4 candidate 验收 |
| `firstcoder/context/compaction.py` | 统一估算回调和 consumed 门槛 |
| `firstcoder/context/llm_compact.py` | generate/commit 两阶段与未消费边界 |
| `firstcoder/context/writer.py` | 追加 consumption event |
| `firstcoder/context/runtime_state.py`、`runtime_replay.py` | 保存并重放 consumed ids |
| `firstcoder/context/inspector.py`、`firstcoder/app/commands.py` | 展示统一预算 |

不因本次改造移动整个模块或重命名现有公共类型。

## TDD 验收

### 动态预算

- 32K、128K、200K 得到预期高低水位；
- 配置值和 assumed fallback 正确；
- 非法窗口与输出预留报错；
- checkpoint 前 raw history 不重复计入；
- `/context` 和 AUTO 使用相同预算；
- fixed 超低水位时不调用 L4。

### 触发时机

- 用户消息、工具结果和最终回复落盘后不立即 AUTO；
- 同步、异步主请求前各只 AUTO 一次；
- 隐藏分类和 L4 摘要不递归 AUTO；
- prompt-too-long 最多恢复并重试一次；
- task hash 变化附近没有 `AUTO -> TASK_HASH_CHANGED -> AUTO`。

### 消费保护

- 新结果首次成功主请求前不进入 L2/L3/L4；
- error、timeout、cancel、部分 stream 不写消费事件；
- 完整同步 response 和 `message_completed` 后写事件；
- 同一 part 不重复写；
- resume 恢复 consumed 集合；
- consumed 后继续按现有 lifecycle 压缩。

### L4 提交

- candidate 生成时 store 没有新 checkpoint；
- 非法边界、未消费边界和仍超低水位均不提交；
- 合格候选恰好提交一个 checkpoint；
- fallback 不留下失败 candidate；
- resume 只看到已提交 checkpoint；
- 候选和最终投影的 tool sequence 都合法。

### 回归

- L1、typed L2、L3 archive/retrieve 行为保持；
- `/compact`、resume、fork、share 保持可用；
- OpenAI-compatible、Anthropic、同步和 streaming fake tests 通过；
- 上下文专项测试通过；
- `.venv/bin/python -m pytest tests` 通过，若有基线失败则改前改后分别复现和报告。

## 后续实施顺序

后续实现计划按以下顺序拆成独立 TDD 提交：

1. `context_window` 配置与 `ContextBudget`；
2. manager、pipeline 和 `/context` 统一估算；
3. AUTO 收敛到主请求前；
4. consumption event、成功响应记录和 replay；
5. L2/L3/L4 consumed 保护；
6. L4 candidate 生成、内存验收和提交；
7. manual、prompt-too-long、task boundary 回归；
8. 上下文专项测试与完整 `pytest tests`。

每一步先写失败测试，再做最小实现。MCP 按需暴露、大对象协议、工具 capability 和 tokenizer 估算均不得顺手进入本次改造。

## 完成标准

全部满足才算完成：

1. 固定 32K/24K 被动态高低水位替代；
2. 普通 AUTO 只发生在主 provider 请求前；
3. 实际请求、manager、pipeline 和 `/context` 使用同一估算入口；
4. 未消费 tool result 无法进入 L2/L3/L4 覆盖范围；
5. 不合格 L4 candidate 不产生 checkpoint；
6. append-only、archive/retrieve、tool sequence 和 resume 不变量保持；
7. 约定测试全部通过。
