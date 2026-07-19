# Todo 与统一提示词运行时重构实施计划

> **供智能体执行者使用：** 实施本计划时，必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 技能逐项执行。所有步骤使用复选框（`- [ ]`）跟踪。

**目标：** 将 Todo 改造成轻量、可靠的“模型主动上报进度”视图，删除会打断模型推理的运行时干预，并把模型行为规则统一到一份提示词中，同时保持现有任务执行、会话恢复和多 Provider 行为兼容。

**架构：** 保留现有按 Session 隔离、追加写入的 `todo_updated` 事件，以及“每次提交完整列表”的 Todo 契约。删除周期性 Todo 催促，新增只进入单次模型请求、不会落盘的临时运行时指令，用于结束前最多一次 Todo 对账。测试成功只作为证据返回模型，不再作为 AgentLoop 的强制停止信号。系统提示词负责行为规则，工具 Schema 负责数据格式，运行时负责机械约束，TUI 只展示模型最后一次上报的状态。

**技术栈：** Python 3.11+、pytest、Textual、dataclasses、JSONL 事件溯源、OpenAI-compatible Chat Completions、Anthropic Messages API

---

## 一、范围与安全边界

本计划基于分支 `codex/prewrite-diff-review` 的提交 `8c9f919`。

当前工作区已经存在用户自己的修改和未跟踪文件。实施期间不得暂存、改写、删除或提交以下无关内容：

- `.gitignore`
- `.release-dist/`
- `.release-dist-0.1.6/`
- `docs/superpowers/plans/` 与 `docs/superpowers/specs/` 下除本计划以外的现有未跟踪文件

每次提交前都执行：

```bash
git status --short
```

只暂存当前任务明确列出的文件。

## 二、文件职责划分

- `firstcoder/tools/todo.py`：Todo 的模型可见 Schema、归一化、约束校验与精简工具结果。
- `firstcoder/agent/todo_policy.py`：读取当前任务的 Todo 状态，并生成结束前一次性对账指令。
- `firstcoder/agent/loop.py`：构造 Provider 请求、注入临时运行时指令、控制工具循环与结束语义。
- `firstcoder/agent/loop_limits.py`：只保留轮次、调用次数和时间等安全预算。
- `firstcoder/agent/tool_execution.py`：执行工具并处理权限状态，不再传播“测试成功即可停止”的标志。
- `firstcoder/agent/verification.py`：删除；移除验证强制收尾后不再拥有独立产品职责。
- `firstcoder/context/system_prompt.py`：组装稳定系统前缀，并只加载一份行为提示词。
- `firstcoder/context/prompts/agent_instructions.md`：新增，成为唯一维护的智能体行为提示词。
- `firstcoder/context/prompts/agent_few_shots.md`：删除，其中有效规则合并进 `agent_instructions.md`。
- `firstcoder/tools/descriptions.py`：只保留工具本身的关键语义，不重复完整工作流规则。
- `firstcoder/tools/session_registry.py`：继续向隐藏分类器提供 `task_boundary`，但主模型不可见。
- `firstcoder/app/tui_state.py`：兼容新旧 Todo 数据并维护 TUI 投影。
- `firstcoder/app/activity_view.py`：明确 Todo 是模型上报状态。
- `tests/test_todo.py`：Todo Schema、归一化和机械约束测试。
- `tests/test_agent_context_loop.py`：运行时消息、结束前对账、验证继续执行、同步与流式一致性测试。
- `tests/test_context_system_prompt.py`：单一提示词、去重和冲突消除测试。
- `tests/test_agent_loop_limits.py`：删除语义停止开关后的安全预算测试。
- `tests/test_app_factory.py`：主模型工具可见性测试。
- `tests/test_app_tui.py`：Todo 实时刷新、恢复和兼容渲染测试。
- `tests/test_model_request_options.py`、`tests/test_providers.py`：验证 Provider 无关请求行为保持一致。

## 三、必须保持的行为契约

1. 简单问题和单步操作不强制创建 Todo。
2. 有明确阶段的复杂任务由模型主动使用 Todo。
3. 每次 Todo 调用替换完整当前列表。
4. 新 Todo 数据只包含 `content` 和 `status`；旧数据中的 `priority` 仍可读取。
5. 同一列表最多只能有一个 `in_progress`。
6. 不再存在“执行若干工具后催建或催更 Todo”的周期提醒。
7. 结束前 Todo 对账每个真实用户轮次最多触发一次，并且不能落盘为用户消息。
8. 测试通过只是完成证据，不会自动终止工具调用。
9. TUI 只展示模型最后一次上报状态，不根据命令或文件变化推断完成。
10. 主模型不可见 `task_boundary`，隐藏分类器仍可正常使用。
11. 所有 Provider 共用一份行为提示词，不增加模型专属提示词。

