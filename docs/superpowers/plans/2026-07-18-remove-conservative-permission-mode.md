# Remove Conservative Permission Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the redundant `conservative` permission mode so FirstCoder exposes only `standard`, `aggressive`, and `bypass`.

**Architecture:** Delete the enum member at the policy boundary, then update every public mode listing and test expectation. Do not retain an alias: `/mode conservative` must follow the existing unknown-mode path.

**Tech Stack:** Python, Textual, pytest, Markdown

---

### Task 1: Lock the three-mode public contract

**Files:**
- Modify: `tests/test_permission_commands.py`
- Modify: `tests/test_app_help_commands.py`
- Modify: `tests/test_app_tui.py`
- Modify: `tests/test_permissions_policy.py`

- [x] Add assertions that only `standard`, `aggressive`, and `bypass` are advertised and that `/mode conservative` is rejected.
- [x] Run the focused tests and confirm they fail against the four-mode implementation.

### Task 2: Remove the mode from runtime and UI surfaces

**Files:**
- Modify: `firstcoder/permissions/types.py`
- Modify: `firstcoder/app/permission_commands.py`
- Modify: `firstcoder/app/help_commands.py`
- Modify: `firstcoder/app/tui.py`

- [x] Remove `PermissionMode.CONSERVATIVE`.
- [x] Change mode listings to `standard, aggressive, bypass`.
- [x] Remove the obsolete TUI color entry.
- [x] Run the focused tests and confirm they pass.

### Task 3: Update permissions documentation and verify

**Files:**
- Modify: `docs/PERMISSIONS_DESIGN.md`
- Modify: `docs/PERMISSIONS_DESIGN.zh-CN.md`

- [x] Remove the conservative-mode descriptions and update mode counts/listings.
- [x] Search the repository for remaining `conservative` references.
- [x] Run the permission, help, TUI, and full pytest suites; report unrelated failures separately.
