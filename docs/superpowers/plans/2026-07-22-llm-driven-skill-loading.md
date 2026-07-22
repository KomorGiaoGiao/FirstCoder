# LLM-Driven Skill Loading Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace FirstCoder's local automatic skill routing with a single LLM-driven `load_skill` tool while shrinking the system-prompt catalog and preserving session/tool/context boundaries.

**Architecture:** A resolved skill catalog becomes the single name-to-definition view used by the system prompt, TUI, and `load_skill`. The session-scoped tool safely loads one registered `SKILL.md`, emits audit events after success, and returns the body as an ordinary tool result so existing append-only persistence, resume, checkpoint, and archive behavior apply without a new message type.

**Tech Stack:** Python 3.11+, dataclasses, pytest, existing FirstCoder ToolRegistry/AgentSession/JsonlSessionStore/ContextBuilder.

---

## File map

- Create `firstcoder/skills/catalog.py`: resolve duplicate names and render the bounded model-visible catalog.
- Create `firstcoder/tools/load_skill.py`: session-scoped `load_skill(name, args?)` tool and audit orchestration.
- Modify `firstcoder/skills/models.py`: expose the resolved-catalog operation from `SkillCatalog` without changing discovery records.
- Modify `firstcoder/agent/session.py`: remove permanent loaded-skill state and build the compact catalog/protocol only.
- Modify `firstcoder/agent/loop.py`: remove local routing/loading before provider calls.
- Modify `firstcoder/tools/session_registry.py`: reserve and register `load_skill` with the live catalog/writer.
- Modify `firstcoder/session/bootstrap.py`: continue passing the discovered catalog through the existing AgentSession constructors.
- Modify `firstcoder/skills/session.py`: retain append-only audit writers and remove disk-based replay.
- Delete `firstcoder/skills/router.py`: remove the obsolete local keyword router.
- Modify `firstcoder/app/skill_commands.py` and `firstcoder/app/picker_adapters.py`: use resolved names and submit an explicit load instruction.
- Modify `firstcoder/context/system_prompt.py`, `firstcoder/agent/prompt_inputs.py`, and `firstcoder/context/versions.py`: remove loaded-skill system section and bump the prompt version.
- Update `docs/SKILL_SYSTEM_DESIGN.md` and `docs/SKILL_SYSTEM_DESIGN.zh-CN.md`: document the new single-path model.
- Tests: `tests/test_skill_discovery.py`, `tests/test_context_system_prompt.py`, `tests/test_agent_skill_flow.py`, `tests/test_skill_loader.py`, `tests/test_tools.py`, `tests/test_app_skill_commands.py`, `tests/test_app_tui.py`, `tests/test_context_versions.py`.

### Task 1: Resolve and bound the model-visible skill catalog

**Files:**
- Create: `firstcoder/skills/catalog.py`
- Modify: `firstcoder/skills/models.py`
- Test: `tests/test_skill_discovery.py`
- Test: `tests/test_context_system_prompt.py`

- [ ] **Step 1: Write failing catalog resolution tests**

Add tests constructing project/global definitions with the same name and asserting `catalog.resolved()` keeps the project definition. Add a rendering test asserting a catalog entry is `- review: Review code.` and does not contain `root=`, `SKILL.md`, source enums, or duplicate names. Add enough long descriptions to assert rendered text is at most 8,000 characters and contains no partial final line.

- [ ] **Step 2: Run tests and verify RED**

Run:

```sh
.venv/bin/python -m pytest tests/test_skill_discovery.py tests/test_context_system_prompt.py -q
```

Expected: FAIL because `SkillCatalog.resolved` and bounded catalog rendering do not exist and the old prompt exposes paths.

- [ ] **Step 3: Implement the catalog boundary**

Create constants `SKILL_CATALOG_MAX_CHARS = 8_000` and `SKILL_DESCRIPTION_MAX_CHARS = 240`. Implement source-priority/name resolution with stable root/path tie-breaking, whitespace-normalized descriptions, whole-line budget admission, and the instruction `Use load_skill(name, args?) to load full instructions when needed.` Expose `SkillCatalog.resolved()` returning a new catalog with the same index content and unique definitions.