## 任务 1：简化并强制执行 Todo 数据契约

**涉及文件：**

- 修改：`firstcoder/tools/todo.py`
- 修改：`firstcoder/app/tui_state.py`
- 修改：`tests/test_todo.py`
- 修改：`tests/test_app_tui.py`

- [ ] **步骤 1：先把 Schema 测试改成新契约**

修改 `test_todo_definition_accepts_only_complete_todo_list`，要求模型可见 Todo item 只包含 `content` 和 `status`：

```python
def test_todo_definition_accepts_only_complete_todo_list() -> None:
    tool = create_todo_tool()

    parameters = tool.definition.parameters
    todo_items = parameters["properties"]["todos"]["items"]
    assert todo_items["required"] == ["content", "status"]
    assert set(todo_items["properties"]) == {"content", "status"}
    assert todo_items["properties"]["status"]["enum"] == [
        "pending",
        "in_progress",
        "completed",
        "cancelled",
    ]
```

- [ ] **步骤 2：增加多个进行中项目与精简输出测试**

```python
def test_todo_rejects_multiple_in_progress_items() -> None:
    result = create_todo_tool().executor(
        todos=[
            {"content": "检查实现", "status": "in_progress"},
            {"content": "运行测试", "status": "in_progress"},
        ]
    )

    assert result.ok is False
    assert "in_progress" in result.error


def test_todo_returns_compact_text_and_structured_state() -> None:
    result = create_todo_tool().executor(
        todos=[{"content": "检查实现", "status": "in_progress"}]
    )

    assert result.ok is True
    assert result.content == "Todo updated"
    assert result.data["todos"] == [
        {"content": "检查实现", "status": "in_progress"}
    ]
```

- [ ] **步骤 3：运行测试，确认当前实现会失败**

```bash
.venv/bin/python -m pytest tests/test_todo.py -q
```

预期：测试失败，原因包括 `priority` 仍是必填字段、多个 `in_progress` 仍被接受、工具结果仍重复返回完整清单。

- [ ] **步骤 4：实现最小归一化与约束校验**

```python
VALID_STATUSES = ("pending", "in_progress", "completed", "cancelled")
LEGACY_STATUS_ALIASES = {"done": "completed"}


def _normalize_todos(todos: object) -> tuple[list[dict[str, str]], str | None]:
    if not isinstance(todos, list):
        return [], "todos 必须是数组"

    normalized: list[dict[str, str]] = []
    active_count = 0
    for index, item in enumerate(todos, start=1):
        if not isinstance(item, dict):
            return [], f"todos[{index}] 必须是对象"
        content = str(item.get("content") or "").strip()
        if not content:
            return [], f"todos[{index}] 缺少 content"
        raw_status = str(item.get("status") or "pending")
        status = LEGACY_STATUS_ALIASES.get(raw_status, raw_status)
        if status not in VALID_STATUSES:
            return [], f"todos[{index}] 未知状态：{status}"
        active_count += status == "in_progress"
        normalized.append({"content": content, "status": status})

    if active_count > 1:
        return [], "最多只能有一个 in_progress Todo item"
    return normalized, None
```

工具文本结果只返回确认语，结构化数据继续供 Session 和 TUI 使用：

```python
def _format_result(todos: list[dict[str, str]]) -> ToolResult:
    return make_text_result("todo", "Todo updated", todos=todos, count=len(todos))
```

- [ ] **步骤 5：保留旧 Session 的 `priority` 兼容读取**

`TuiTodoItem.priority` 暂时保留为兼容字段，继续采用默认值读取：

```python
priority=str(item.get("priority") or "medium")
```

增加测试：同时输入一条带 `priority` 的旧数据和一条不带 `priority` 的新数据，断言两条都能正确恢复 `content` 和 `status`。

- [ ] **步骤 6：运行 Todo 和 TUI 测试**

```bash
.venv/bin/python -m pytest tests/test_todo.py tests/test_app_tui.py -q
```

预期：全部通过，旧 Session 恢复行为不退化。

- [ ] **步骤 7：提交 Todo 契约改造**

```bash
git add firstcoder/tools/todo.py firstcoder/app/tui_state.py tests/test_todo.py tests/test_app_tui.py
git commit -m "Simplify Todo payload contract"
```

