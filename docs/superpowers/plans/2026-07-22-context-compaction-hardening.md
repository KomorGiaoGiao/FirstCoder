# FirstCoder Context Compaction Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在保留现有 JSONL、SessionView、L1-L4 和 archive/retrieve 边界的前提下，实现动态上下文阈值、主请求前 AUTO、工具结果首次消费保护和 L4 checkpoint 两阶段提交。

**Architecture:** 使用 `ContextBudget` 统一计算真实 provider 输入、高水位和低水位；AgentLoop 只在构造主请求时触发 AUTO，并在完整 provider 响应后写入消费事件。L1-L3 在现有 lifecycle 之前增加 consumed 硬门槛，L4 改为“生成内存 candidate → manager 验收 → commit”。

**Tech Stack:** Python 3.11+、dataclasses、pytest、append-only JSONL、现有 provider-neutral ChatRequest/ChatMessage、Textual TUI 命令层。

---

## 实施约束

- 只修改设计稿列出的压缩链路，不引入 MCP 按需暴露、oversized artifact、provider tokenizer 或新 task-boundary 状态机。
- 不保留固定 32K/24K 的第二套运行路径；最终所有主请求预算都走 `ContextBudget`。
- 不破坏旧 session：缺少 consumption event 的旧 tool result 按未消费处理。
- 每个任务先跑指定失败测试，再写最小实现，再跑指定回归，最后独立提交。
- 当前基线命令：

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_model_request_options.py \
  tests/test_context_window_manager.py tests/test_context_compaction_pipeline.py \
  tests/test_context_llm_compact.py tests/test_context_runtime_replay.py \
  tests/test_context_inspector.py tests/test_app_context_commands.py \
  tests/test_agent_context_loop.py -q
```

基线结果：`221 passed`。

## 文件职责图

| 文件 | 本计划中的唯一新增职责 |
| --- | --- |
| `firstcoder/config/models.py` | 解析模型 `context_window` |
| `firstcoder/context/token_budget.py` | 统一计算 provider-facing `ContextBudget` |
| `firstcoder/app/runtime.py`、`firstcoder/app/factory.py` | 在模型切换和 loop 创建时传递窗口 |
| `firstcoder/context/writer.py`、`runtime_state.py`、`runtime_replay.py` | 持久化并恢复 consumed tool-result part ids |
| `firstcoder/context/compaction.py` | L1-L3 使用统一估算并保护未消费结果 |
| `firstcoder/context/llm_compact.py` | 生成/提交 checkpoint candidate，保护未消费边界 |
| `firstcoder/context/manager.py` | 用高低水位编排 L1-L4，并验收 L4 candidate |
| `firstcoder/agent/loop.py` | 主请求前 AUTO、成功响应后记录消费 |
| `firstcoder/context/inspector.py`、`firstcoder/app/commands.py` | 展示同一预算和未消费数量 |

### Task 1: 模型窗口配置和统一预算原语

**Files:**
- Modify: `firstcoder/config/models.py:48-55,129-187`
- Modify: `firstcoder/context/token_budget.py:1-52`
- Modify: `firstcoder/config/settings.py:191-215`
- Test: `tests/test_config.py`
- Create: `tests/test_context_token_budget.py`

- [ ] **Step 1: 写模型窗口配置失败测试**

在 `tests/test_config.py` 增加：

```python
def test_model_catalog_reads_context_window() -> None:
    config = AppConfig(
        provider_name="custom",
        env={},
        project_config={
            "providers": {"custom": {"type": "openai-compatible"}},
            "models": {
                "custom/model": {
                    "context_window": 128_000,
                    "request": {"max_tokens": 8_192},
                }
            },
        },
    )

    profile = config.model_catalog().require("custom/model")

    assert profile.context_window == 128_000
    assert profile.request.max_tokens == 8_192


@pytest.mark.parametrize("value", [0, -1, True, "128000"])
def test_model_catalog_rejects_invalid_context_window(value) -> None:
    config = AppConfig(
        provider_name="custom",
        env={},
        project_config={
            "providers": {"custom": {"type": "openai-compatible"}},
            "models": {"custom/model": {"context_window": value}},
        },
    )

    with pytest.raises(ModelCatalogError, match="context_window"):
        config.model_catalog()


def test_model_catalog_rejects_output_reserve_that_exhausts_window() -> None:
    config = AppConfig(
        provider_name="custom",
        env={},
        project_config={
            "providers": {"custom": {"type": "openai-compatible"}},
            "models": {
                "custom/model": {
                    "context_window": 1_000,
                    "request": {"max_tokens": 950},
                }
            },
        },
    )

    with pytest.raises(ModelCatalogError, match="max_tokens.*context_window"):
        config.model_catalog()
```

- [ ] **Step 2: 运行配置测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py::test_model_catalog_reads_context_window \
  tests/test_config.py::test_model_catalog_rejects_invalid_context_window \
  tests/test_config.py::test_model_catalog_rejects_output_reserve_that_exhausts_window -q
```

Expected: FAIL，`ModelProfile` 没有 `context_window`，无效值和窗口/输出预留组合也未校验。

- [ ] **Step 3: 实现最小模型窗口解析**

先在 `firstcoder/context/token_budget.py` 顶部加入集中默认值，供配置校验和运行时预算共同使用：

```python
DEFAULT_CONTEXT_WINDOW = 32_768
DEFAULT_OUTPUT_RESERVE = 4_096
```

再在 `firstcoder/config/models.py` 把模型定义改为：

```python
from firstcoder.context.token_budget import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_OUTPUT_RESERVE,
)


@dataclass(frozen=True, slots=True)
class ModelProfile:
    ref: str
    provider_id: str
    model_id: str
    label: str
    provider: ProviderProfile
    request: ModelRequestOptions
    context_window: int | None = None


def _optional_positive_int(value: Any, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ModelCatalogError(f"{field_name} 必须是大于 0 的整数")
    return value


def _validate_context_capacity(
    *,
    ref: str,
    context_window: int | None,
    max_tokens: int | None,
) -> None:
    resolved_window = context_window or DEFAULT_CONTEXT_WINDOW
    resolved_output = max_tokens or DEFAULT_OUTPUT_RESERVE
    if resolved_output >= int(resolved_window * 0.95):
        raise ModelCatalogError(
            f"模型 {ref}.request.max_tokens 必须小于 context_window 的 95% 可用窗口"
        )
```

解析每个模型时先得到 request/window，执行联合校验，再使用关键字参数构造 profile：

```python
request = _request_options(raw.get("request"), ref=ref)
context_window = _optional_positive_int(
    raw.get("context_window"),
    f"模型 {ref}.context_window",
)
_validate_context_capacity(
    ref=ref,
    context_window=context_window,
    max_tokens=request.max_tokens,
)
profiles.append(
    ModelProfile(
        ref=ref,
        provider_id=provider_id,
        model_id=model_id,
        label=label,
        provider=provider,
        request=request,
        context_window=context_window,
    )
)
```

同时在 `render_default_config()` 的默认模型段加入示例：

```python
'[models."yurenapi/gpt-5.5"]',
'label = "GPT-5.5"',
'context_window = 128000',
```

- [ ] **Step 4: 写动态预算失败测试**

创建 `tests/test_context_token_budget.py`：

```python
import pytest

from firstcoder.context.token_budget import build_context_budget
from firstcoder.providers.types import ChatMessage, ContentPart, ToolDefinition


@pytest.mark.parametrize(
    ("window", "output", "capacity", "high", "low"),
    [
        (32_768, 4_096, 27_033, 24_329, 19_463),
        (128_000, 8_192, 113_408, 102_067, 81_653),
        (200_000, 8_192, 181_808, 163_627, 130_901),
    ],
)
def test_budget_uses_dynamic_watermarks(window, output, capacity, high, low) -> None:
    budget = build_context_budget(
        messages=[],
        tools=[],
        context_window=window,
        max_output_tokens=output,
    )

    assert budget.input_capacity == capacity
    assert budget.high_watermark == high
    assert budget.low_watermark == low


def test_budget_separates_fixed_and_history_tokens() -> None:
    budget = build_context_budget(
        messages=[
            ChatMessage(role="system", content="s" * 40),
            ChatMessage(role="user", content="u" * 80),
        ],
        tools=[ToolDefinition(name="read", description="d" * 40, parameters={})],
        context_window=32_768,
        max_output_tokens=4_096,
    )

    assert budget.fixed_tokens > 10
    assert budget.history_tokens == 20
    assert budget.input_tokens == budget.fixed_tokens + budget.history_tokens


def test_budget_counts_image_once_without_counting_base64_bytes() -> None:
    budget = build_context_budget(
        messages=[
            ChatMessage(
                role="user",
                content="describe",
                content_parts=[
                    ContentPart(type="text", text="describe"),
                    ContentPart(type="image", media_type="image/png", data_base64="x" * 100_000),
                ],
            )
        ],
        tools=[],
        context_window=None,
        max_output_tokens=None,
    )

    assert budget.source == "assumed"
    assert budget.history_tokens < 2_000


@pytest.mark.parametrize(
    ("window", "output"),
    [(0, 1), (1_000, 1_000), (1_000, 2_000)],
)
def test_budget_rejects_invalid_capacity(window, output) -> None:
    with pytest.raises(ValueError):
        build_context_budget(
            messages=[],
            tools=[],
            context_window=window,
            max_output_tokens=output,
        )
```

- [ ] **Step 5: 运行预算测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_token_budget.py -q
```

Expected: FAIL，`build_context_budget` 和 `ContextBudget` 尚不存在。

- [ ] **Step 6: 实现 `ContextBudget`**

在 `firstcoder/context/token_budget.py` 保留现有 `estimate_text_tokens()`，新增：

```python
from dataclasses import dataclass
from typing import Literal

