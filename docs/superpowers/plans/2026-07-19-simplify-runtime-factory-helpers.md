# Runtime, Factory, and Helper Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove repeated turn bookkeeping, model-profile application, and genuinely identical helpers after the larger Agent Loop/provider/event batches are stable.

**Architecture:** Use ordinary private methods with explicit inputs and return values. Do not introduce decorators, reflection, inheritance, or cross-domain utility dumping grounds.

**Tech Stack:** Python 3.11+, pytest, Textual runtime fakes

---

### Task 1: Characterize runtime finalization

**Files:**
- Modify: `tests/test_app_runtime.py`

- [ ] **Step 1: Add sync/streaming bookkeeping parity tests**

Cover completed response and pending permission response. Assert `last_pending_input`, `last_display_lines`, `last_stream_events`, and active cancellation-token cleanup.

```python
assert runner._active_cancellation_token is None
assert runner.last_pending_input is None
assert runner.last_display_lines == ["streamed"]
```

- [ ] **Step 2: Prove failure detection**

Temporarily assert a non-empty active token, run the new test, observe failure, restore the assertion, and observe pass.

### Task 2: Share AgentChatRunner turn bookkeeping

**Files:**
- Modify: `firstcoder/app/runtime.py`
- Test: `tests/test_app_runtime.py`

- [ ] **Step 1: Add start/finalize helpers**

```python
def _start_turn(self, *, streaming: bool = False) -> tuple[int, CancellationToken, AgentLoop]:
    before_count = len(self.current_session.rebuild_view().messages)
    self.last_pending_input = None
    token = self._begin_cancellable_turn()
    return before_count, token, self._create_loop(token, streaming=streaming)

def _refresh_turn_output(self, before_count: int, loop: AgentLoop) -> None:
    self.last_stream_events = list(loop.last_stream_events)
    messages = self.current_session.rebuild_view().messages[before_count:]
    self.last_display_lines = _display_lines_from_messages(messages)
```

- [ ] **Step 2: Apply helpers to four entry points**

Use them in `run_user_turn`, `resume_with_user_input`, `arun_user_turn`, and `aresume_with_user_input`. Preserve the exact rules that append pending questions or fallback response content to `last_display_lines`.

- [ ] **Step 3: Run runtime tests**

```sh
.venv/bin/python -m pytest tests/test_app_runtime.py tests/test_app_tui.py -q
```

Expected: all pass. Revert if production lines do not decrease.

### Task 3: Share catalog profile application

**Files:**
- Modify: `firstcoder/app/factory.py`
- Test: `tests/test_app_factory.py`

- [ ] **Step 1: Extract the repeated profile application**

```python
def _apply_profile(self, profile: ModelProfile, *, persist: bool) -> ModelState:
    try:
        provider = create_provider_for_model(self._app_config, profile)
    except ProviderConfigError as error:
        raise ValueError(str(error)) from error
    self._current_profile = profile
    self._chat_runner.set_model(
        provider,
        request_options=_main_request_options(profile),
        use_streaming=_should_use_streaming(provider, self._app_config),
    )
    self._compact_summarizer.provider = provider
    if persist and self._state_store is not None:
        self._state_store.record_selection(profile.ref)
    return ModelState(provider=provider.name, model=provider.model)
```

- [ ] **Step 2: Replace both catalog branches**

The explicit `provider/model` branch calls `_apply_profile(profile, persist=True)`. The current-provider shortcut calls it with `persist=False`. Keep legacy provider switching untouched.

- [ ] **Step 3: Run model tests**

```sh
.venv/bin/python -m pytest tests/test_app_factory.py tests/test_app_model_commands.py tests/test_config.py -q
```

Expected: all pass.

### Task 4: Consolidate only identical small helpers

**Files:**
- Modify: `firstcoder/context/identity.py`
- Modify: `firstcoder/context/compaction.py`
- Modify: `firstcoder/context/manager.py`
- Modify: `firstcoder/agent/tool_flow.py`
- Modify: `firstcoder/context/writer.py`
- Test: `tests/test_context_compaction_pipeline.py`
- Test: `tests/test_context_window_manager.py`
- Test: `tests/test_context_writer.py`
- Test: `tests/test_agent_tool_flow.py`

- [ ] **Step 1: Share context-view fingerprinting**

Add one domain helper:

```python
def session_view_fingerprint(view: SessionView) -> str:
    return stable_json_hash(
        {"session_id": view.session_id, "messages": [message.to_dict() for message in view.messages]},
        length=24,
    )
```

Replace `_view_fingerprint` and `_effective_context_fingerprint`. Do not move unrelated identity helpers.

- [ ] **Step 2: Reuse tool-call part conversion**

Rename `context.writer._tool_call_part()` to `tool_call_to_part()` and make `agent/tool_flow.py` import and re-export that function. This preserves `firstcoder.agent.tool_flow.tool_call_to_part` for existing callers without making the context package import the agent package. Remove the duplicate function body and its now-unused identity/model imports from `agent/tool_flow.py`.

The five-line `_positive_int()` functions remain explicit in CLI and SWE-bench. Creating a cross-domain parsing module would increase total production lines and couple the application CLI to the evaluation CLI.

- [ ] **Step 3: Run focused tests**

```sh
.venv/bin/python -m pytest tests/test_context_compaction_pipeline.py tests/test_context_window_manager.py tests/test_context_writer.py tests/test_agent_tool_flow.py tests/test_cli.py tests/test_eval_swebench.py -q
```

Expected: all pass.

### Task 5: Final repository audit

**Files:**
- Modify: `docs/superpowers/plans/2026-07-19-simplify-runtime-factory-helpers.md`

- [ ] **Step 1: Verify all public exports import**

```sh
.venv/bin/python - <<'PY'
import firstcoder.agent, firstcoder.app, firstcoder.config, firstcoder.input
import firstcoder.mcp, firstcoder.permissions, firstcoder.providers
import firstcoder.runtime, firstcoder.session, firstcoder.skills, firstcoder.tools
for module in (
    firstcoder.agent, firstcoder.app, firstcoder.config, firstcoder.input,
    firstcoder.mcp, firstcoder.permissions, firstcoder.providers,
    firstcoder.runtime, firstcoder.session, firstcoder.skills, firstcoder.tools,
):
    for name in getattr(module, "__all__", ()):
        getattr(module, name)
print("public exports ok")
PY
```

- [ ] **Step 2: Run full verification**

```sh
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q firstcoder
.venv/bin/vulture firstcoder --min-confidence 80
git diff --check
```

Treat vulture output as candidates, not failures; document every remaining high-confidence result.

- [ ] **Step 3: Calculate final production delta**

```sh
find firstcoder -name '*.py' -type f -print0 | xargs -0 wc -l | tail -n 1
git diff --numstat 691eabf..HEAD -- firstcoder
git diff --numstat -- firstcoder
```

Report current total, reduction from the 25,616-line baseline, files changed, and rejected candidates. Tests/docs/benchmark do not count.

- [ ] **Step 4: Commit the final batch**

```sh
git add firstcoder tests docs/superpowers/plans/2026-07-19-simplify-runtime-factory-helpers.md
git commit -m "Simplify runtime and shared helpers"
```