## 任务 2：删除周期性 Todo reminder

**涉及文件：**

- 修改：`firstcoder/agent/todo_policy.py`
- 修改：`firstcoder/agent/loop.py`
- 修改：`tests/test_agent_context_loop.py`

- [ ] **步骤 1：用“不注入提醒”测试替换现有 reminder 测试**

删除所有期待 `Todo planning reminder` 或 `Todo progress reminder` 的测试。新增测试，模拟 Todo 后连续执行三个普通工具：

```python
def test_agent_loop_does_not_inject_periodic_todo_user_messages(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_no_todo_reminders",
        agents_md="",
        tools=[create_todo_tool(), _echo_tool()],
    )
    provider = FakeProvider(
        [
            ChatResponse(
                provider="fake",
                model="fake-model",
                content="",
                tool_calls=[
                    ToolCall(
                        id="call_todo",
                        name="todo",
                        arguments={
                            "todos": [
                                {"content": "检查实现", "status": "in_progress"},
                                {"content": "运行测试", "status": "pending"},
                            ]
                        },
                    )
                ],
                finish_reason="tool_calls",
            ),
            *[
                ChatResponse(
                    provider="fake",
                    model="fake-model",
                    content="",
                    tool_calls=[
                        ToolCall(
                            id=f"call_echo_{index}",
                            name="echo",
                            arguments={"text": str(index)},
                        )
                    ],
                    finish_reason="tool_calls",
                )
                for index in range(1, 4)
            ],
            ChatResponse(provider="fake", model="fake-model", content="完成"),
            ChatResponse(provider="fake", model="fake-model", content="已对账"),
        ]
    )

    AgentLoop(session=session, provider=provider).run_user_turn("完成多步骤任务")

    projected_user_messages = [
        message.content
        for request in provider.requests
        for message in request.messages
        if message.role == "user"
    ]
    assert all("Todo planning reminder" not in text for text in projected_user_messages)
    assert all("Todo progress reminder" not in text for text in projected_user_messages)
```

- [ ] **步骤 2：运行新测试，确认当前循环会失败**

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py::test_agent_loop_does_not_inject_periodic_todo_user_messages -q
```

预期：失败，因为当前 AgentLoop 会把 reminder 追加成真实 `role=user` 消息。

- [ ] **步骤 3：把 TodoPolicy 缩减到状态读取和结束前对账**

删除：

- `STALE_TOOL_RESULT_THRESHOLD`
- `MISSING_TOOL_RESULT_THRESHOLD`
- `_last_stale_reminder_count`
- `_missing_plan_reminded`
- `next_reminder()`
- `planning_reminder()`
- `progress_reminder()`
- `has_todo_result()`
- `non_todo_tool_results_since_latest_todo()`

保留当前任务过滤，并提供结束前对账指令：

```python
class TodoPolicy:
    def __init__(self, session: AgentSession) -> None:
        self.session = session

    def final_reconciliation_instruction(self) -> str | None:
        unfinished = self.latest_unfinished_todos()
        if not unfinished:
            return None
        lines = [
            "Before finalizing, reconcile the unfinished Todo items.",
            "Continue required work, update completed or cancelled statuses, "
            "or explain the real blocker. Do not claim completion while required work remains.",
        ]
        lines.extend(
            f"- [{item.get('status', 'pending')}] {item.get('content', '')}"
            for item in unfinished
        )
        return "\n".join(lines)
```

- [ ] **步骤 4：删除 AgentLoop 同步与异步两处提醒注入**

工具执行完成后直接进入下一次模型请求：

```python
tool_rounds += 1
if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
    return self._tool_round_limit_response(response), None
self._check_cancelled()
response = self._drop_unsupported_tool_calls(complete_once())
```

异步路径采用完全一致的语义。

- [ ] **步骤 5：运行 AgentLoop 测试**

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py -q
```

预期：全部通过，不再有测试依赖周期提醒。

- [ ] **步骤 6：提交 reminder 删除**

```bash
git add firstcoder/agent/todo_policy.py firstcoder/agent/loop.py tests/test_agent_context_loop.py
git commit -m "Remove periodic Todo reminders"
```

## 任务 3：增加单次请求有效的临时运行时指令

**涉及文件：**

- 修改：`firstcoder/agent/loop.py`
- 修改：`tests/test_agent_context_loop.py`
- 验证：`tests/test_model_request_options.py`
- 验证：`tests/test_providers.py`