DEFAULT_CONTEXT_WINDOW = 32_768
DEFAULT_OUTPUT_RESERVE = 4_096
IMAGE_INPUT_TOKEN_ESTIMATE = 1_024


@dataclass(frozen=True, slots=True)
class ContextBudget:
    context_window: int
    output_reserve: int
    input_capacity: int
    fixed_tokens: int
    history_tokens: int
    input_tokens: int
    high_watermark: int
    low_watermark: int
    source: Literal["configured", "assumed"]


def build_context_budget(
    *,
    messages: list[ChatMessage],
    tools: list[ToolDefinition],
    context_window: int | None,
    max_output_tokens: int | None,
) -> ContextBudget:
    resolved_window = DEFAULT_CONTEXT_WINDOW if context_window is None else context_window
    output_reserve = DEFAULT_OUTPUT_RESERVE if max_output_tokens is None else max_output_tokens
    if resolved_window <= 0 or output_reserve <= 0:
        raise ValueError("context window and output reserve must be positive")
    usable_window = int(resolved_window * 0.95)
    input_capacity = usable_window - output_reserve
    if input_capacity <= 0:
        raise ValueError("output reserve must be smaller than usable context window")

    fixed_messages = [message for message in messages if message.role == "system"]
    history_messages = [message for message in messages if message.role != "system"]
    fixed_tokens = sum(_estimate_chat_message_tokens(message) for message in fixed_messages)
    fixed_tokens += _estimate_tool_definition_tokens(tools)
    history_tokens = sum(_estimate_chat_message_tokens(message) for message in history_messages)
    high_watermark = int(input_capacity * 0.90)
    low_watermark = int(input_capacity * 0.72)
    if low_watermark >= high_watermark:
        raise ValueError("low watermark must be below high watermark")
    return ContextBudget(
        context_window=resolved_window,
        output_reserve=output_reserve,
        input_capacity=input_capacity,
        fixed_tokens=fixed_tokens,
        history_tokens=history_tokens,
        input_tokens=fixed_tokens + history_tokens,
        high_watermark=high_watermark,
        low_watermark=low_watermark,
        source="configured" if context_window is not None else "assumed",
    )


def _estimate_chat_message_tokens(message: ChatMessage) -> int:
    tokens = estimate_text_tokens(message.content)
    tokens += estimate_text_tokens(message.name or "")
    tokens += estimate_text_tokens(message.tool_call_id or "")
    tokens += sum(
        estimate_text_tokens(call.name + json.dumps(call.arguments, ensure_ascii=False, sort_keys=True))
        for call in message.tool_calls
    )
    tokens += sum(
        IMAGE_INPUT_TOKEN_ESTIMATE
        for part in message.content_parts or []
        if part.type == "image"
    )
    return tokens


