# Agent Loop Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reduce duplicated synchronous/asynchronous Agent Loop production code while preserving every public entry point and turn-state behavior.

**Architecture:** Keep synchronous provider/tool scheduling and asynchronous streaming scheduling explicit. Extract only pure state transitions and identical finalization into narrow helpers, so both paths remain readable and externally unchanged.

**Tech Stack:** Python 3.11+, pytest, anyio, dataclasses

---

### Task 1: Lock down sync/async turn parity

**Files:**
- Modify: `tests/test_agent_context_loop.py`
- Test: `tests/test_agent_context_loop.py`

- [ ] **Step 1: Add a characterization test for matching final state**

Add one parametrized test that runs equivalent tool turns through `run_user_turn()` and `run_user_turn_streaming_sync()`, then compares the response, persisted message roles, tool-call IDs, and pending-input state. Use separate stores/providers per mode.

```python
@pytest.mark.parametrize("streaming", [False, True])
def test_sync_and_streaming_tool_loops_persist_equivalent_terminal_state(tmp_path, streaming):
    store = JsonlSessionStore(tmp_path / str(streaming))
    session = AgentSession.create(store=store, session_id="sess_parity", agents_md="")
    responses = [
        ChatResponse(
            provider="fake",
            model="fake-model",
            content="",
            tool_calls=[ToolCall(id="call_1", name="echo", arguments={"text": "abc"})],
            finish_reason="tool_calls",
        ),
        ChatResponse(provider="fake", model="fake-model", content="完成"),
    ]
    provider = StreamingProvider(responses) if streaming else FakeProvider(responses)
    loop = AgentLoop(session=session, provider=provider, tools=[_echo_tool()])

    response = (
        loop.run_user_turn_streaming_sync("调用工具")
        if streaming
        else loop.run_user_turn("调用工具")
    )

    view = session.rebuild_view()
    assert response.content == "完成"
    assert [message.role for message in view.messages] == ["user", "assistant", "tool", "assistant"]
    assert view.messages[1].parts[0].metadata["tool_call_id"] == "call_1"
    assert session.pending_permission_execution is None
```

- [ ] **Step 2: Prove the characterization test detects a regression**

Temporarily change the expected role list to omit `"tool"` and run:

```sh
.venv/bin/python -m pytest tests/test_agent_context_loop.py::test_sync_and_streaming_tool_loops_persist_equivalent_terminal_state -q
```

Expected: two assertion failures. Restore the correct assertion and rerun; expected: `2 passed`.

- [ ] **Step 3: Run the existing Agent Loop baseline**

```sh
.venv/bin/python -m pytest tests/test_agent_context_loop.py tests/test_agent_e2e.py -q
```

Expected: all tests pass before production edits.

### Task 2: Share terminal result construction

**Files:**
- Modify: `firstcoder/agent/loop.py`
- Test: `tests/test_agent_context_loop.py`

- [ ] **Step 1: Add narrow helpers for pending and completed turns**

Introduce helpers with no provider calls and no `await`:

```python
def _pending_turn_result(self, pending_input: UserInputRequest) -> AgentTurnResult:
    return AgentTurnResult(
        status=AgentTurnStatus.WAITING_FOR_USER_INPUT,
        pending_input=pending_input,
    )

def _complete_turn(self, response: ChatResponse) -> AgentTurnResult:
    self.session.append_assistant_response(response)
    self._auto_compact()
    return AgentTurnResult(status=AgentTurnStatus.COMPLETED, response=response)
```

Use them in both `_run_tool_loop_interactive()` variants and in the pending permission branches. Do not change public method signatures.

- [ ] **Step 2: Share cancellation finalization**

Add:

```python
def _cancelled_turn_result(self) -> AgentTurnResult:
    self._append_interrupted_tool_results()
    return self._complete_turn(self._interrupted_response())
```

Replace the duplicated post-exception cancellation block in the sync and async loops. Preserve the existing check order.

- [ ] **Step 3: Run focused tests**

```sh
.venv/bin/python -m pytest tests/test_agent_context_loop.py -q
```

Expected: all tests pass.

### Task 3: Share tool-loop state advancement