- [ ] **Step 4: Use compact rendering in AgentSession**

Replace `_skill_catalog_summary()` with the shared renderer and change `_skill_protocol()` to state that the model must call `load_skill` before claiming to follow a skill. Do not include paths, roots, or sources.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 2: Add the session-scoped `load_skill` tool

**Files:**
- Create: `firstcoder/tools/load_skill.py`
- Modify: `firstcoder/tools/session_registry.py`
- Modify: `firstcoder/agent/session.py`
- Modify: `firstcoder/skills/session.py`
- Test: `tests/test_skill_loader.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing tool tests**

Create a temporary `review/SKILL.md`, a resolved catalog, store, and writer. Assert registry definitions contain `load_skill`; execution with `{"name": "review", "args": "check app.py"}` returns the full file plus a short name/args header and writes `skill_selected` then `skill_loaded`. Assert an unknown name and a deleted file return `ok=False` and write no skill events. Assert a supplied tool named `load_skill` is rejected as reserved.

- [ ] **Step 2: Run tests and verify RED**

```sh
.venv/bin/python -m pytest tests/test_skill_loader.py tests/test_tools.py -q
```

Expected: FAIL because `load_skill` is not registered.

- [ ] **Step 3: Implement `create_load_skill_tool`**

Look up only by exact registered name, call `SkillLoader.load`, and return a normal `ToolResult`. On success append `skill_selected` with reason `model_tool_call`/confidence `high`, then `skill_loaded`. Use `object_schema` with required string `name`, optional string `args`, and `additionalProperties=False`. Unknown names return a concise list of available names; loader exceptions return a safe failure.

- [ ] **Step 4: Register the tool through AgentSession**

Add `skill_catalog` to `create_session_tool_registry`, reserve `load_skill`, and register it only when store/writer are present. Pass the resolved session catalog from both AgentSession create and resume paths.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 3: Remove automatic routing and permanent prompt state

**Files:**
- Modify: `firstcoder/agent/loop.py`
- Modify: `firstcoder/agent/session.py`
- Modify: `firstcoder/skills/session.py`
- Modify: `firstcoder/context/system_prompt.py`
- Modify: `firstcoder/agent/prompt_inputs.py`
- Modify: `firstcoder/context/versions.py`
- Delete: `firstcoder/skills/router.py`
- Delete: `tests/test_skill_router.py`
- Rewrite: `tests/test_agent_skill_flow.py`
- Modify: `tests/test_context_system_prompt.py`
- Modify: `tests/test_context_versions.py`

- [ ] **Step 1: Write failing agent-flow tests**

Replace automatic-routing expectations with two behaviors: a normal user message causes no `skill_*` event and the first request contains only the compact catalog; a fake provider response calling `load_skill` causes the ordinary tool result to appear in the next request and audit events to be persisted. Add a resume test that deletes the original `SKILL.md` after the successful call and verifies the resumed provider still sees the historical tool result without disk replay.

- [ ] **Step 2: Run tests and verify RED**

```sh
.venv/bin/python -m pytest tests/test_agent_skill_flow.py tests/test_context_system_prompt.py tests/test_context_versions.py -q
```

Expected: FAIL because AgentLoop still auto-loads and session still injects loaded bodies.

- [ ] **Step 3: Remove automatic and permanent state**

Delete `_prepare_skills_for_current_turn`, its imports/turn flag/call sites, `AgentSession.loaded_skills`, `_loaded_skill_context`, and `replay_loaded_skills`. Remove `loaded_skill_context` from `SystemPromptInputs`, fingerprinting, and prompt construction. Delete the obsolete router module/tests. Bump `SYSTEM_PROMPT_VERSION` from `v13` to `v14`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 4: Align TUI commands with model-driven loading

**Files:**
- Modify: `firstcoder/app/skill_commands.py`
- Modify: `firstcoder/app/picker_adapters.py`
- Test: `tests/test_app_skill_commands.py`
- Test: `tests/test_app_tui.py`
- Test: `tests/test_app_factory.py`

- [ ] **Step 1: Write failing command tests**

Assert `/skills` lists each resolved name once, picker IDs are names, `/skill-use review` produces a reference instructing the model to call `load_skill` rather than mentioning a path, and `/review check app.py` submits text equivalent to `Use load_skill with name=review and args=check app.py before continuing.` Preserve `/skill review` detail inspection.

- [ ] **Step 2: Run tests and verify RED**

```sh
.venv/bin/python -m pytest tests/test_app_skill_commands.py tests/test_app_tui.py tests/test_app_factory.py -q
```

Expected: FAIL because picker IDs and submitted text are path-based.

- [ ] **Step 3: Implement resolved name-based commands**

Resolve the catalog once per handler operation, use skill names as action IDs, and generate explicit load instructions without directly reading files or mutating session state. Keep path/source visible only in `/skill <name>` detail output.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run the command from Step 2. Expected: PASS.

### Task 5: Verify ordinary context and compaction behavior

**Files:**
- Modify: `tests/test_agent_skill_flow.py`
- Modify: `tests/test_context_compaction_pipeline.py`
- Modify: `tests/test_context_resume.py`

- [ ] **Step 1: Write the context integration tests**

Persist a successful `load_skill` call/result sequence. Assert `ContextBuilder` projects it like any other assistant tool call/tool result, resume does not expand or reread it, and compaction can route/archive a sufficiently large historical skill result without a skill-specific branch.

- [ ] **Step 2: Run tests and verify behavior**

```sh
.venv/bin/python -m pytest tests/test_agent_skill_flow.py tests/test_context_compaction_pipeline.py tests/test_context_resume.py -q
```

Expected before any necessary adjustment: existing generic behavior may already pass. If a new test fails, the failure must identify a generic context integration gap; implement only the minimal generic fix and rerun until PASS.

### Task 6: Remove stale documentation and verify the repository

**Files:**
- Modify: `docs/SKILL_SYSTEM_DESIGN.md`
- Modify: `docs/SKILL_SYSTEM_DESIGN.zh-CN.md`
- Inspect: all `firstcoder/`, `tests/`, and `docs/` skill references

- [ ] **Step 1: Update design documentation**

Document the resolved compact catalog, LLM-owned selection, `load_skill` ordinary tool result, audit events, resume semantics, and deletion of automatic routing/permanent loaded state.

- [ ] **Step 2: Scan for obsolete code and wording**

```sh
rg -n "SkillRouter|replay_loaded_skills|loaded_skill_context|loaded_skills|自动.*skill|auto.*skill" firstcoder tests docs
```

Expected: no production references to removed mechanisms; historical design/plan references may remain only when explicitly marked historical.

- [ ] **Step 3: Run the complete focused skill suite**

```sh
.venv/bin/python -m pytest tests/test_skill_discovery.py tests/test_skill_loader.py tests/test_agent_skill_flow.py tests/test_context_system_prompt.py tests/test_app_skill_commands.py tests/test_app_tui.py tests/test_tools.py tests/test_context_compaction_pipeline.py tests/test_context_resume.py -q
```

Expected: PASS.

- [ ] **Step 4: Run full tests and formatting checks**

```sh
.venv/bin/python -m pytest tests
git diff --check
```

Expected: all tests pass and `git diff --check` produces no output.

- [ ] **Step 5: Review the final diff without touching unrelated work**

```sh
git status --short
git diff --stat
git diff -- firstcoder/skills firstcoder/tools/load_skill.py firstcoder/tools/session_registry.py firstcoder/agent/session.py firstcoder/agent/loop.py firstcoder/context firstcoder/app/skill_commands.py firstcoder/app/picker_adapters.py tests docs/SKILL_SYSTEM_DESIGN.md docs/SKILL_SYSTEM_DESIGN.zh-CN.md
```

Expected: task-list files that were already dirty remain preserved and are not folded into skill-system changes.