def _estimate_tool_definition_tokens(tools: list[ToolDefinition]) -> int:
    return sum(
        estimate_text_tokens(
            json.dumps(
                {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        for tool in tools
    )
```

保留 `estimate_chat_request_tokens()` 作为非主请求测试/内部调用的薄包装，但让它复用 `_estimate_chat_message_tokens()` 和 `_estimate_tool_definition_tokens()`，不得保留另一套计算公式。

- [ ] **Step 7: 运行 Task 1 测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_config.py tests/test_context_token_budget.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交 Task 1**

```bash
git add firstcoder/config/models.py firstcoder/config/settings.py \
  firstcoder/context/token_budget.py tests/test_config.py \
  tests/test_context_token_budget.py
git commit -m "Add dynamic context budgets"
```

### Task 2: 把模型窗口传到运行中的 AgentLoop

**Files:**
- Modify: `firstcoder/agent/loop.py:67-105`
- Modify: `firstcoder/app/runtime.py:75-114,281-299`
- Modify: `firstcoder/app/factory.py:120-169,208-225,316-340`
- Test: `tests/test_app_model_commands.py`
- Test: `tests/test_model_request_options.py`

- [ ] **Step 1: 写窗口传播失败测试**

在 `tests/test_model_request_options.py` 增加：

```python
from firstcoder.app.runtime import AgentChatRunner, CurrentSessionState
from firstcoder.runtime.cancellation import CancellationToken


def test_chat_runner_passes_context_window_to_agent_loop(tmp_path) -> None:
    provider = RecordingProvider()
    session = _session(tmp_path)
    runner = AgentChatRunner(
        current_session=CurrentSessionState(session),
        provider=provider,
        context_window=128_000,
        request_options=MainRequestOptions(max_tokens=8_192),
    )

    loop = runner._create_loop(CancellationToken())

    assert loop.context_window == 128_000
    assert loop.request_options.max_tokens == 8_192
```

在 `tests/test_app_model_commands.py` 的 profile switch 测试中加入：

```python
assert chat_runner.context_window == 200_000
```

并让该测试 profile 使用：

```python
ModelProfile(
    ref="custom/model",
    provider_id="custom",
    model_id="model",
    label="Model",
    provider=provider_profile,
    request=ModelRequestOptions(max_tokens=8_192),
    context_window=200_000,
)
```

- [ ] **Step 2: 运行传播测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_model_request_options.py::test_chat_runner_passes_context_window_to_agent_loop \
  tests/test_app_model_commands.py -q
```

Expected: FAIL，runner/loop 尚无 `context_window`。

- [ ] **Step 3: 实现窗口传播**

在 `AgentLoop.__init__()` 增加：

```python
context_window: int | None = None,
```

并保存：

```python
self.context_window = context_window
```

在 `AgentChatRunner` 增加字段并传给 loop：

```python
context_window: int | None = None
```

```python
kwargs = {
    "session": self.current_session.session,
    "provider": self.provider,
    "tools": self._current_tools(),
    "context_builder": self.context_builder,
    "context_manager": self.context_manager,
    "limits": self.limits,
    "tool_event_handler": self.tool_event_handler,
    "guidance_provider": self.drain_guidance,
    "cancellation_token": cancellation_token,
    "background_manager": self.background_manager,
    "request_options": self.request_options,
    "context_window": self.context_window,
}
```

扩展模型切换接口：

```python
def set_model(
    self,
    provider: ChatProvider,
    *,
    request_options: MainRequestOptions,
    context_window: int | None,
    use_streaming: bool,
) -> None:
    self.provider = provider
    self.request_options = request_options
    self.context_window = context_window
    self.use_streaming = use_streaming
    self.last_stream_events = []
```

无 catalog 的 `set_provider()` 显式传 `context_window=None`。`RuntimeModelSwitcher._apply_profile()` 传：

```python
self._chat_runner.set_model(
    provider,
    request_options=_main_request_options(profile),
    context_window=profile.context_window,
    use_streaming=_should_use_streaming(provider, self._app_config),
)
```

首次构造 runner 时加入：

```python
context_window=selected_profile.context_window if selected_profile is not None else None,
```

- [ ] **Step 4: 运行 Task 2 测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_model_request_options.py \
  tests/test_app_model_commands.py tests/test_app_factory.py -q
```

Expected: PASS。

- [ ] **Step 5: 提交 Task 2**

```bash
git add firstcoder/agent/loop.py firstcoder/app/runtime.py firstcoder/app/factory.py \
  tests/test_model_request_options.py tests/test_app_model_commands.py tests/test_app_factory.py
git commit -m "Propagate model context windows"
```

### Task 3: 持久化工具结果首次消费状态

**Files:**
- Modify: `firstcoder/context/identity.py:16-39`
- Modify: `firstcoder/context/runtime_state.py:72-148`
- Modify: `firstcoder/context/runtime_replay.py:25-52`
- Modify: `firstcoder/context/writer.py:199-260`
- Modify: `firstcoder/agent/session.py:459-525`
- Test: `tests/test_context_runtime_replay.py`
- Test: `tests/test_context_writer.py`

- [ ] **Step 1: 写 consumption event/replay 失败测试**

在 `tests/test_context_runtime_replay.py` 增加：

```python
def test_replay_unions_consumed_tool_result_part_ids(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    for event_id, part_ids in (
        ("evt_1", ["part_a", "part_b"]),
        ("evt_2", ["part_b", "part_c"]),
    ):
        store.append_event(
            SessionEvent(
                id=event_id,
                session_id="sess_test",
                type="provider_projection_consumed",
                payload={
                    "request_id": f"req_{event_id}",
                    "projection_fingerprint": f"fp_{event_id}",
                    "part_ids": part_ids,
                    "provider": "fake",
                    "model": "fake-model",
                },
            )
        )

    state = replay_runtime_state(store, "sess_test")

    assert state.consumed_tool_result_part_ids == {"part_a", "part_b", "part_c"}
```

在 `tests/test_context_writer.py` 增加：

```python
def test_writer_appends_projection_consumed_event(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_test")

    writer.append_provider_projection_consumed(
        request_id="req_1",
        projection_fingerprint="fp_1",
        part_ids=["part_b", "part_a", "part_a"],
        provider="fake",
        model="fake-model",
    )

    event = store.list_events("sess_test")[-1]
    assert event.type == "provider_projection_consumed"
    assert event.payload["part_ids"] == ["part_a", "part_b"]
```

- [ ] **Step 2: 运行消费状态测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_runtime_replay.py::test_replay_unions_consumed_tool_result_part_ids \
  tests/test_context_writer.py::test_writer_appends_projection_consumed_event -q
```

Expected: FAIL，runtime 字段和 writer 方法不存在。

- [ ] **Step 3: 实现 event、runtime 和 replay**

在 `SessionRuntimeState` 增加：

```python
consumed_tool_result_part_ids: set[str] = field(default_factory=set)
```

在 `identity.py` 增加：

```python
def new_request_id() -> str:
    return _new_id("req")
```

在 `SessionEventWriter` 增加：

```python
def append_provider_projection_consumed(
    self,
    *,
    request_id: str,
    projection_fingerprint: str,
    part_ids: list[str],
    provider: str,
    model: str,
) -> None:
    normalized = sorted({part_id for part_id in part_ids if part_id})
    if not normalized:
        return
    self.append_event(
        "provider_projection_consumed",
        {
            "request_id": request_id,
            "projection_fingerprint": projection_fingerprint,
            "part_ids": normalized,
            "provider": provider,
            "model": model,
        },
    )
```

在 runtime replay 的 `_apply_event()` 早返回分支中加入：

```python
if event.type == "provider_projection_consumed":
    part_ids = event.payload.get("part_ids")
    if isinstance(part_ids, list):
        state.consumed_tool_result_part_ids.update(
            str(part_id) for part_id in part_ids if isinstance(part_id, str) and part_id
        )
    return
```

在 `AgentSession` 增加原子运行时更新入口：

```python
def record_provider_projection_consumed(
    self,
    *,
    request_id: str,
    projection_fingerprint: str,
    part_ids: tuple[str, ...],
    provider: str,
    model: str,
) -> None:
    new_ids = sorted(set(part_ids) - self.runtime_state.consumed_tool_result_part_ids)
    if not new_ids:
        return
    self.writer.append_provider_projection_consumed(
        request_id=request_id,
        projection_fingerprint=projection_fingerprint,
        part_ids=new_ids,
        provider=provider,
        model=model,
    )
    self.runtime_state.consumed_tool_result_part_ids.update(new_ids)
```

- [ ] **Step 4: 写 session 去重测试**

在 `tests/test_context_writer.py` 增加：

```python
def test_session_records_only_new_consumed_part_ids(tmp_path) -> None:
    session = AgentSession.create(
        store=JsonlSessionStore(tmp_path),
        session_id="sess_test",
        agents_md="",
    )

    for request_id in ("req_1", "req_2"):
        session.record_provider_projection_consumed(
            request_id=request_id,
            projection_fingerprint=f"fp_{request_id}",
            part_ids=("part_a", "part_b"),
            provider="fake",
            model="fake-model",
        )

    events = [
        event for event in session.store.list_events("sess_test")
        if event.type == "provider_projection_consumed"
    ]
    assert len(events) == 1
    assert session.runtime_state.consumed_tool_result_part_ids == {"part_a", "part_b"}
```

- [ ] **Step 5: 运行 Task 3 测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_writer.py \
  tests/test_context_runtime_replay.py tests/test_context_runtime_state.py -q
```

Expected: PASS。

- [ ] **Step 6: 提交 Task 3**

```bash
git add firstcoder/context/identity.py firstcoder/context/runtime_state.py \
  firstcoder/context/runtime_replay.py firstcoder/context/writer.py \
  firstcoder/agent/session.py tests/test_context_writer.py \
  tests/test_context_runtime_replay.py tests/test_context_runtime_state.py
git commit -m "Record consumed tool results"
```

### Task 4: L1-L3 使用统一估算并保护未消费结果

**Files:**
- Modify: `firstcoder/context/compaction.py:36-47,90-216,288-398,520-710`
- Test: `tests/test_context_compaction_pipeline.py`

- [ ] **Step 1: 写 provider-facing 估算失败测试**

在 `tests/test_context_compaction_pipeline.py` 增加：

```python
def test_pipeline_uses_request_estimator_instead_of_raw_ledger(tmp_path) -> None:
    view = SessionView(
        session_id="sess_test",
        messages=[_message("msg_old", "x" * 40_000), _message("msg_tail", "tail")],
        checkpoints=[
            Checkpoint(
                id="ckpt_1",
                session_id="sess_test",
                summary="short",
                tail_start_message_id="msg_tail",
                covered_until_message_id="msg_old",
                source_fingerprint="fp_1",
            )
        ],
    )
    estimates = []

    result = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash=None,
            target_tokens=100,
            current_turn=1,
            estimate_tokens=lambda candidate: estimates.append(candidate) or 5,
            consumed_tool_result_part_ids=frozenset(),
        )
    )

    assert result.event.noop is True
    assert result.event.before_tokens == 5
    assert estimates
```

- [ ] **Step 2: 写 consumed 门槛失败测试**

在 `tests/test_context_compaction_pipeline.py` 增加合法 transaction helper：

```python
def _derived_tool_result_view(*, content: str) -> tuple[SessionView, MessagePart]:
    result_message = _tool_result(
        "consumption_guard",
        "shell",
        content=content,
        data={"command": "pytest -q", "exit_code": 1},
    )
    return (
        SessionView(
            session_id="sess_test",
            messages=[
                _tool_call("consumption_guard", "shell", {"command": "pytest -q"}),
                result_message,
            ],
        ),
        result_message.parts[0],
    )
```

然后增加：

```python
def test_unconsumed_derived_result_is_not_l2_or_l3_candidate(tmp_path) -> None:
    view, part = _derived_tool_result_view(content="FAILED\n" + "x" * 8_000)

    protected = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash=None,
            target_tokens=1,
            current_turn=10,
            estimate_tokens=lambda candidate: sum(
                estimate_text_tokens(item.content)
                for message in candidate.messages
                for item in message.parts
            ),
            consumed_tool_result_part_ids=frozenset(),
        )
    )
    consumed = CompactionPipeline(root=tmp_path).compact(
        CompactionRequest(
            view=view,
            active_task_hash=None,
            target_tokens=1,
            current_turn=10,
            estimate_tokens=lambda candidate: sum(
                estimate_text_tokens(item.content)
                for message in candidate.messages
                for item in message.parts
            ),
            consumed_tool_result_part_ids=frozenset({part.id}),
        )
    )

    assert protected.event.changed_parts == 0
    assert consumed.event.changed_parts > 0
    assert consumed.event.archive_ids
```

- [ ] **Step 3: 运行新测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_compaction_pipeline.py \
  -k "request_estimator or unconsumed_derived" -q
```

Expected: FAIL，`CompactionRequest` 尚无两个新字段，pipeline 仍统计 raw view。

- [ ] **Step 4: 修改 `CompactionRequest` 和所有阶段估算**

```python
from collections.abc import Callable


@dataclass(slots=True)
class CompactionRequest:
    view: SessionView
    active_task_hash: str | None
    target_tokens: int
    current_turn: int
    estimate_tokens: Callable[[SessionView], int]
    consumed_tool_result_part_ids: frozenset[str]
    enabled_levels: tuple[CompactionLevel, ...] = ("l1", "l2", "l3")
    required_levels: tuple[CompactionLevel, ...] = ()
    l2_result_target_tokens: int | None = None
    force_route_current_text: bool = False
    force_old_task_compaction: bool = False
```

在 `compact()` 中把三处 `_estimate_view_tokens(view)` 改成：

```python
before_tokens = request.estimate_tokens(view)
```

```python
before_level_tokens = request.estimate_tokens(view)
# apply level
after_level_tokens = request.estimate_tokens(view)
```

```python
after_tokens = request.estimate_tokens(view)
```

删除不再使用的 `_estimate_view_tokens()`，避免未来回退到第二套口径。

- [ ] **Step 5: 在 L2/L3 eligibility 前增加 consumed 门槛**

把 consumed set 传入 `_should_route_compact_l2_part()`、`_has_l3_*()`、`_l3_candidates()` 和 `_can_archive_l3_part()`。最终门槛写成：

```python
def _is_consumed_tool_result(
    part: MessagePart,
    *,
    consumed_tool_result_part_ids: frozenset[str],
) -> bool:
    return part.kind == "tool_result" and part.id in consumed_tool_result_part_ids
```

L2：

```python
if not _is_consumed_tool_result(
    part,
    consumed_tool_result_part_ids=consumed_tool_result_part_ids,
):
    return False
```

L3 使用同样的首个检查，然后才检查 `compaction_state`、retrieval protection 和 lifecycle。不要修改 lifecycle 分类规则。

更新现有 pipeline 测试 helper：期望工具结果可压缩的测试必须显式把对应 part id 放入 `consumed_tool_result_part_ids`；只测试 L1 的场景传空集合。

- [ ] **Step 6: 运行 Task 4 测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_compaction_pipeline.py \
  tests/test_context_store.py tests/test_context_content_router.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交 Task 4**

```bash
git add firstcoder/context/compaction.py tests/test_context_compaction_pipeline.py \
  tests/test_context_store.py tests/test_context_content_router.py
git commit -m "Protect unconsumed compaction inputs"
```

### Task 5: L4 生成未落盘 candidate，并保护未消费 transaction

**Files:**
- Modify: `firstcoder/context/llm_compact.py:52-213,272-307`
- Test: `tests/test_context_llm_compact.py`

- [ ] **Step 1: 写 candidate 不落盘失败测试**

在 `tests/test_context_llm_compact.py` 增加：

```python
def test_l4_generate_candidate_does_not_write_checkpoint(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    state = SessionRuntimeState(session_id="sess_test")
    service = LlmCompactService(
        store=store,
        summarizer=FakeSummarizer(
            [
                LlmCompactSummary(
                    summary="摘要",
                    tail_start_message_id="msg_2",
                    covered_until_message_id="msg_1",
                )
            ]
        ),
    )
    request = LlmCompactRequest(
        view=SessionView(
            session_id="sess_test",
            messages=[_message("msg_1", "旧历史"), _message("msg_2", "tail")],
        ),
        runtime_state=state,
        consumed_tool_result_part_ids=frozenset(),
    )

    candidate = service.generate_candidate(request)

    assert candidate.checkpoint is not None
    assert store.list_events("sess_test") == []
    assert state.latest_checkpoint_id is None

    committed = service.commit_candidate(candidate, runtime_state=state)
    assert committed.id == candidate.checkpoint.id
    assert [event.type for event in store.list_events("sess_test")] == ["checkpoint_created"]
```

- [ ] **Step 2: 写未消费 transaction 边界失败测试**

在同一文件创建合法 assistant call + tool result + recent user helper：

```python
def _tool_transaction_view() -> SessionView:
    call = AgentMessage(
        id="msg_call",
        session_id="sess_test",
        role="assistant",
        parts=[
            MessagePart(
                id="part_call",
                message_id="msg_call",
                kind="tool_call",
                content="",
                metadata={
                    "tool_call_id": "call_1",
                    "tool_name": "shell",
                    "arguments": {"command": "pytest -q"},
                },
            )
        ],
    )
    result = AgentMessage(
        id="msg_tool",
        session_id="sess_test",
        role="tool",
        parts=[
            MessagePart(
                id="part_result",
                message_id="msg_tool",
                kind="tool_result",
                content="3 passed",
                metadata={"tool_call_id": "call_1", "tool_name": "shell", "ok": True},
            )
        ],
    )
    return SessionView(
        session_id="sess_test",
        messages=[call, result, _message("msg_recent", "继续")],
    )
```

然后增加：

```python
def test_l4_candidate_cannot_cover_unconsumed_tool_transaction(tmp_path) -> None:
    view = _tool_transaction_view()
    summarizer = FakeSummarizer(
        [
            LlmCompactSummary(
                summary="摘要",
                tail_start_message_id="msg_recent",
                covered_until_message_id="msg_tool",
            )
        ]
    )

    candidate = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=summarizer,
    ).generate_candidate(
        LlmCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            consumed_tool_result_part_ids=frozenset(),
        )
    )

    assert candidate.checkpoint is None
    assert candidate.event.status == "failed"
    assert candidate.event.failure_reason == "unconsumed_boundary"
```

再增加 consumed 对照：

```python
def test_l4_candidate_can_cover_consumed_tool_transaction(tmp_path) -> None:
    candidate = LlmCompactService(
        store=JsonlSessionStore(tmp_path),
        summarizer=FakeSummarizer(
            [
                LlmCompactSummary(
                    summary="摘要",
                    tail_start_message_id="msg_recent",
                    covered_until_message_id="msg_tool",
                )
            ]
        ),
    ).generate_candidate(
        LlmCompactRequest(
            view=_tool_transaction_view(),
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            consumed_tool_result_part_ids=frozenset({"part_result"}),
        )
    )

    assert candidate.event.status == "success"
    assert candidate.checkpoint is not None
    assert candidate.checkpoint.tail_start_message_id == "msg_recent"
```

- [ ] **Step 3: 运行新 L4 测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_llm_compact.py \
  -k "generate_candidate or unconsumed_tool_transaction" -q
```

Expected: FAIL，candidate API 和 consumed boundary 尚不存在。

- [ ] **Step 4: 定义 candidate API**

```python
@dataclass(frozen=True, slots=True)
class LlmCompactCandidate:
    checkpoint: Checkpoint | None
    event: LlmCompactEvent


@dataclass(slots=True)
class LlmCompactRequest:
    view: SessionView
    runtime_state: SessionRuntimeState
    consumed_tool_result_part_ids: frozenset[str]
    mode: CompactMode = "auto"
    expected_source_fingerprint: str | None = None
    summary_mode: str = "default"
```

把当前 `compact()` 的摘要、retry 和 boundary 逻辑移动到：

```python
def generate_candidate(self, request: LlmCompactRequest) -> LlmCompactCandidate:
    source = _build_l4_source(request.view)
    source_fingerprint = _source_fingerprint(request.view.session_id, source)
    if request.expected_source_fingerprint not in {None, source_fingerprint}:
        raise LlmSourceFingerprintMismatchError(
            "expected_source_fingerprint does not match current L4 source"
        )
    if request.runtime_state.last_compaction_input_fingerprint == source_fingerprint:
        return LlmCompactCandidate(
            checkpoint=None,
            event=LlmCompactEvent(
                status="skipped",
                source_fingerprint=source_fingerprint,
                failure_reason="duplicate_source",
            ),
        )
    if request.mode == "auto" and auto_compact_circuit_is_open(request.runtime_state):
        return LlmCompactCandidate(
            checkpoint=None,
            event=LlmCompactEvent(
                status="skipped",
                source_fingerprint=source_fingerprint,
                failure_reason="circuit_open",
            ),
        )

    attempts = 0
    retries = 0
    while True:
        attempts += 1
        try:
            summary = _summarize(
                self.summarizer,
                source.messages,
                summary_mode=request.summary_mode,
            )
            _validate_summary_boundary(
                summary,
                source=source,
                consumed_tool_result_part_ids=request.consumed_tool_result_part_ids,
            )
            checkpoint = _candidate_checkpoint(
                request.view,
                summary=summary,
                source=source,
                source_fingerprint=source_fingerprint,
                retry_count=retries,
            )
            return LlmCompactCandidate(
                checkpoint=checkpoint,
                event=LlmCompactEvent(
                    status="success",
                    source_fingerprint=source_fingerprint,
                    retry_count=retries,
                    checkpoint_id=checkpoint.id,
                ),
            )
        except UnconsumedLlmCheckpointBoundaryError:
            return _failed_candidate(source_fingerprint, retries, "unconsumed_boundary")
        except InvalidLlmCheckpointBoundaryError:
            return _failed_candidate(source_fingerprint, retries, "invalid_tool_sequence")
        except (PromptTooLongError, CompactTimeoutError, NoSummaryError) as error:
            reason = _failure_reason(error)
            decision = self.retry_policy.decide(reason, attempt=attempts)
            if not decision.should_retry:
                return _failed_candidate(source_fingerprint, retries, reason)
            retries += 1
```

成功 checkpoint 必须设置递增 sequence：

```python
sequence=max((checkpoint.sequence for checkpoint in request.view.checkpoints), default=0) + 1,
```

提交方法只负责 durable write 和 runtime success：

```python
def commit_candidate(
    self,
    candidate: LlmCompactCandidate,
    *,
    runtime_state: SessionRuntimeState,
) -> Checkpoint:
    checkpoint = candidate.checkpoint
    if checkpoint is None or candidate.event.status != "success":
        raise ValueError("only successful L4 candidates can be committed")
    self.store.append_event(
        SessionEvent(
            id=new_event_id(),
            session_id=checkpoint.session_id,
            type="checkpoint_created",
            payload=checkpoint.to_dict(),
        )
    )
    runtime_state.latest_checkpoint_id = checkpoint.id
    runtime_state.last_compaction_input_fingerprint = candidate.event.source_fingerprint
    return checkpoint
```

`generate_candidate()` 的所有返回路径都不得调用 `record_auto_compact_success()`、`record_auto_compact_failure()` 或修改 `latest_checkpoint_id`；manager 在 candidate 最终验收后统一维护熔断状态。两个纯 helper 为：

```python
def _candidate_checkpoint(
    view: SessionView,
    *,
    summary: LlmCompactSummary,
    source: L4Source,
    source_fingerprint: str,
    retry_count: int,
) -> Checkpoint:
    return Checkpoint(
        id="",
        session_id=view.session_id,
        summary=summary.summary,
        tail_start_message_id=summary.tail_start_message_id,
        covered_until_message_id=summary.covered_until_message_id,
        source_fingerprint=source_fingerprint,
        sequence=max(
            (checkpoint.sequence for checkpoint in view.checkpoints),
            default=0,
        )
        + 1,
        metadata={
            "created_by": "l4_llm_compact",
            "summary_prompt_scope": "conversation_history_only",
            "retry_count": retry_count,
            "base_checkpoint_id": source.base_checkpoint_id,
            "source_message_ids": [message.id for message in source.messages],
        },
    )


def _failed_candidate(
    source_fingerprint: str,
    retry_count: int,
    failure_reason: str,
) -> LlmCompactCandidate:
    return LlmCompactCandidate(
        checkpoint=None,
        event=LlmCompactEvent(
            status="failed",
            source_fingerprint=source_fingerprint,
            retry_count=retry_count,
            failure_reason=failure_reason,
        ),
    )
```

删除旧的 public `compact()`，一次性更新测试和 manager fake；不要保留“调用 compact 就立即落盘”的兼容分支。

- [ ] **Step 5: 实现未消费边界校验**

扩展 `_validate_summary_boundary()`：

```python
def _validate_summary_boundary(
    summary: LlmCompactSummary,
    *,
    source: L4Source,
    consumed_tool_result_part_ids: frozenset[str],
) -> None:
    _validate_summary_ids_and_tool_sequence(summary, source=source)
    earliest_protected = _earliest_unconsumed_transaction_index(
        _source_tail_messages(source),
        consumed_tool_result_part_ids=consumed_tool_result_part_ids,
    )
    tail_order = {message_id: index for index, message_id in enumerate(source.tail_message_ids)}
    if earliest_protected is not None and tail_order[summary.tail_start_message_id] > earliest_protected:
        raise UnconsumedLlmCheckpointBoundaryError(
            "checkpoint tail would cover an unconsumed tool transaction"
        )
```

`_validate_summary_ids_and_tool_sequence()` 保留现有三项边界校验，并完整写成：

```python
def _validate_summary_ids_and_tool_sequence(
    summary: LlmCompactSummary,
    *,
    source: L4Source,
) -> None:
    valid_ids = (
        {message.id for message in source.messages}
        if source.base_checkpoint_id is None
        else set(source.tail_message_ids)
    )
    if summary.tail_start_message_id not in valid_ids:
        raise InvalidLlmCheckpointBoundaryError("tail_start_message_id must stay within current L4 input tail")
    if summary.covered_until_message_id not in valid_ids:
        raise InvalidLlmCheckpointBoundaryError("covered_until_message_id must stay within current L4 input tail")
    tail_order = {message_id: index for index, message_id in enumerate(source.tail_message_ids)}
    if tail_order[summary.covered_until_message_id] >= tail_order[summary.tail_start_message_id]:
        raise InvalidLlmCheckpointBoundaryError("covered_until_message_id must be before tail_start_message_id")
    tail_messages = _source_tail_messages(source)
    try:
        validate_tool_call_sequence(tail_messages[tail_order[summary.tail_start_message_id] :])
    except InvalidToolCallSequenceError as error:
        raise InvalidLlmCheckpointBoundaryError(
            "checkpoint tail would break assistant tool_call/tool_result sequence"
        ) from error
```

在 `InvalidLlmCheckpointBoundaryError` 后增加专用子类，避免依赖异常文案判断原因：

```python
class UnconsumedLlmCheckpointBoundaryError(InvalidLlmCheckpointBoundaryError):
    """L4 boundary would hide a tool result before its first successful projection."""
```

`_earliest_unconsumed_transaction_index()` 必须通过 tool result 的 `tool_call_id` 回找 assistant message 中匹配的 tool call，返回该 assistant message 在 tail 的索引；缺少匹配时 fail closed，把 tool result 自身索引当作保护起点。实现时增加下面的纯函数，并为缺少匹配 assistant 的情况写一条 fail-closed 测试：

```python
def _earliest_unconsumed_transaction_index(
    messages: list[AgentMessage],
    *,
    consumed_tool_result_part_ids: frozenset[str],
) -> int | None:
    assistant_by_call_id = {
        str(part.metadata.get("tool_call_id")): index
        for index, message in enumerate(messages)
        if message.role == "assistant"
        for part in message.parts
        if part.kind == "tool_call" and part.metadata.get("tool_call_id")
    }
    earliest: int | None = None
    for index, message in enumerate(messages):
        if message.role != "tool":
            continue
        for part in message.parts:
            if part.kind != "tool_result" or part.id in consumed_tool_result_part_ids:
                continue
            start = assistant_by_call_id.get(str(part.metadata.get("tool_call_id")), index)
            earliest = start if earliest is None else min(earliest, start)
    return earliest
```

- [ ] **Step 6: 更新现有 L4 测试并运行**

所有原先调用 `.compact()` 的成功测试改成：

```python
candidate = service.generate_candidate(request)
checkpoint = service.commit_candidate(candidate, runtime_state=state)
```

只验证生成/失败的测试只调用 `generate_candidate()`。所有 `LlmCompactRequest` 显式传 `consumed_tool_result_part_ids`。

Run:

```bash
.venv/bin/python -m pytest tests/test_context_llm_compact.py \
  tests/test_context_checkpoint.py tests/test_context_resume.py -q
```

Expected: PASS。

- [ ] **Step 7: 提交 Task 5**

```bash
git add firstcoder/context/llm_compact.py tests/test_context_llm_compact.py \
  tests/test_context_checkpoint.py tests/test_context_resume.py
git commit -m "Validate L4 candidates before commit"
```

### Task 6: ContextWindowManager 使用动态高低水位并验收 L4 candidate

**Files:**
- Modify: `firstcoder/context/manager.py:41-341,343-565`
- Modify: `firstcoder/context/triggers.py:12-40,61-86`
- Test: `tests/test_context_window_manager.py`

- [ ] **Step 1: 写动态 manager 失败测试**

在 `tests/test_context_window_manager.py` 增加 helper：

```python
def _budget(*, input_tokens: int, fixed_tokens: int = 10) -> ContextBudget:
    return ContextBudget(
        context_window=32_768,
        output_reserve=4_096,
        input_capacity=27_033,
        fixed_tokens=fixed_tokens,
        history_tokens=max(0, input_tokens - fixed_tokens),
        input_tokens=input_tokens,
        high_watermark=100,
        low_watermark=60,
        source="configured",
    )
```

增加：

```python
def test_manager_uses_high_watermark_for_auto_and_low_for_target(tmp_path) -> None:
    view = _view(_message("msg_1", "content"))
    pipeline = FakePipeline(_programmatic_result(view, before_tokens=101, after_tokens=50))
    manager = ContextWindowManager(
        store=JsonlSessionStore(tmp_path),
        pipeline=pipeline,
        l4_service=None,
    )

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=view,
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
            current_turn=1,
            budget=_budget(input_tokens=101),
            estimate_budget=lambda candidate: _budget(input_tokens=50),
        )
    )

    assert result.status == "success"
    assert pipeline.calls[0].target_tokens == 60


