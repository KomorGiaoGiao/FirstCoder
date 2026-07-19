# Event and Session Simplification Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize repeated session-event envelopes and session-service validation while preserving the append-only schema and public service APIs.

**Architecture:** `SessionEventWriter` remains the sole event-envelope owner. Session services reuse plain functions for validation/bootstrap assembly; create, resume, and fork business operations stay separate.

**Tech Stack:** Python 3.11+, pytest, JSONL session store

---

### Task 1: Protect every event envelope

**Files:**
- Modify: `tests/test_context_writer.py`
- Modify: `tests/test_agent_skill_flow.py`

- [ ] **Step 1: Add an event-envelope characterization test**

Write representative session metadata, message metadata, todo, compaction-skipped, and skill audit events. Assert every event has a non-empty id, the requested session id, exact event type, and unchanged payload keys.

```python
def test_writer_applies_a_consistent_event_envelope(tmp_path):
    store = JsonlSessionStore(tmp_path)
    writer = SessionEventWriter(store=store, session_id="sess_event")
    writer.append_session_metadata_updated(title="Demo")
    event = store.list_events("sess_event")[0]
    assert event.id
    assert event.session_id == "sess_event"
    assert event.type == "session_metadata_updated"
    assert event.payload == {"title": "Demo"}
```

- [ ] **Step 2: Prove red/green behavior**

Temporarily expect the wrong event type, run the new test, observe failure, restore the assertion, and observe pass.

### Task 2: Centralize event envelope creation

**Files:**
- Modify: `firstcoder/context/writer.py`
- Modify: `firstcoder/skills/session.py`
- Test: `tests/test_context_writer.py`
- Test: `tests/test_agent_skill_flow.py`

- [ ] **Step 1: Add the narrow writer primitive**

```python
def append_event(self, event_type: str, payload: dict[str, Any]) -> None:
    self.store.append_event(
        SessionEvent(
            id=new_event_id(),
            session_id=self.session_id,
            type=event_type,
            payload=payload,
        )
    )
```

This is a writer API, not a store bypass. Keep `append_task_boundary_observation()` unchanged because the domain service already builds a complete event.

- [ ] **Step 2: Replace repeated envelopes**

Change writer methods to call `append_event(type, payload)`. In `skills/session.py`, retain three explicit audit functions but replace the repeated `SessionEvent(...)` envelope with `writer.append_event(...)`. Delete now-unused event/id imports.

- [ ] **Step 3: Run event and replay tests**

```sh
.venv/bin/python -m pytest tests/test_context_writer.py tests/test_context_store.py tests/test_context_runtime_replay.py tests/test_agent_skill_flow.py tests/test_skill_loader.py -q
```

Expected: all pass.

### Task 3: Share session record validation

**Files:**
- Modify: `firstcoder/session/catalog.py`
- Modify: `firstcoder/session/resume.py`
- Modify: `firstcoder/session/fork.py`
- Test: `tests/test_session_resume_service.py`
- Test: `tests/test_app_session_commands.py`

- [ ] **Step 1: Add a public domain validator inside session package**

```python
def require_usable_record(record: SessionRecord) -> SessionRecord:
    if record.status == "corrupt":
        raise SessionCorruptError(record.error or f"session is corrupt: {record.session_id}")
    if record.status == "empty":
        raise SessionEmptyError(f"session is empty: {record.session_id}")
    return record
```

Import `SessionRecord`, `SessionCorruptError`, and `SessionEmptyError` in `catalog.py`.

- [ ] **Step 2: Replace duplicate corrupt/empty checks**

Use `require_usable_record(catalog.get_session(...))` in resume and fork. Preserve `SessionNotFoundError` behavior and fork's explicit empty event-log check.

- [ ] **Step 3: Run session service tests**

```sh
.venv/bin/python -m pytest tests/test_session_resume_service.py tests/test_app_session_commands.py tests/test_session_catalog.py -q
```

Expected: all pass.

### Task 4: Verify and measure the event/session batch

**Files:**
- Modify: `docs/superpowers/plans/2026-07-19-simplify-events-sessions.md`

The repeated `SessionBootstrap(...)` argument blocks remain explicit. A shared service protocol/helper would add more production lines than it removes and would obscure the independent create/resume/fork services.

- [ ] **Step 1: Run full verification**

```sh
.venv/bin/python -m pytest tests -q
.venv/bin/python -m compileall -q firstcoder
git diff --check
```

- [ ] **Step 2: Measure and commit**

```sh
find firstcoder -name '*.py' -type f -print0 | xargs -0 wc -l | tail -n 1
git diff --numstat -- firstcoder/context/writer.py firstcoder/skills/session.py firstcoder/session
git add firstcoder/context/writer.py firstcoder/skills/session.py firstcoder/session tests/test_context_writer.py tests/test_agent_skill_flow.py tests/test_session_resume_service.py tests/test_app_session_commands.py docs/superpowers/plans/2026-07-19-simplify-events-sessions.md
git commit -m "Simplify session event assembly"
```

## Execution record

- Added event-envelope characterization: red run `1 failed`, corrected run `1 passed`.
- Event/replay/skill focused suite: `35 passed`.
- Session service focused suite: `40 passed`.
- Full suite: `1188 passed, 30 warnings`.
- `compileall` and `git diff --check`: exit 0.
- Centralized event id/session/type construction in `SessionEventWriter.append_event`; skill audit events now use the same envelope owner.
- Shared corrupt/empty record validation while preserving missing-session and fork empty-log behavior.
- Did not abstract repeated `SessionBootstrap(...)` arguments: a service protocol/helper would add coupling and little or no net reduction.
- Production total after this batch: 25,510 lines; this batch net reduction: 41 lines; cumulative reduction from 25,616 baseline: 106 lines.