- [ ] **步骤 1：增加“不落盘且只发送一次”的失败测试**

```python
def test_runtime_instruction_is_sent_once_without_persisting_user_message(tmp_path) -> None:
    store = JsonlSessionStore(tmp_path)
    session = AgentSession.create(
        store=store,
        session_id="sess_runtime_instruction",
        agents_md="",
    )
    session.append_user_message("真实用户请求")
    provider = FakeProvider(
        [
            ChatResponse(provider="fake", model="fake-model", content="first"),
            ChatResponse(provider="fake", model="fake-model", content="second"),
        ]
    )
    loop = AgentLoop(session=session, provider=provider)

    loop._complete_once(runtime_instruction="Reconcile Todo state")
    loop._complete_once()

    first_request = provider.requests[0]
    assert any(
        message.role == "system" and message.content == "Reconcile Todo state"
        for message in first_request.messages
    )
    assert all(
        "Reconcile Todo state" not in part.content
        for message in session.rebuild_view().messages
        for part in message.parts
    )
    assert all(
        message.content != "Reconcile Todo state"
        for message in provider.requests[1].messages
    )
```

- [ ] **步骤 2：增加同步、流式和 prompt-too-long 重试覆盖**

用现有 `FakeProvider`、`StreamingProvider` 和 prompt-too-long 测试 Provider 增加参数化测试，明确断言：

- 同步请求能收到该指令；
- 流式请求能收到该指令；
- prompt-too-long 压缩重试会再次带上同一条指令；
- `SessionView.messages` 永远不包含该指令。

- [ ] **步骤 3：运行测试，确认当前函数签名不支持该参数**

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py -k "runtime_instruction" -q
```

预期：失败，因为当前单次请求路径没有 `runtime_instruction` 参数。

- [ ] **步骤 4：增加 Provider 无关的请求消息构造函数**

```python
def _request_messages(self, *, runtime_instruction: str | None = None):
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
    return self._build_provider_messages(
        self.session.rebuild_view(),
        system_prefix=system_prefix,
    )
```

从 `firstcoder.providers.types` 导入 `ChatMessage`。不增加新的消息角色，因为 OpenAI-compatible 和 Anthropic 当前都已支持 `system`。

- [ ] **步骤 5：把参数贯穿所有单次请求和恢复路径**

同步路径采用：

```python
def _complete_once(
    self,
    *,
    tool_choice="auto",
    runtime_instruction: str | None = None,
) -> ChatResponse:
    self._repair_interrupted_tool_calls_before_provider_request()
    self._check_cancelled()
    self._append_pending_guidance()
    self._prepare_skills_for_current_turn()
    definitions = self._provider_tool_definitions()
    messages = self._request_messages(runtime_instruction=runtime_instruction)
    self._check_provider_call_limit()
    self._check_turn_timeout()
    self._check_cancelled()
    self.provider_call_count += 1
    return self.provider.complete(
        self._main_chat_request(messages, definitions, tool_choice)
    )
```

`_complete_once_with_recovery` 在压缩重试时必须继续传递原值：

```python
return self._complete_once(
    tool_choice=tool_choice,
    runtime_instruction=runtime_instruction,
)
```

`_stream_once`、`_stream_once_with_recovery` 和 `_stream_once_attempt` 使用同名参数，并在流式重试与同步 fallback 中原样向下传递。已有流事件消费逻辑保持不变。

- [ ] **步骤 6：验证两个 Provider Adapter**

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py -k "runtime_instruction" -q
.venv/bin/python -m pytest tests/test_model_request_options.py tests/test_providers.py -q
```

预期：OpenAI-compatible 收到额外的 system message；Anthropic 把它合并进 system prompt；请求参数和工具 Schema 不受影响。

- [ ] **步骤 7：提交临时指令通道**

```bash
git add firstcoder/agent/loop.py tests/test_agent_context_loop.py
git commit -m "Add ephemeral runtime instructions"
```

## 任务 4：用临时指令重做结束前 Todo 对账

**涉及文件：**

- 修改：`firstcoder/agent/loop.py`
- 修改：`firstcoder/agent/todo_policy.py`
- 修改：`tests/test_agent_context_loop.py`

- [ ] **步骤 1：把测试从实现细节改成可观察契约**

替换所有期待持久化 self-check `role=user` 消息的断言：

```python
assert any(
    message.role == "system" and "reconcile" in message.content.lower()
    for message in reconciliation_request.messages
)
assert all(
    "reconcile" not in part.content.lower()
    for message in session.rebuild_view().messages
    for part in message.parts
)
```