def test_manager_fails_without_l4_when_fixed_context_exceeds_low_watermark(tmp_path) -> None:
    manager = ContextWindowManager(store=JsonlSessionStore(tmp_path), pipeline=FakePipeline([]))

    result = manager.compact_if_needed(
        ContextCompactRequest(
            view=SessionView(session_id="sess_test"),
            runtime_state=SessionRuntimeState(session_id="sess_test"),
            trigger=ContextWindowTrigger.AUTO,
            current_turn=0,
            budget=_budget(input_tokens=120, fixed_tokens=70),
            estimate_budget=lambda candidate: _budget(input_tokens=120, fixed_tokens=70),
        )
    )

    assert result.status == "failed"
    assert result.reason == "fixed_context_over_budget"
    assert manager.pipeline.calls == []
```

- [ ] **Step 2: 写 L4 不合格不提交测试**

在测试文件中用下面的 fake 替换旧 `FakeL4`/`WritingFakeL4` 的即时写入语义：

```python
class FakeCandidateL4:
    def __init__(self, store: JsonlSessionStore, candidate: LlmCompactCandidate) -> None:
        self.store = store
        self.candidate = candidate
        self.generate_calls: list[LlmCompactRequest] = []
        self.commit_calls: list[LlmCompactCandidate] = []

    def generate_candidate(self, request: LlmCompactRequest) -> LlmCompactCandidate:
        self.generate_calls.append(request)
        return self.candidate

    def commit_candidate(
        self,
        candidate: LlmCompactCandidate,
        *,
        runtime_state: SessionRuntimeState,
    ) -> Checkpoint:
        self.commit_calls.append(candidate)
        assert candidate.checkpoint is not None
        self.store.append_event(
            SessionEvent(
                id="evt_l4",
                session_id=candidate.checkpoint.session_id,
                type="checkpoint_created",
                payload=candidate.checkpoint.to_dict(),
            )
        )
        runtime_state.latest_checkpoint_id = candidate.checkpoint.id
        return candidate.checkpoint


