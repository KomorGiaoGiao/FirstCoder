# Skill System Design

[中文版本](SKILL_SYSTEM_DESIGN.zh-CN.md)

## What a Skill Is

A skill is a reusable, filesystem-backed instruction workflow. It is not an
executable plugin and it is not an arbitrary prompt fragment pasted without
trace. The system discovers candidates, routes deterministically, safely loads
the selected file and its declared supporting files, records audit events, then
adds the content to the stable prompt-prefix inputs.

## One Turn With a Skill

```text
user message + AGENTS.md
  -> discover_all_skills(project root and optional global roots)
  -> SkillRouter chooses explicit/AGENTS/metadata match
  -> SkillLoader validates root-relative paths and loads content
  -> append skill_selected / skill_loaded / required-file events
  -> system-prefix build receives loaded skill context
  -> provider sees the instructions for this turn
```

Routing is model-free. This makes the selection reproducible and avoids spending
another model call merely to choose a local instruction file.

## Discovery: Where Skills Come From

| Priority | Location | Source |
| ---: | --- | --- |
| 1 | `<project>/.agents/skills/*/SKILL.md` | project agent skill |
| 2 | `<project>/skills/*.md` | project markdown skill |
| 3 | `~/.agents/skills`, `~/.codex/skills`, `~/.firstcoder/skills` | global agent/markdown skill |
| 4 | `FIRSTCODER_SKILL_ROOTS` comma-separated roots | additional global roots |

`<project>/skills/INDEX.md` is catalog text, not a runnable skill. Set
`FIRSTCODER_DISABLE_GLOBAL_SKILLS=1` to keep discovery project-only. Frontmatter
may supply `name`, `description`, and comma-separated `triggers`; discovery is
sorted and deduplicated so repeated roots do not produce unstable catalogs.

## Core Data and Routing Rules

`SkillDefinition` identifies a candidate (name, path, source, root,
description, triggers). `SkillCatalog` contains candidates, index text, and a
fingerprint. `SkillRoutingDecision` records selection, candidates, reason, and
string confidence.

`SkillRouter` checks in strict order:

1. explicit name or path mentioned by the user;
2. meaningful overlap on an `AGENTS.md` line that references a skill path;
3. token overlap against skill name, description, and triggers.

An ambiguous metadata match deliberately selects nothing. When identical names
match, project sources win over global sources. That “no selection” outcome is
safer than silently loading unrelated instructions.

## Loading Is Root-Constrained

`SkillLoader` resolves the skill path relative to its registered root and
rejects any path escape. It can then extract required-file references under
headings such as “Required files”, “Must read”, or Chinese equivalents.
Required files are again resolved beneath the same root.

This is a containment rule, not a claim that skill content itself is trusted.
A skill can instruct the model, but it cannot use a required-file reference to
read arbitrary `../` paths.

## Audit, Resume, and Change Semantics

Session events record `skill_selected`, `skill_loaded`, and
`skill_required_file_loaded`. Loaded state is retained in runtime state and
replayed on resume. Resume reconstructs what was selected but rereads files that
still exist, so it is not a frozen content snapshot. If reproducibility across
skill edits is required, version the skill files in the project alongside the
session rather than assuming resume preserves old bytes.

## Add a Project Skill

1. Prefer `<project>/.agents/skills/<name>/SKILL.md` for a structured workflow
   or `<project>/skills/<name>.md` for a simple one.
2. Give it clear frontmatter description/triggers or an unambiguous heading.
3. Put required relative files under the same root and list them under a
   required-files heading.
4. Add an `AGENTS.md` route hint only when automatic routing is truly wanted.
5. Test discovery, explicit routing, ambiguity, and path-escape rejection.

```sh
.venv/bin/python -m pytest tests/test_skill_discovery.py tests/test_skill_router.py \
  tests/test_skill_loader.py tests/test_agent_skill_flow.py -q
```

## Debugging

| Symptom | Check |
| --- | --- |
| skill missing | root layout, disable-global flag, filename, and discovery catalog |
| wrong skill wins | explicit text first, then AGENTS line overlap, then metadata tie |
| skill selected but content absent | loader error/audit events and system-prefix inputs |
| required file unexpectedly readable | ensure it is root-relative; path traversal should fail |
| resumed session behaves differently | skill file changed after the original session |

Related: [Context Management](CONTEXT_MANAGEMENT_DESIGN.md) and
[Codebase Reading Guide](CODEBASE_READING_GUIDE.md).
