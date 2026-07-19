# Pre-write Diff Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require a human-readable, trusted preview before FirstCoder performs a direct local file mutation.

**Architecture:** Add a review planner that models the `write`, `edit`, `apply_patch`, and `delete` tool calls against the project filesystem without writing it. The planner emits immutable review data into the existing pending-permission state; the TUI renders that data as a highlighted, bounded unified diff before users choose the existing permission options or reject with feedback.

**Tech Stack:** Python 3.11, Textual/Rich, pytest, existing `PermissionManager`, `AgentSession`, and built-in file tools.

---

### Task 1: Model trusted file-change previews

**Files:**
- Create: `firstcoder/tools/review.py`
- Test: `tests/test_prewrite_review.py`

- [ ] Write failing tests for a new write, an edit, a patch move/delete, a direct delete, and an invalid edit. Assert the planner returns immutable before/after summaries, operation kinds, unified diff text, line counts, and validation errors without changing disk state.
- [ ] Run: `.venv/bin/python -m pytest tests/test_prewrite_review.py -q`; expect failure because `firstcoder.tools.review` does not exist.
- [ ] Implement `build_prewrite_review(root, tool_call)` using `PathSandbox`, the existing patch parser, and `difflib.unified_diff`. Make it return structured review entries, bounded rendered diffs, aggregate counts, and an execution-blocking error when a mutation cannot be applied.
- [ ] Run the focused test again; expect all cases to pass.

### Task 2: Attach review snapshots to the permission pause

**Files:**
- Modify: `firstcoder/agent/session.py`
- Modify: `firstcoder/agent/tool_execution.py`
- Modify: `firstcoder/runtime/user_input.py`
- Test: `tests/test_agent_context_loop.py`
- Test: `tests/test_app_runtime.py`

- [ ] Write failing loop tests proving an `ASK` mutation produces `pending.payload["prewrite_review"]`, leaves files untouched, and executes the original locally-stored tool call after approval even if the UI payload is tampered with.
- [ ] Run those test nodes; expect assertions for `prewrite_review` to fail.
- [ ] Build reviews only for direct mutation tools. Store the review in `PendingPermissionExecution`, restore it on resumed sessions, and expose a copy on `UserInputRequest.payload`. If planning fails, append a rejected tool result without creating a pending confirmation.
- [ ] Run the loop and runner tests; expect them to pass.

### Task 3: Support rejection feedback without writing

**Files:**
- Modify: `firstcoder/permissions/types.py`
- Modify: `firstcoder/permissions/manager.py`
- Modify: `firstcoder/agent/loop.py`
- Modify: `firstcoder/app/permission_view.py`
- Test: `tests/test_permission_registry.py`
- Test: `tests/test_agent_context_loop.py`

- [ ] Write failing tests for `reject_with_feedback: <text>`, asserting the call does not execute, its tool result contains the feedback, and the model receives the feedback on the resumed turn.
- [ ] Run the new tests; expect the answer parser and decision resolver to reject the new choice.
- [ ] Add a non-persistent `REJECT_WITH_FEEDBACK` permission choice, parse it in the TUI, and record it as a permission-denied tool result with the feedback. Preserve `deny`, `allow once`, and `allow always` semantics.
- [ ] Run the permission and loop tests; expect them to pass.

### Task 4: Render bounded red/green review cards

**Files:**
- Create: `firstcoder/app/review_view.py`
- Modify: `firstcoder/app/tui.py`
- Modify: `firstcoder/app/tui.tcss`
- Test: `tests/test_review_view.py`
- Test: `tests/test_app_tui.py`

- [ ] Write failing rendering tests for creation, update, deletion, move metadata, omitted diff lines, and option text. Assert Rich markup distinguishes additions and removals and contains per-file statistics.
- [ ] Run the tests; expect import or rendering failures.
- [ ] Implement a Rich/Textual review renderable with per-file headers, red deletion lines, green addition lines, muted context, truncation notice, and a readable plain-text fallback. Mount it from `_write_pending_input` before the permission controls.
- [ ] Run rendering and TUI tests; expect them to pass.

### Task 5: Verify direct-mutation coverage and documentation

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Test: `tests/test_mutation_tools.py`

- [ ] Add failing coverage tests that verify `write`, `edit`, `apply_patch`, and `delete` all produce preview entries; assert shell calls do not claim a precomputed diff.
- [ ] Run mutation and review tests; expect the new review coverage to fail before any missing adapter is implemented.
- [ ] Complete any missing planner adapter and document the feature, its limits, and the feedback response syntax in both READMEs.
- [ ] Run the focused review/permission/TUI suite, then `.venv/bin/python -m pytest` for the full suite. Inspect `git diff --check` before handoff.