def _candidate(*, tail_start_message_id: str, covered_until_message_id: str) -> LlmCompactCandidate:
    checkpoint = Checkpoint(
        id="ckpt_test",
        session_id="sess_test",
        summary="L4 摘要",
        tail_start_message_id=tail_start_message_id,
        covered_until_message_id=covered_until_message_id,
        source_fingerprint="fp_l4",
    )
    return LlmCompactCandidate(
        checkpoint=checkpoint,
        event=LlmCompactEvent(
            status="success",
            source_fingerprint="fp_l4",
            checkpoint_id=checkpoint.id,
        ),
    )
```

把现有 `test_manager_reports_still_over_budget_after_successful_l4_checkpoint` 改成 candidate budget 始终为 100，并断言：

```python
assert result.status == "failed"
assert result.reason == "still_over_budget"
assert fake_l4.commit_calls == []
assert [event.type for event in store.list_events("sess_test")] == [
    "compaction_completed",
    "llm_compaction_completed",
]
```

再增加合格对照：candidate 预算为 50 时 `commit_candidate()` 恰好调用一次，重建后的 view 含 `ckpt_test`。两条测试都使用 `target_tokens=60`；不合格场景断言 candidate 的 100 **不低于**目标，合格场景断言 50 低于目标。

再增加窗口无解测试：`budget.input_tokens > budget.input_capacity` 且 L4 candidate 返回 `failure_reason="unconsumed_boundary"` 时，manager 不提交 checkpoint，结果 `reason` 为 `unconsumed_result_over_budget`，事件中的 `final_failure_reason` 也使用该值；这条路径供 PROMPT_TOO_LONG 和普通 AUTO 共用。

- [ ] **Step 3: 运行 manager 新测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_window_manager.py \
  -k "high_watermark or fixed_context or still_over_budget" -q
```

Expected: FAIL，request 尚未携带 budget，manager 仍调用旧 L4 `.compact()`。

- [ ] **Step 4: 修改 manager request/protocol**

```python
from firstcoder.context.checkpoint import Checkpoint
from firstcoder.context.context_builder import InvalidCheckpointBoundaryError
from firstcoder.context.models import AgentMessage
from firstcoder.context.llm_compact import LlmCompactCandidate
from firstcoder.context.tool_sequence import InvalidToolCallSequenceError
from firstcoder.context.token_budget import ContextBudget


@dataclass(slots=True)
class ContextCompactRequest:
    view: SessionView
    runtime_state: SessionRuntimeState
    budget: ContextBudget
    estimate_budget: Callable[[SessionView], ContextBudget]
    trigger: ContextWindowTrigger | str = ContextWindowTrigger.AUTO
    mode: ContextCompactMode | str = ContextCompactMode.AUTO
    current_turn: int = 0
    target_tokens: int | None = None
```

L4 protocol 改为：

```python
class L4Compactor(Protocol):
    def generate_candidate(self, request: LlmCompactRequest) -> LlmCompactCandidate: ...
    def commit_candidate(
        self,
        candidate: LlmCompactCandidate,
        *,
        runtime_state: SessionRuntimeState,
    ) -> Checkpoint: ...
```

AUTO 判断：

