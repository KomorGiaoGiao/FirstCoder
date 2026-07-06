# Codebase Reading Guide

This guide is for readers who want to understand FirstCoder from the outside in.

## Recommended Order

1. Read [../README.md](../README.md) for the project-level positioning and quickstart.
2. Read [CLI / TUI Design](CLI_TUI_DESIGN.md) to understand how the runtime is assembled and exposed to the user.
3. Read [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.md) and [Tools Design](TOOLS_DESIGN.md) to understand the main execution loop.
4. Read [Permissions Design](PERMISSIONS_DESIGN.md) and [Context Management Design](CONTEXT_MANAGEMENT_DESIGN.md) for safety and long-session behavior.
5. Read [Providers Design](PROVIDERS_DESIGN.md) and [Skill System Design](SKILL_SYSTEM_DESIGN.md) for extension boundaries.

## Source Tree Mapping

After reading the docs above, use this mapping to move into the code:

- `firstcoder/app/`: Textual TUI, command routing, and runtime assembly
- `firstcoder/agent/`: agent loop, runtime orchestration, and turn handling
- `firstcoder/tools/`: built-in tools, schemas, and tool execution flow
- `firstcoder/permissions/`: permission policies, approvals, and grants
- `firstcoder/context/`: event log, projection, checkpointing, and compaction
- `firstcoder/session/`: session catalog, resume flow, transcript, and sharing
- `firstcoder/providers/`: provider abstraction and concrete adapters
- `firstcoder/skills/`: skill discovery, loading, and routing

## Reading Tips

- Start with runtime assembly before reading individual subsystems in isolation.
- Read one subsystem end to end instead of jumping across all modules at once.
- Keep the matching design doc open while reading the implementation.
- Use the benchmark and runbook docs only after the main architecture is clear.
