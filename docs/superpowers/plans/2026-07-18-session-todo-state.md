# Session Todo State Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the shared in-memory Todo store with one session-scoped append-only Todo state.

**Architecture:** The `todo` tool becomes a stateless whole-list writer. Successful calls append a `todo_updated` event; `SessionView.todos` is consumed by TodoPolicy, resume/fork, and TUI replay. Legacy Todo tool results remain readable as a fallback until a native Todo event appears.

**Tech Stack:** Python, JSONL event sourcing, Textual, pytest

---

### Task 1: Add session Todo state

- [x] Add failing tests for `todo_updated` replay and session isolation.
- [x] Add `SessionView.todos` and Todo event writer/store replay support.
- [x] Persist successful Todo results as native events.

### Task 2: Make the Todo tool stateless

- [x] Replace CRUD tests with whole-list replacement tests.
- [x] Remove `TodoStore`, IDs, action dispatch, and mutable closure state.
- [x] Update the model-visible schema and prompt instructions.

### Task 3: Unify policy and UI reads

- [x] Make TodoPolicy read `SessionView.todos` for the active task.
- [x] Restore the TUI Todo panel from the current session view on replay/switch.
- [x] Keep live tool-event rendering as an immediate projection of the same payload.

### Task 4: Verify behavior

- [x] Run Todo, AgentLoop, session, TUI, and full test suites.
- [x] Confirm new/resume/fork session isolation and legacy JSONL compatibility.