```python
if trigger == ContextWindowTrigger.AUTO and request.budget.input_tokens < request.budget.high_watermark:
    return ContextCompactResult(
        status="skipped",
        reason="under_threshold",
        view=request.view,
        before_tokens=request.budget.input_tokens,
        after_tokens=request.budget.input_tokens,
    )
```

目标：

```python
target_tokens = request.target_tokens or request.budget.low_watermark
if trigger == ContextWindowTrigger.TASK_HASH_CHANGED and request.target_tokens is None:
    target_tokens = max(1, request.budget.low_watermark * 2 // 3)
```

pipeline request 必须传：

```python
estimate_tokens=lambda candidate: request.estimate_budget(candidate).input_tokens,
consumed_tool_result_part_ids=frozenset(
    request.runtime_state.consumed_tool_result_part_ids
),
```

删除 manager 对 `evaluate_context_triggers()` 和固定 `auto_compact_threshold/target_tokens` 的主路径依赖；`ContextCompactionConfig` 只保留 L2/L3 内容阈值、tail heuristic 和 circuit breaker 所需字段。普通 token 触发只看 budget high watermark。

- [ ] **Step 5: 实现 fixed failure 和 candidate 验收**

在任何 pipeline/L4 调用前：

```python
if request.budget.fixed_tokens >= request.budget.low_watermark:
    return ContextCompactResult(
        status="failed",
        reason="fixed_context_over_budget",
        view=request.view,
        before_tokens=request.budget.input_tokens,
        after_tokens=request.budget.input_tokens,
        final_failure_reason="fixed_context_over_budget",
    )
```

L4 success candidate 的验收：

```python
candidate = self.l4_service.generate_candidate(
    LlmCompactRequest(
        view=programmatic.view,
        runtime_state=request.runtime_state,
        consumed_tool_result_part_ids=frozenset(
            request.runtime_state.consumed_tool_result_part_ids
        ),
        mode=mode.value,
    )
)
if candidate.event.status != "success":
    failure_reason = candidate.event.failure_reason or candidate.event.status
    if (
        failure_reason == "unconsumed_boundary"
        and request.budget.input_tokens > request.budget.input_capacity
    ):
        failure_reason = "unconsumed_result_over_budget"
    failed_event = replace(candidate.event, final_failure_reason=failure_reason)
    self._record_l4_event(
        session_id=request.view.session_id,
        trigger=trigger,
        target_tokens=target_tokens,
        event=failed_event,
    )
    self._record_auto_failure_if_needed(
        request=request,
        mode=mode,
        before_failure_count=auto_failure_count_before,
        failure_reason=failure_reason,
    )
    return ContextCompactResult(
        status="failed",
        reason=failure_reason,
        view=programmatic.view,
        before_tokens=request.budget.input_tokens,
        after_tokens=request.estimate_budget(programmatic.view).input_tokens,
        programmatic_event=programmatic.event,
        l4_event=failed_event,
        final_failure_reason=failure_reason,
    )
if candidate.event.status == "success" and candidate.checkpoint is not None:
    candidate_view = _view_with_checkpoint(programmatic.view, candidate.checkpoint)
    try:
        candidate_budget = request.estimate_budget(candidate_view)
    except (InvalidCheckpointBoundaryError, InvalidToolCallSequenceError):
        failed_event = replace(
            candidate.event,
            status="failed",
            failure_reason="invalid_tool_sequence",
            checkpoint_id=None,
        )
        self._record_l4_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=failed_event,
        )
        self._record_auto_failure_if_needed(
            request=request,
            mode=mode,
            before_failure_count=auto_failure_count_before,
            failure_reason="invalid_tool_sequence",
        )
        return ContextCompactResult(
            status="failed",
            reason="invalid_tool_sequence",
            view=programmatic.view,
            before_tokens=request.budget.input_tokens,
            after_tokens=request.estimate_budget(programmatic.view).input_tokens,
            programmatic_event=programmatic.event,
            l4_event=failed_event,
            final_failure_reason="invalid_tool_sequence",
        )
    if candidate_budget.input_tokens >= target_tokens:
        failed_event = replace(
            candidate.event,
            status="failed",
            failure_reason="still_over_budget",
            checkpoint_id=None,
        )
        self._record_l4_event(
            session_id=request.view.session_id,
            trigger=trigger,
            target_tokens=target_tokens,
            event=failed_event,
        )
        self._record_auto_failure_if_needed(
            request=request,
            mode=mode,
            before_failure_count=auto_failure_count_before,
            failure_reason="still_over_budget",
        )
        return ContextCompactResult(
            status="failed",
            reason="still_over_budget",
            view=programmatic.view,
            before_tokens=request.budget.input_tokens,
            after_tokens=candidate_budget.input_tokens,
            programmatic_event=programmatic.event,
            l4_event=failed_event,
            final_failure_reason="still_over_budget",
        )
    self.l4_service.commit_candidate(candidate, runtime_state=request.runtime_state)
    self._record_l4_event(
        session_id=request.view.session_id,
        trigger=trigger,
        target_tokens=target_tokens,
        event=candidate.event,
    )
    rebuilt_view = self.store.rebuild_session_view(request.view.session_id)
    rebuilt_budget = request.estimate_budget(rebuilt_view)
    self._record_auto_success_if_needed(request=request, mode=mode)
    return ContextCompactResult(
        status="success",
        reason=_result_reason(trigger=trigger, auto_reason="over_threshold"),
        view=rebuilt_view,
        before_tokens=request.budget.input_tokens,
        after_tokens=rebuilt_budget.input_tokens,
        programmatic_event=programmatic.event,
        l4_event=candidate.event,
    )
```

`_view_with_checkpoint()` 必须深拷贝 messages 和 checkpoint list，不能修改调用者 view：

```python
def _view_with_checkpoint(view: SessionView, checkpoint: Checkpoint) -> SessionView:
    return SessionView(
        session_id=view.session_id,
        messages=[AgentMessage.from_dict(message.to_dict()) for message in view.messages],
        checkpoints=[*view.checkpoints, Checkpoint.from_dict(checkpoint.to_dict())],
        metadata=dict(view.metadata),
        task_plan=view.task_plan,
    )
```

把上面的“估算 → 校验 `< target_tokens` → commit → 记录事件”主体提成 manager 私有方法，普通 L4、默认 fallback 和 stronger fallback 都只调用该方法，禁止保留任何先 commit 再估算的分支。

所有“达到目标”的判断统一使用 `input_tokens < target_tokens`；恰好等于低水位仍视为未达标，以满足设计稿“压到低水位以下”的验收语义。

candidate 已明确返回 `unconsumed_boundary` 且当前 `request.budget.input_tokens > request.budget.input_capacity` 时，把最终失败原因映射为 `unconsumed_result_over_budget`；否则保留 `unconsumed_boundary`，不能把保护失败伪装成普通 `still_over_budget`。

- [ ] **Step 6: 更新 manager 现有测试 fake 和 request helper**

将 `FakeL4.compact()` 改为 `generate_candidate()`/`commit_candidate()`；统一测试 helper 为每个 `ContextCompactRequest` 提供 `budget` 和 `estimate_budget`。删除对固定 32K/24K 的断言，保留大工具结果、manual、task switch、prompt-too-long、fallback 和 circuit breaker 行为断言。

同时收紧 `firstcoder/context/triggers.py`：`ContextCompactionConfig` 删除 `auto_compact_threshold`、`target_tokens`、`blocking_target_tokens`、`task_switch_target_tokens`、`max_tail_tokens` 和 `reserved_output_tokens`；只保留 L2/L3 单结果阈值、tail heuristic 和 preview 配置。`evaluate_context_triggers()` 改为只接收调用方传入的 `input_tokens`、`high_watermark`、`low_watermark`，不自行估算或读取固定 token 常量；所有触发测试改用 `_budget(input_tokens=...)`，确保普通 AUTO 的唯一 token 触发来自 `ContextBudget.high_watermark`。

`max_tail_messages` 只用于 `/context` 诊断字段和 manual/task-boundary 的附加信息，不能让普通 AUTO 在主请求前额外触发。

- [ ] **Step 7: 运行 Task 6 测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_window_manager.py \
  tests/test_context_triggers.py tests/test_context_retry_policy.py \
  tests/test_context_circuit_breaker.py -q
```

Expected: PASS。

- [ ] **Step 8: 提交 Task 6**

```bash
git add firstcoder/context/manager.py firstcoder/context/triggers.py \
  tests/test_context_window_manager.py tests/test_context_triggers.py \
  tests/test_context_retry_policy.py tests/test_context_circuit_breaker.py
git commit -m "Drive compaction with dynamic budgets"
```

### Task 7: AgentLoop 收敛到主请求前 AUTO，并在成功后记录消费

**Files:**
- Modify: `firstcoder/agent/loop.py:194-211,277-292,446-450,501-626,738-823,872-947`
- Modify: `firstcoder/context/context_builder.py:30-79`
- Test: `tests/test_agent_context_loop.py`
- Test: `tests/test_context_builder_new.py`
- Test: `tests/test_model_request_options.py`

- [ ] **Step 1: 写触发时机失败测试**

在 `tests/test_agent_context_loop.py` 替换旧的 `test_agent_loop_runs_auto_compact_after_large_tool_result`，增加：

```python
def _loop(tmp_path, *, provider, context_manager, tools):
    store = JsonlSessionStore(tmp_path / "session")
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    return AgentLoop(
        session=session,
        provider=provider,
        context_manager=context_manager,
        tools=tools,
    )