**Files:**
- Modify: `firstcoder/agent/loop.py`
- Test: `tests/test_agent_context_loop.py`

- [ ] **Step 1: Define an internal step result**

Add a private dataclass near `_AgentLoopLimitReached`:

```python
@dataclass(frozen=True, slots=True)
class _ToolLoopStep:
    tool_rounds: int
    pending_input: UserInputRequest | None = None
    stop_response: ChatResponse | None = None
    next_tool_choice: object = "auto"
```

- [ ] **Step 2: Extract identical post-execution state changes**

Add a helper that receives the current response and `ToolExecutionState`, then performs auto-compaction, task-hash compaction, round counting, limit handling, todo reminder insertion, and next tool-choice selection. It must not call a provider or execute a tool.

```python
def _advance_tool_loop(
    self,
    response: ChatResponse,
    execution: ToolExecutionState,
    tool_rounds: int,
) -> _ToolLoopStep:
    if execution.pending_input is not None:
        return _ToolLoopStep(tool_rounds, pending_input=execution.pending_input)
    self._auto_compact()
    if execution.task_hash_changed:
        self._compact_after_task_hash_changed()
    if self.limits.successful_verification_stop and execution.successful_verification:
        return _ToolLoopStep(tool_rounds, next_tool_choice="none")
    tool_rounds += 1
    if self.max_tool_rounds is not None and tool_rounds >= self.max_tool_rounds:
        return _ToolLoopStep(tool_rounds, stop_response=self._tool_round_limit_response(response))
    if reminder := self.todo_policy.next_reminder():
        self.session.append_user_message(reminder)
    return _ToolLoopStep(tool_rounds)
```

- [ ] **Step 3: Make sync and async loops thin adapters**

Each loop keeps only its distinct execution and provider call:

```python
execution = self.tool_executor.execute_interactive(response.tool_calls)
step = self._advance_tool_loop(response, execution, tool_rounds)
```

and:

```python
execution = await self.tool_executor.execute_interactive_async(response.tool_calls)
step = self._advance_tool_loop(response, execution, tool_rounds)
```

Handle `pending_input`, `stop_response`, and `next_tool_choice` identically. If the helper plus adapters do not produce a net line reduction, revert this task.

- [ ] **Step 4: Run tool-loop edge cases**

```sh
.venv/bin/python -m pytest tests/test_agent_context_loop.py -q -k 'tool_round or todo_self_check or permission or parallel or successful_verification'
```

Expected: all selected tests pass.

### Task 4: Verify and measure the Agent Loop batch

**Files:**
- Modify: `docs/superpowers/plans/2026-07-19-simplify-agent-loop.md`

- [ ] **Step 1: Run full verification**

```sh
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q firstcoder
git diff --check
```

Expected: 0 failures and exit code 0 for all commands.

- [ ] **Step 2: Record production-code delta**

```sh
find firstcoder -name '*.py' -type f -print0 | xargs -0 wc -l | tail -n 1
git diff --numstat -- firstcoder/agent/loop.py
```

Record the measured total and net reduction in this plan. Do not count test changes.

- [ ] **Step 3: Commit only this batch**

```sh
git add firstcoder/agent/loop.py tests/test_agent_context_loop.py docs/superpowers/plans/2026-07-19-simplify-agent-loop.md
git commit -m "Simplify agent loop state transitions"
```

## Execution record

- Added sync/streaming terminal-state characterization: red run `2 failed`, corrected run `2 passed`.
- Existing Agent Loop/E2E baseline before refactor: `99 passed`.
- Kept shared `_pending_turn_result`, `_complete_turn`, and `_prepare_todo_self_check` helpers.
- Rejected `_advance_tool_loop`: the helper plus four-value branching reduced `loop.py` by only two lines and made each adapter harder to follow. It was reverted before the final run.
- Focused Agent Loop suite: `86 passed`.
- Edge selection: `26 passed, 60 deselected`.
- Full suite: `1185 passed, 30 warnings`.
- `compileall` and `git diff --check`: exit 0.
- `firstcoder/agent/loop.py`: 23 additions, 31 deletions, net -8 production lines.
- Production total after this batch: 25,585 lines; cumulative reduction from 25,616 baseline: 31 lines.