继续覆盖：

- Todo 全部完成或取消时不触发对账；
- 对账响应可以调用 Todo；
- 对账响应可以调用其他工具；
- 对账中可以正常触发权限确认和 prewrite review；
- 同步与流式路径行为一致。

- [ ] **步骤 2：增加“每个用户轮次最多一次”测试**

模拟第一次对账调用普通工具，之后 Todo 仍未完成。统计所有 Provider 请求中的对账 system message，断言数量严格等于 1。

- [ ] **步骤 3：运行测试，确认当前实现会把检查写入 Session**

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py -k "todo_self_check or todo_reconciliation" -q
```

预期：失败，因为当前 `_prepare_todo_self_check()` 会调用 `append_user_message()`。

- [ ] **步骤 4：增加轮次级一次性状态**

每次真实用户轮次开始时重置：

```python
def _begin_turn(self) -> None:
    self.provider_call_count = 0
    self.turn_started_at = self.clock()
    self._todo_reconciliation_attempted = False
```

生成对账指令时先锁定本轮状态：

```python
def _todo_reconciliation_instruction(self) -> str | None:
    if self._todo_reconciliation_attempted:
        return None
    instruction = self.todo_policy.final_reconciliation_instruction()
    if instruction is None:
        return None
    self._todo_reconciliation_attempted = True
    return instruction
```

只在一次 Provider 调用中使用：

```python
complete_once(runtime_instruction=instruction)
```

若模型返回工具调用，进入普通工具循环；后续请求不重复携带这条对账指令。

- [ ] **步骤 5：取消和硬上限不能触发结束前对账**

中断响应或 `AgentLoopStopReason` 上限响应不是模型自然给出的最终答案，应直接结束。增加 cancellation、tool round limit 和 provider call limit 断言，证明这些路径不产生对账请求。

- [ ] **步骤 6：运行对账与权限相关测试**

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py -k "todo_self_check or todo_reconciliation or prewrite_review" -q
```

预期：全部通过；对账只出现在一次 Provider 请求中，不进入 Session。

- [ ] **步骤 7：提交结束前对账改造**

```bash
git add firstcoder/agent/loop.py firstcoder/agent/todo_policy.py tests/test_agent_context_loop.py
git commit -m "Make Todo final check ephemeral"
```

## 任务 5：取消“成功验证即强制收尾”

**涉及文件：**

- 修改：`firstcoder/agent/loop.py`
- 修改：`firstcoder/agent/loop_limits.py`
- 修改：`firstcoder/agent/tool_execution.py`
- 删除：`firstcoder/agent/verification.py`
- 删除：`tests/test_agent_verification.py`
- 修改：`tests/test_agent_context_loop.py`
- 修改：`tests/test_agent_loop_limits.py`

- [ ] **步骤 1：用“成功后继续工作”测试替换强制收尾测试**

把 `test_agent_loop_forces_final_answer_after_successful_verification` 改为以下调用序列：

```text
shell(pytest 成功)
git_diff
todo(更新状态)
最终回答
```

断言：

```python
assert [call.name for call in executed_calls] == ["shell", "git_diff", "todo"]
assert all(request.tool_choice != "none" for request in provider.requests[1:])
assert response.content == "完成并已检查差异"
```

保留失败测试场景，证明失败输出同样会正常回到模型，由模型决定继续调试。

- [ ] **步骤 2：运行验证相关测试，确认当前代码过早结束**

```bash
.venv/bin/python -m pytest tests/test_agent_context_loop.py -k "verification" -q
```

预期：失败，因为当前 AgentLoop 在成功测试后调用 `tool_choice="none"`。

- [ ] **步骤 3：从 Loop Limits 删除语义停止字段**

删除 `successful_verification_stop`。`AgentLoopLimits` 只保留：

```python
max_tool_rounds: int | None = 200
max_provider_calls: int | None = 400
max_turn_seconds: float | None = 3600
```

同步更新 `default()`、`swe_lite()`、`summary()` 和 `tests/test_agent_loop_limits.py`。

- [ ] **步骤 4：删除验证成功状态传播和分类器**

删除：

- `ToolExecutionState.successful_verification`
- `is_successful_verification_result` import 与赋值
- 同步与异步工具循环中的强制 `tool_choice="none"` 分支
- `firstcoder/agent/verification.py`
- `tests/test_agent_verification.py`

先执行确认搜索：

```bash
rg -n "is_successful_verification_result|successful_verification" firstcoder tests
```