```

```python
def test_agent_loop_runs_auto_once_before_each_main_provider_request(tmp_path) -> None:
    manager = RecordingContextManager()
    provider = FakeProvider(
        responses=[
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "hello"})],
                finish_reason="tool_calls",
            ),
            ChatResponse(provider="fake", model="fake-model", content="done"),
        ]
    )
    loop = _loop(tmp_path, provider=provider, context_manager=manager, tools=[_echo_tool()])

    loop.run_user_turn("run")

    auto_calls = [call for call in manager.calls if call.trigger == ContextWindowTrigger.AUTO]
    assert len(auto_calls) == 2
    assert len(provider.requests) == 2
```

增加最终回复不再触发额外 AUTO：上述断言本身会捕获第三次调用。再增加 permission resume 场景，断言 tool result 落盘时不立即 AUTO，恢复后的下一次 provider request 前才增加一次。

- [ ] **Step 2: 写消费成功/失败测试**

同步成功：

```python
def test_sync_main_request_records_projected_tool_result_as_consumed(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path / "session")
    session = AgentSession.create(store=store, session_id="sess_test", agents_md="")
    session.append_user_message("继续")
    tool_call = ToolCall(id="call_existing", name="echo", arguments={"text": "hello"})
    session.append_assistant_response(
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="",
            tool_calls=[tool_call],
            finish_reason="tool_calls",
        )
    )
    session.append_tool_result(
        tool_call=tool_call,
        result=ToolResult(name="echo", ok=True, content="echo:hello"),
    )
    provider = FakeProvider([ChatResponse(provider="fake", model="fake-model", content="ok")])
    loop = AgentLoop(session=session, provider=provider, tools=[_echo_tool()])

    loop._complete_once()

    tool_part = next(
        part for message in session.rebuild_view().messages
        for part in message.parts if part.kind == "tool_result"
    )
    assert tool_part.id in session.runtime_state.consumed_tool_result_part_ids
    assert [
        event.type for event in session.store.list_events(session.session_id)
    ].count("provider_projection_consumed") == 1
```

同步失败：让 provider 抛 `ProviderError(API_ERROR, ...)`，断言集合为空且没有消费事件。

再用同一个 prepared request 覆盖 `ProviderError(TIMEOUT, ...)` 和 `AgentCancelledError`；两者都必须保持集合为空且没有消费事件。streaming 分别覆盖 `message_completed` 成功和只有部分 delta 后报错；只有前者写事件。对每个失败 fake 都断言 `provider_projection_consumed` 事件数量为 0。

- [ ] **Step 3: 运行 AgentLoop 新测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py \
  -k "auto_once_before or records_projected or partial_stream" -q
```

Expected: FAIL，AUTO 仍在旧调用点，成功响应尚未记录消费。

- [ ] **Step 4: 定义 prepared request 数据**

在 `agent/loop.py` 增加：

```python
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PreparedMainRequest:
    request: ChatRequest
    request_id: str
    projection_fingerprint: str
    tool_result_part_ids: tuple[str, ...]
```

先在 `ContextBuilder` 增加与 provider 投影共用 tail/collapse/sequence 校验的只读方法，避免 AgentLoop 另写一套 checkpoint 边界判断：

```python
def projected_tool_result_part_ids(self, view: SessionView) -> tuple[str, ...]:
    checkpoint = CheckpointIndex(view.checkpoints).latest()
    tail = _collapse_identical_adjacent_duplicate_tool_calls(
        self._tail_messages(view, checkpoint=checkpoint)
    )
    validate_tool_call_sequence(tail)
    return tuple(
        part.id
        for message in tail
        if message.role == "tool"
        for part in message.parts
        if part.kind == "tool_result"
    )
```

在 `tests/test_context_builder_new.py` 验证 checkpoint 覆盖的旧 tool result 不在返回集合中，当前 tail 的两个结果按原始顺序返回；`PreparedMainRequest` 只调用这个方法，不扫描 raw history。

增加 budget helper：

```python
def _context_budget_for_view(
    self,
    view: SessionView,
    *,
    runtime_instruction: str | None,
    definitions: list[ToolDefinition],
) -> ContextBudget:
    system_prefix = self.session.build_system_prefix(
        provider_name=self.provider.name,
        provider_model=self.provider.model,
        provider_capabilities=getattr(self.provider, "capabilities", None),
    )
    if runtime_instruction:
        system_prefix = [
            *system_prefix,
            ChatMessage(role="system", content=runtime_instruction),
        ]
    messages = self.context_builder.build_provider_messages(
        view,
        system_prefix=system_prefix,
        store_root=self.session.store.root,
    )
    return build_context_budget(
        messages=messages,
        tools=definitions,
        context_window=self.context_window,
        max_output_tokens=self.request_options.max_tokens,
    )


def context_budget_for_view(self, view: SessionView) -> ContextBudget:
    return self._context_budget_for_view(
        view,
        runtime_instruction=None,
        definitions=self._provider_tool_definitions(),
    )
```

- [ ] **Step 5: 实现主请求准备入口**

```python
def _prepare_main_provider_request(
    self,
    *,
    tool_choice="auto",
    runtime_instruction: str | None = None,
) -> PreparedMainRequest:
    self._repair_interrupted_tool_calls_before_provider_request()
    self._check_cancelled()
    self._append_pending_guidance()
    self._append_background_notifications()
    definitions = self._provider_tool_definitions()
    view = self.session.rebuild_view()
    budget = self._context_budget_for_view(
        view,
        runtime_instruction=runtime_instruction,
        definitions=definitions,
    )
    if self.context_manager is not None:
        result = self.context_manager.compact_if_needed(
            ContextCompactRequest(
                view=view,
                runtime_state=self.session.runtime_state,
                trigger=ContextWindowTrigger.AUTO,
                current_turn=self.session.current_turn,
                budget=budget,
                estimate_budget=lambda candidate: self._context_budget_for_view(
                    candidate,
                    runtime_instruction=runtime_instruction,
                    definitions=definitions,
                ),
            )
        )
        if result.status == "success":
            view = self.session.rebuild_view()
    messages = self._build_provider_messages(
        view,
        system_prefix=system_prefix,
    )
    request = self._main_chat_request(messages, definitions, tool_choice)
part_ids = self.context_builder.projected_tool_result_part_ids(view)
    return PreparedMainRequest(
        request=request,
        request_id=new_request_id(),
        projection_fingerprint=stable_json_hash(
            {
                "messages": [asdict(message) for message in messages],
                "tools": [asdict(definition) for definition in definitions],
            },
            length=24,
        ),
        tool_result_part_ids=part_ids,
    )
```

`ContextBuilder.projected_tool_result_part_ids()` 使用 latest checkpoint 的 effective tail，只收集实际投影为 `role=tool` 的 `tool_result` part；不能把 checkpoint 已覆盖的原始结果算作已消费。

- [ ] **Step 6: 同步和 streaming 成功后记录消费**

同步：

```python
prepared = self._prepare_main_provider_request(
    tool_choice=tool_choice,
    runtime_instruction=runtime_instruction,
)
self._reserve_provider_call()
self._check_turn_timeout()
self._check_cancelled()
response = self.provider.complete(prepared.request)
self.session.record_provider_projection_consumed(
    request_id=prepared.request_id,
    projection_fingerprint=prepared.projection_fingerprint,
    part_ids=prepared.tool_result_part_ids,
    provider=self.provider.name,
    model=self.provider.model,
)
return response
```

streaming 只在 `final_response is not None` 后执行同一记录；任何 exception、取消或没有 `message_completed` 的路径都不能调用。

- [ ] **Step 7: 删除旧 AUTO 调用点并修正强制 compact**

删除以下调用：

```python
self._auto_compact()
```

具体包括用户消息后、权限恢复 tool result 后、每轮工具执行后和 `_complete_turn()` 最终回复后。删除不再使用的 `_auto_compact()` helper。

保留 `_compact_for_prompt_too_long()`，但让它接收当前 `runtime_instruction` 和 definitions，通过同一 `_context_budget_for_view()` 构造 `ContextCompactRequest`。主请求恢复仍只重试一次。

`TASK_HASH_CHANGED` 强制 compact 后不要紧跟普通 AUTO；下一次主请求统一准备时再正常检查。

`_compact_if_needed()` 仅保留给 `PROMPT_TOO_LONG` 和 `TASK_HASH_CHANGED` 等强制路径：先按当前 view/definitions 构造 `ContextBudget`，再传 `budget`、`estimate_budget` 和 `consumed_tool_result_part_ids`；普通 AUTO 不再从该 helper 调用。删除 `_estimate_provider_request_tokens()`，避免继续把 output reserve 加到 input token 总量或读取已删除的 `ContextCompactionConfig.reserved_output_tokens`。

- [ ] **Step 8: 运行 Task 7 测试**

Run:

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py \
  tests/test_model_request_options.py tests/test_agent_tool_flow.py \
  tests/test_multimodal_input.py -q
```

Expected: PASS。

- [ ] **Step 9: 提交 Task 7**

```bash
git add firstcoder/agent/loop.py tests/test_agent_context_loop.py \
  firstcoder/context/context_builder.py tests/test_context_builder_new.py \
  tests/test_model_request_options.py tests/test_agent_tool_flow.py \
  tests/test_multimodal_input.py
