# Skill System Design

[中文版本](SKILL_SYSTEM_DESIGN.zh-CN.md)

## Overview

The skill system is a deterministic instruction-loading layer for reusable workflows. A skill is not just a text prompt fragment. It is a discovered file, routed through explicit rules, optionally expanded with required files, and written into session events when loaded.

This gives FirstCoder a lightweight workflow-extension mechanism without turning skills into arbitrary executable plugins.

## Key Files

- `firstcoder/skills/models.py`: skill catalog and definition models
- `firstcoder/skills/discovery.py`: filesystem discovery
- `firstcoder/skills/router.py`: deterministic routing
- `firstcoder/skills/loader.py`: content loading and required-file extraction
- `firstcoder/skills/session.py`: session-event append helpers for skill audit
- `firstcoder/agent/loop.py`: turn-time routing and loading integration
- `firstcoder/agent/session.py`: loaded-skill retention in runtime state

## Skill Sources

Current discovery supports both project-local and machine-global skill roots.

Project-local sources:

- `<project>/skills/*.md`
- `<project>/.agents/skills/*/SKILL.md`

Global roots include:

- `~/.agents/skills`
- `~/.codex/skills`
- `~/.firstcoder/skills`
- extra roots from `FIRSTCODER_SKILL_ROOTS`

Global skill discovery can be disabled with `FIRSTCODER_DISABLE_GLOBAL_SKILLS`.

## Core Models

`SkillDefinition` currently includes:

- `name`
- `path`
- `source`
- `root`
- `description`
- `triggers`

`SkillCatalog` contains:

- discovered skills
- optional project `INDEX.md` content
- a computed fingerprint

The current implementation does not store numeric confidence on each skill definition. Confidence is produced later by routing.

## Discovery Model

Discovery is purely filesystem-driven.

Important behavior in `firstcoder/skills/discovery.py`:

- project `skills/INDEX.md` is read as catalog index content, not as a runnable skill
- markdown skills are discovered as `.md` files
- agent skills are discovered as nested `*/SKILL.md`
- frontmatter metadata can provide `name`, `description`, and `triggers`
- results are sorted and deduplicated deterministically

This keeps the catalog stable across runs and avoids accidental duplicates from overlapping roots.

## Routing Model

Routing is deterministic and currently model-free.

The router checks, in order:

1. explicit skill name or path mention in the user message
2. route-hint overlap from project instructions such as `AGENTS.md`
3. metadata token overlap using name, description, and triggers

The result is a routing decision that includes:

- the selected skill or `None`
- candidate list
- routing reason
- string confidence such as `high`, `medium`, or `none`

The runtime prefers project-local skills over global ones when several matches exist.

## Loading Model

If a skill is selected for the current turn, `AgentLoop` loads it before the provider request is built.

Loading behavior includes:

1. read the skill file
2. parse required-file references from the markdown body
3. load those required files if they stay within the skill root
4. append audit events to the session
5. inject the loaded content into the system-prefix build path

This makes skills part of prompt construction rather than a separate post-processing layer.

## Audit And Session Behavior

Skill-related audit events currently include:

- `skill_selected`
- `skill_loaded`
- `skill_required_file_loaded`

Loaded skills are retained in runtime state and also replayed during session resume.

Resume behavior is important:

- previously loaded skills are reconstructed from prior skill events
- then the current skill files are re-read from disk if still present

That means loaded-skill state is not a frozen snapshot of old file contents. It is partly reconstructed from the current filesystem.

## Priority Rules

The current effective priority order is:

1. project agent skill
2. project markdown skill
3. global agent skill
4. global markdown skill

This lets project-local workflows override broader machine-global defaults without changing the router itself.

## Design Notes

- Skills are discovered and routed deterministically, not by another model call.
- Required-file loading is content-driven and constrained to the skill root.
- Skills leave audit traces in the session log so prompt construction can be understood later.
- Resume prefers reconstructable workflow state over opaque hidden skill caches.