删除后该搜索不应再发现活动代码引用。

- [ ] **步骤 5：运行 Loop 与 Limits 测试**

```bash
.venv/bin/python -m pytest tests/test_agent_loop_limits.py tests/test_agent_context_loop.py -q
```

预期：测试通过后模型仍拥有工具权限；轮次、调用次数和时间上限仍可阻止无限循环。

- [ ] **步骤 6：提交验证循环改造**

```bash
git add firstcoder/agent/loop.py firstcoder/agent/loop_limits.py firstcoder/agent/tool_execution.py tests/test_agent_context_loop.py tests/test_agent_loop_limits.py
git add -u firstcoder/agent/verification.py tests/test_agent_verification.py
git commit -m "Let verification evidence return to the agent"
```

## 任务 6：将模型行为规则合并为一份提示词

**涉及文件：**

- 新增：`firstcoder/context/prompts/agent_instructions.md`
- 修改：`firstcoder/context/system_prompt.py`
- 删除：`firstcoder/context/prompts/agent_few_shots.md`
- 修改：`firstcoder/tools/descriptions.py`
- 修改：`firstcoder/context/versions.py`
- 修改：`tests/test_context_system_prompt.py`
- 修改：`tests/test_todo.py`

- [ ] **步骤 1：先把提示词测试改成“唯一来源”契约**

```python
for heading in (
    "# Role and instruction priority",
    "# Working loop",
    "# Project discipline",
    "# Tool use",
    "# Task tracking",
    "# Verification and completion",
    "# Communication",
):
    assert content.count(heading) == 1

assert "Todo planning reminder" not in content
assert "Todo progress reminder" not in content
assert "Call `task_boundary" not in content
assert "Call task_boundary" not in content
assert "successful verification" not in content.lower()
```

- [ ] **步骤 2：运行测试，确认当前硬编码规则和 few-shot 分裂状态不满足契约**

```bash
.venv/bin/python -m pytest tests/test_context_system_prompt.py -q
```

预期：失败，直到提示词合并完成。

- [ ] **步骤 3：创建唯一行为提示词 `agent_instructions.md`**

文件必须包含以下七个一级章节：

```markdown
# Role and instruction priority
# Working loop
# Project discipline
# Tool use
# Task tracking
# Verification and completion
# Communication
```

正文规则必须表达：

- 简单问题直接回答，依赖仓库事实时使用工具；
- 实施任务持续到验证和交付，不停在分析或半成品；
- 修改前阅读相关代码，保护用户已有工作；
- 复杂多阶段任务使用 Todo，简单任务跳过；
- Todo 每次完整替换，最多一个 `in_progress`；
- 完成并验证一步后，在开始下一步前更新状态；
- 普通状态变化不能随意改写、拆分、合并或重排步骤；
- Todo 是协作状态，不是正确性证据；
- 测试成功是证据，不代表自动完成；
- focused test 后根据共享入口和回归风险决定是否扩大验证；
- 最终回答前检查相关 diff/status；
- runtime 负责 task boundary 和 task hash，主模型不得发明 hash；
- 沟通简洁，只在自然里程碑汇报。

提示词保持 Provider 无关，不出现具体模型名、Provider 名或专属推理参数。

- [ ] **步骤 4：用一个加载函数替换硬编码规则与 few-shot**

```python
def _agent_instructions() -> str:
    path = Path(__file__).with_name("prompts") / "agent_instructions.md"
    return path.read_text(encoding="utf-8").strip()
