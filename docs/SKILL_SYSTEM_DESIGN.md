# Skill System Design

[中文版本](SKILL_SYSTEM_DESIGN.zh-CN.md)

## What a Skill Is

A skill is a reusable, filesystem-backed workflow for the model. It is not an executable plugin or a hidden rule automatically selected from user text. FirstCoder discovers, indexes, and safely loads files; the LLM decides whether to call `load_skill` from a compact catalog.

## One Turn With a Skill

```text
SessionBootstrap discovers project and global skills
  -> resolve one effective definition per name (project before global)
  -> system prompt receives only name + short one-line description
  -> user message enters the normal agent loop
  -> LLM decides a skill is needed
  -> LLM calls load_skill(name, args?)
  -> SkillLoader validates the registered root-relative path and reads SKILL.md
  -> append skill_selected / skill_loaded after success
  -> return the full body as an ordinary append-only tool_result
  -> LLM follows it with existing view/read_multi/shell tools
```

There is no local keyword router and no permanent `session.loaded_skills` system-prompt state. Skill bodies do not enter a request until `load_skill` is actually called.

## Discovery and Effective Catalog

| Priority | Location | Source |
| ---: | --- | --- |
| 1 | `<project>/.agents/skills/*/SKILL.md` | project agent skill |
| 2 | `<project>/skills/*.md` | project markdown skill |
| 3 | `~/.agents/skills`, `~/.codex/skills`, `~/.firstcoder/skills` | global agent/markdown skill |
| 4 | `FIRSTCODER_SKILL_ROOTS` comma-separated roots | additional global roots |

`<project>/skills/INDEX.md` remains catalog documentation, not a callable skill. `FIRSTCODER_DISABLE_GLOBAL_SKILLS=1` disables global discovery.

Discovery retains source records. Runtime consumers resolve one definition per `name` using the priority above and stable root/path ordering for ties. The system prompt, TUI, and `load_skill` therefore share one effective view.

## Model-Visible Catalog

The model sees only entries like:

```text
- code-review: Review code correctness and maintainability.
- pdf: Read and transform PDF documents.
Use load_skill(name, args?) to load full instructions when needed.
```

It contains no filesystem path, root, source enum, or duplicate name. Descriptions are normalized to one bounded line. The complete listing is capped at 8,000 characters and admits only whole entries.

## `load_skill` Tool

```text
load_skill(name: string, args?: string)
```

- `name` must exactly match the effective catalog and is never a path.
- `args` carries task-specific arguments and never participates in lookup.
- The registered root/path still passes through `SkillLoader` containment checks.
- The result contains the complete `SKILL.md` and parsed required-file path metadata.
- Referenced files are not expanded automatically; the model uses existing read tools on demand.
- Unknown names, missing files, and read failures return ordinary tool errors and emit no successful audit events.

`load_skill` is a reserved session-scoped tool and cannot be overridden by supplied tools.

## Audit, Resume, and Compaction

A successful call records `skill_selected` and `skill_loaded`. The full body is also persisted as the normal tool result, so:

- resume replays the exact body returned at the time and does not reread the current file;
- providers receive the standard assistant tool-call -> tool-result sequence;
- checkpoints and tool-result archival can use generic context rules;
- repeated loads are explicit facts rather than hidden permanent state.

Old skill audit events remain in historical JSONL but are not restored into the system prompt.

## TUI Commands

- `/skills` opens the resolved skill picker.
- `/skill <name>` shows internal details, including path/source, outside the model prompt.
- `/skill-use <name>` prepares an explicit instruction for the model to call `load_skill`.
- `/<skill-name> <instruction>` passes the instruction as args and asks the model to load first.

Commands never read the file or mutate skill state directly. Formal loading always follows the model tool-call path.

## Add a Project Skill

1. Put structured workflows in `<project>/.agents/skills/<name>/SKILL.md`; simple workflows may use `<project>/skills/<name>.md`.
2. Give frontmatter a short, distinctive `name` and `description`.
3. State when to use it, steps, verification requirements, and on-demand references/scripts/assets in the body.
4. Do not depend on Python keyword matching; the description is the LLM's primary selection index.
5. Test discovery, duplicate priority, `load_skill` success/failure, resume of tool results, and path containment.

```sh
.venv/bin/python -m pytest tests/test_skill_discovery.py tests/test_skill_loader.py \
  tests/test_agent_skill_flow.py tests/test_context_system_prompt.py -q
```

## Debugging

| Symptom | Check |
| --- | --- |
| model cannot see a skill | directory layout, disable-global flag, frontmatter name/description, 8,000-character budget |
| wrong duplicate wins | project/global source priority and stable root/path tie order |
| model does not call a skill | catalog description quality or explicit `/<skill-name>` invocation |
| `load_skill` fails | exact name, file existence, and registered path containment |
| body changes after resume | inspect the historical tool result; resume does not reread the skill file |

Related: [Architecture](ARCHITECTURE.md), [Context Management](CONTEXT_MANAGEMENT_DESIGN.md), and [Codebase Reading Guide](CODEBASE_READING_GUIDE.md).