git commit -m "Compact only before main requests"
```

### Task 8: `/context`、`/compact` 与完整回归

**Files:**
- Modify: `firstcoder/context/inspector.py:21-78,91-104`
- Modify: `firstcoder/app/commands.py:22-124`
- Modify: `firstcoder/app/runtime.py:75-114`
- Modify: `firstcoder/app/factory.py:204-240`
- Test: `tests/test_context_inspector.py`
- Test: `tests/test_app_context_commands.py`
- Test: `tests/test_app_factory.py`
- Modify: `tests/test_app_session_commands.py`
- Modify: `tests/test_app_tui.py`
- Modify: `README.md`
- Modify: `docs/CONTEXT_MANAGEMENT_DESIGN.zh-CN.md`
- Modify: `docs/CONTEXT_MANAGEMENT_DESIGN.md`

- [ ] **Step 1: 写 inspector/command 失败测试**

在 `tests/test_context_inspector.py` 增加：

```python
def test_inspector_reports_shared_budget_and_unconsumed_count() -> None:
    runtime = SessionRuntimeState(
        session_id="sess_test",
        consumed_tool_result_part_ids={"part_consumed"},
    )
    view = SessionView(
        session_id="sess_test",
        messages=[
            _message(
                "msg_tool",
                role="tool",
                kind="tool_result",
                content="result",
                metadata={"tool_call_id": "call_1"},
            )
        ],
    )
    budget = ContextBudget(
        context_window=128_000,
        output_reserve=8_192,
        input_capacity=113_408,
        fixed_tokens=18_000,
        history_tokens=42_000,
        input_tokens=60_000,
        high_watermark=102_067,
        low_watermark=81_653,
        source="configured",
    )

    report = ContextInspector().inspect(view, runtime, budget=budget)

    assert report.context_window == 128_000
    assert report.fixed_tokens == 18_000
    assert report.history_tokens == 42_000
    assert report.input_tokens == 60_000
    assert report.unconsumed_tool_result_count == 1
```

在 `tests/test_app_context_commands.py` 更新 `/context` 断言：

```python
assert "Model window: 128000 (configured)" in result.output
assert "Fixed tokens: 18000" in result.output
assert "History tokens: 42000" in result.output
assert "High watermark: 102067" in result.output
assert "Unconsumed tool results: 1" in result.output
```

- [ ] **Step 2: 运行 inspector/command 测试并确认失败**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_inspector.py \
  tests/test_app_context_commands.py -q
```

Expected: FAIL，report 尚无 budget 字段，command 尚未获得共享 budget provider。

- [ ] **Step 3: 扩展 inspector report**

在 `ContextInspectionReport` 增加：

```python
context_window: int
context_window_source: str
output_reserve: int
fixed_tokens: int
history_tokens: int
input_tokens: int
high_watermark: int
low_watermark: int
unconsumed_tool_result_count: int
```

修改入口：

```python
def inspect(
    self,
    view: SessionView,
    runtime: SessionRuntimeState,
    *,
    budget: ContextBudget,
) -> ContextInspectionReport:
```

未消费数量只统计 effective tail 中 `kind == "tool_result"` 且 part id 不在 consumed set 的结果。删除 inspector 自己的 `_estimate_context_tokens()`，`estimated_tokens` 字段改名为 `input_tokens`，避免保留第四套估算。

将该文件其余所有 `ContextInspector().inspect(view, runtime)` 调用补为显式 `budget=...`；测试中用 `build_context_budget(messages=[], tools=[], context_window=128_000, max_output_tokens=8_192)` 生成默认预算，固定/历史拆分断言继续传入测试内构造的 `ContextBudget`，不得恢复旧的 raw-ledger 估算。

- [ ] **Step 4: 给 ContextCommandHandler 注入唯一 budget provider**

```python
class BudgetProvider(Protocol):
    def __call__(self, view: SessionView) -> ContextBudget: ...


@dataclass(slots=True)
class ContextCommandHandler:
    session: SessionLike
    budget_provider: BudgetProvider
    context_manager: ContextManagerLike | None = None
    inspector: ContextInspector = ContextInspector()
```

```python
def _inspect(self) -> ContextInspectionReport:
    view = self.session.rebuild_view()
    return self.inspector.inspect(
        view,
        self.session.runtime_state,
        budget=self.budget_provider(view),
    )
```

同步更新 `tests/test_app_context_commands.py`、`tests/test_app_session_commands.py` 和 `tests/test_app_tui.py` 的 fake handler，全部传入同一个 provider：

```python
def _budget_provider(view: SessionView) -> ContextBudget:
    return build_context_budget(
        messages=[],
        tools=[],
        context_window=32_768,
        max_output_tokens=4_096,
    )
```

`_manual_compact()` 使用同一个 budget 和 estimator：

```python
view = self.session.rebuild_view()
budget = self.budget_provider(view)
result = self.context_manager.compact_if_needed(
    ContextCompactRequest(
        view=view,
        runtime_state=self.session.runtime_state,
        budget=budget,
        estimate_budget=self.budget_provider,
        trigger=ContextWindowTrigger.MANUAL,
        mode="manual",
        current_turn=self.session.current_turn,
        target_tokens=_manual_target_tokens(budget),
    )
)
```

将 `_manual_target_tokens` 改为不可能低于 fixed context 的动态目标：

```python
def _manual_target_tokens(budget: ContextBudget) -> int | None:
    if budget.input_tokens <= 2_000:
        return None
    proposed = min(budget.low_watermark - 1, int(budget.input_tokens * 0.6))
    target = max(budget.fixed_tokens + 1, proposed)
    return target if target < budget.input_tokens else None
```

在 `AgentChatRunner` 增加只读 `context_budget(view)`，内部复用与 AgentLoop 相同的 system prefix、tool definitions 和 `build_context_budget()` helper；不要复制 token 公式。factory 先构造 runner，再构造 handler：

```python
def context_budget(self, view: SessionView) -> ContextBudget:
    loop = self.loops[-1] if self.loops else self._create_loop(CancellationToken())
    return loop.context_budget_for_view(view)
```

`AgentLoop.context_budget_for_view()` 只调用 `_provider_tool_definitions()`、`_context_budget_for_view()`，不 append 消息、不调用 manager、不访问 provider；第一次执行 `/context` 时创建的 loop 只注册现有 session-scoped helper tools，不产生事件或模型请求。

```python
context_handler = ContextCommandHandler(
    session=current,
    context_manager=context_manager,
    budget_provider=chat_runner.context_budget,
)
```

更新两个渲染函数，所有 `estimated_tokens` 访问改成 `input_tokens`，并在 `/context` 输出窗口来源、输出预留、fixed/history、high/low 水位和未消费数量：

```python
lines = [
    f"Session: {report.session_id}",
    f"Model window: {report.context_window} ({report.context_window_source})",
    f"Output reserve: {report.output_reserve}",
    f"Fixed tokens: {report.fixed_tokens}",
    f"History tokens: {report.history_tokens}",
    f"Input tokens: {report.input_tokens}",
    f"High watermark: {report.high_watermark}",
    f"Low watermark: {report.low_watermark}",
    f"Unconsumed tool results: {report.unconsumed_tool_result_count}",
    f"Tail messages: {report.tail_message_count}",
    f"Latest checkpoint: {display_value(report.latest_checkpoint_id)}",
]
```

- [ ] **Step 5: 更新用户文档**

在 README provider model 示例中加入：

```toml
context_window = 128000
```

在中英文 context design 中只更新这些事实：动态高低水位、pre-request AUTO、consumed event、L4 candidate 验收。明确 oversized artifact、MCP schema 暴露和 tokenizer 不在当前实现中，不能把旧大设计重新写回文档。

- [ ] **Step 6: 运行 UI 和专项回归**

Run:

```bash
.venv/bin/python -m pytest tests/test_context_inspector.py \
  tests/test_app_context_commands.py tests/test_app_factory.py \
  tests/test_app_model_commands.py -q
```

Expected: PASS。

Run:

```bash
.venv/bin/python -m pytest tests/test_context_token_budget.py \
  tests/test_context_window_manager.py tests/test_context_compaction_pipeline.py \
  tests/test_context_llm_compact.py tests/test_context_resume.py \
  tests/test_context_archive.py tests/test_context_runtime_replay.py \
  tests/test_agent_context_loop.py -q
```

Expected: PASS。

- [ ] **Step 7: 运行完整测试**

Run:

```bash
.venv/bin/python -m pytest tests
```

Expected: 全部 PASS。若失败，先在改造前基线提交重跑同一失败测试；只有能证明基线同样失败时才能标记为既有问题，并在交付说明中同时给出两个 commit 的命令和结果。

- [ ] **Step 8: 检查压缩旧路径已删除**

Run:

```bash
rg -n "auto_compact_threshold|target_tokens: int = 24_000|_estimate_view_tokens|def _auto_compact" firstcoder tests
```

Expected: 不再出现固定主窗口阈值、raw-ledger estimator 或旧 `_auto_compact` helper；允许出现 L2/L3 单结果阈值和测试名称中的历史描述。

Run:

```bash
rg -n "provider_projection_consumed|generate_candidate|commit_candidate|context_window" firstcoder tests
```

Expected: writer/replay、L4 两阶段、配置和运行时调用链均有命中。

- [ ] **Step 9: 提交 Task 8**

```bash
git add firstcoder/context/inspector.py firstcoder/app/commands.py \
  firstcoder/app/runtime.py firstcoder/app/factory.py \
  tests/test_context_inspector.py tests/test_app_context_commands.py \
  tests/test_app_factory.py README.md docs/CONTEXT_MANAGEMENT_DESIGN.zh-CN.md \
  docs/CONTEXT_MANAGEMENT_DESIGN.md
git commit -m "Expose context budget status"
```

## 最终交付检查

- [ ] `git status --short` 为空。
- [ ] `git log --oneline` 显示 8 个按任务拆分的提交。
- [ ] 记录动态预算三个示例的真实测试输出。
- [ ] 记录消费保护测试中同步、streaming、失败和 resume 四条证据。
- [ ] 记录不合格 L4 candidate 未产生 `checkpoint_created` 的事件列表。
- [ ] 记录 `.venv/bin/python -m pytest tests` 的总通过数和耗时。
- [ ] 交付说明明确：本次没有实现 MCP 按需暴露、oversized artifact、tokenizer 或 task-boundary 重构。