```

在 `SystemPromptBuilder.build()` 中只追加一次 `_agent_instructions()`。删除 `_agent_behavior_rules()`、`_agent_few_shots()` 和 `agent_few_shots.md`。

- [ ] **步骤 5：把 Todo 工具描述缩减到工具本身语义**

```python
"todo": (
    "Replace the current Todo list for multi-step work. Each item has content and "
    "status; at most one item may be in_progress. An empty list clears Todo state."
),
```

系统提示词负责“何时规划、何时更新、何时完成”；工具描述只负责完整替换和机械约束；JSON Schema 只负责字段和枚举。

- [ ] **步骤 6：提升系统提示词版本**

把 `firstcoder/context/versions.py` 中的：

```python
SYSTEM_PROMPT_VERSION = "v12"
```

更新为：

```python
SYSTEM_PROMPT_VERSION = "v13"
```

同步修改精确版本断言，避免复用旧提示词缓存。

- [ ] **步骤 7：运行提示词与 Todo Schema 测试**

```bash
.venv/bin/python -m pytest tests/test_context_system_prompt.py tests/test_todo.py -q
```

预期：只加载一份行为提示词，每个章节只出现一次，旧 reminder 和 `task_boundary` 冲突文字均消失。

- [ ] **步骤 8：提交统一提示词**

```bash
git add firstcoder/context/prompts/agent_instructions.md firstcoder/context/system_prompt.py firstcoder/context/versions.py firstcoder/tools/descriptions.py tests/test_context_system_prompt.py tests/test_todo.py
git add -u firstcoder/context/prompts/agent_few_shots.md
git commit -m "Unify agent behavior prompt"
```

## 任务 7：让主模型看不到内部 `task_boundary` 工具

**涉及文件：**

- 修改：`firstcoder/agent/loop.py`
- 验证但不修改：`firstcoder/tools/session_registry.py`
- 修改：`tests/test_app_factory.py`
- 修改：`tests/test_agent_context_loop.py`
- 验证：`tests/test_model_request_options.py`
- 验证：`tests/test_task_boundary_tool.py`

- [ ] **步骤 1：增加主模型工具可见性失败测试**

```python
assert "task_boundary" in session.tool_registry.names()
assert "task_boundary" not in [tool.name for tool in provider.requests[0].tools]
```

保留隐藏分类器的既有测试，证明它仍可执行 `task_boundary` 并持久化 observation。

- [ ] **步骤 2：运行测试，确认当前主模型仍能看到该工具**

```bash
.venv/bin/python -m pytest tests/test_app_factory.py -k "task_boundary" -q
```

预期：失败，因为当前 `_provider_tool_definitions()` 返回全部 registry definitions。

- [ ] **步骤 3：只在主模型请求边界过滤内部工具**

复用现有隐藏工具常量，或新增职责明确的 `INTERNAL_TOOL_NAMES`：

```python
return [
    definition
    for definition in self.session.tool_registry.definitions()
    if definition.name not in INTERNAL_TOOL_NAMES
]
```

不得从 registry 删除 `task_boundary`，不得修改隐藏分类器的固定请求参数、Schema 和持久化逻辑。

- [ ] **步骤 4：运行 task boundary 与请求参数测试**

```bash
.venv/bin/python -m pytest tests/test_app_factory.py tests/test_task_boundary_tool.py tests/test_model_request_options.py -q
```

预期：主模型看不到该工具；隐藏分类器全部测试仍通过。

- [ ] **步骤 5：提交工具可见性边界**

```bash
git add firstcoder/agent/loop.py tests/test_app_factory.py tests/test_agent_context_loop.py
git commit -m "Hide task boundary from main model"
```

## 任务 8：明确并验证 TUI Todo 投影

**涉及文件：**

- 修改：`firstcoder/app/activity_view.py`
- 修改：`tests/test_app_tui.py`
- 验证：`firstcoder/app/tui.py`
- 验证：`tests/test_context_writer.py`
- 验证：`tests/test_session_resume_service.py`

- [ ] **步骤 1：增加“模型上报状态”标题测试**

```python
text = todo_panel_text(
    [TuiTodoItem(content="运行测试", status="in_progress")]
)
assert text.splitlines()[0] == "Todo · model reported"
assert "[~] 运行测试" in text
```

- [ ] **步骤 2：增加实时更新与恢复投影一致性测试**

测试按以下顺序执行：

1. 向 TUI 传入成功的 Todo `ToolExecutionEvent`；
2. 记录 `app.transcript.todos` 的 `content/status`；
3. 从同一 Session 的 `todo_updated` 重建 `SessionView`；
4. 调用 `_replay_current_session()`；
5. 断言重放后的 `content/status` 与实时事件结果一致；
6. 输入中包含一条带 `priority` 的旧数据，验证兼容读取。

- [ ] **步骤 3：运行 Todo 相关 TUI 测试**

```bash
.venv/bin/python -m pytest tests/test_app_tui.py -k "todo" -q
```

预期：标题断言失败，现有实时与恢复路径仍可被测试覆盖。

- [ ] **步骤 4：只修改展示标题**

```python
lines = ["Todo · model reported"]
```

不得增加命令到 Todo 的自动推断；不得因 `pytest`、文件写入、shell exit code 0 或任意工具成功而自动勾选项目。

- [ ] **步骤 5：运行状态持久化与 TUI 测试**

```bash
.venv/bin/python -m pytest tests/test_app_tui.py tests/test_context_writer.py tests/test_session_resume_service.py -q
```

预期：实时更新、Session 切换、恢复和旧 payload 重放结果一致。

- [ ] **步骤 6：提交 TUI 表达调整**

```bash
git add firstcoder/app/activity_view.py tests/test_app_tui.py
git commit -m "Clarify Todo as model reported state"
```

## 任务 9：完整验证与架构文档对齐

**涉及文件：**

- 修改：`docs/ARCHITECTURE.md`
- 验证：任务 1—8 修改的全部文件

- [ ] **步骤 1：搜索已经废弃的行为与文字**

```bash
rg -n "Todo planning reminder|Todo progress reminder|successful_verification_stop|agent_few_shots|Call task_boundary|Call `task_boundary|priority.*Todo|Todo.*priority" firstcoder tests docs README.md
```

预期：活动代码、测试和当前技术文档中不再描述已删除行为。历史计划文件可以保留明确的历史记录，不改写其他用户已有计划。

- [ ] **步骤 2：更新当前架构文档**

在 `docs/ARCHITECTURE.md` 的 AgentLoop、Session 状态和职责表部分明确：

- `todo_updated` 是持久化的完整 Todo 快照；
- Todo 工具事件驱动 TUI 实时刷新；
- 结束前对账是临时 system instruction，不是 Session event；
- 验证证据返回模型，不会终止 AgentLoop；
- 隐藏 task-boundary classifier 负责内部 `task_boundary` 使用；
- 删除 `agent/verification.py` 的职责行。

上下文管理文档不修改，因为它们描述的是压缩和生命周期分类，而非 Todo 循环控制。

- [ ] **步骤 3：运行聚焦测试集**

```bash
.venv/bin/python -m pytest \
  tests/test_todo.py \
  tests/test_context_system_prompt.py \
  tests/test_agent_context_loop.py \
  tests/test_agent_loop_limits.py \
  tests/test_app_factory.py \
  tests/test_app_tui.py \
  tests/test_context_writer.py \
  tests/test_session_resume_service.py \
  tests/test_model_request_options.py \
  tests/test_providers.py -q
```

预期：全部通过。

- [ ] **步骤 4：运行仓库安全范围内的完整测试**

```bash
.venv/bin/python -m pytest tests
```

预期：全部通过。不要使用不限定目录的 `pytest`，避免收集 benchmark 或生成目录。

- [ ] **步骤 5：检查最终 diff 和用户文件**

```bash
git status --short
git diff --check
git diff --stat
git diff -- firstcoder tests docs/ARCHITECTURE.md
```

预期：

- 没有空白错误；
- 没有发布产物被暂存；
- `.gitignore` 仍是用户无关修改；
- 没有新增模型专属提示词；
- 没有加入 Hook 系统、稳定 Todo ID 或自动完成推断。

- [ ] **步骤 6：提交架构文档更新**

```bash
git add docs/ARCHITECTURE.md
git commit -m "Document Todo runtime boundaries"
```

## 四、最终验收清单

- [ ] 多步骤任务连续运行三个以上工具时，不出现合成的 Todo `user` 消息。
- [ ] focused test 成功后不设置 `tool_choice="none"`，模型仍可检查 diff/status。
- [ ] 结束前 Todo 对账只出现在一次 Provider 请求中，不进入 Session 历史。
- [ ] 对账过程支持 Todo、其他工具、权限确认、prewrite review 和 streaming。
- [ ] 新 Todo 调用会拒绝多个 `in_progress`。
- [ ] 新 Todo payload 不含 `priority`，旧 Session 仍可渲染。
- [ ] Todo 工具文本结果保持精简，完整结构化状态仍写入 `todo_updated`。
- [ ] TUI 实时状态和恢复后的 Todo 状态完全一致。
- [ ] TUI 明确标注模型上报状态，不做语义自动完成。
- [ ] 主模型不能调用 `task_boundary`，隐藏分类器仍能调用。
- [ ] 稳定行为提示词只从一个 Markdown 文件加载。
- [ ] OpenAI-compatible 和 Anthropic 测试均通过，不含 Provider 专属行为分支。
- [ ] `.venv/bin/python -m pytest tests` 全部通过。

## 五、不在本轮范围内

- 稳定 Todo item ID
- Todo 局部 patch/update
- 根据命令、测试或文件变化自动判断 Todo 完成
- Provider 或模型专属提示词
- Plan mode
- Hook/插件生命周期系统
- Reviewer 自动加入主循环
- 推理强度参数改造
- Release、推送、PyPI 发布或版本号更新
