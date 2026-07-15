# FirstCoder Technical Documentation

This directory is the implementation guide for FirstCoder. It is deliberately
different from the repository README: the README tells a user how to start the
application; these documents explain what happens after they press Enter, where
that behavior lives in source, and how to prove a change is correct.

The documentation follows one rule: a claim about runtime behavior must point
to its real implementation boundary. In particular, tool descriptions and JSON
schemas are sent in the provider request's native `tools` field, not copied into
the system prompt. Permission safety is enforced by program code, not by a
sentence in a prompt.

## A Learning Route

Read in this order if you are new to the codebase:

1. [Codebase Reading Guide](CODEBASE_READING_GUIDE.md) — a map and a first
   end-to-end trace.
2. [CLI / TUI Design](CLI_TUI_DESIGN.md) — process startup, dependency
   assembly, commands, streaming, and UI state.
3. [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.md) — the transaction that
   turns one user message into model calls and tool results.
4. [Tools Design](TOOLS_DESIGN.md) and [Permissions Design](PERMISSIONS_DESIGN.md)
   — how a model request becomes a controlled local operation.
5. [Context Management Design](CONTEXT_MANAGEMENT_DESIGN.md) — durable facts,
   provider projection, compaction, and task boundaries.
6. [Providers Design](PROVIDERS_DESIGN.md) and [Skill System Design](SKILL_SYSTEM_DESIGN.md)
   — the two main extension seams.

Each design document contains a runnable observation and links to relevant
tests. Read code with the document open; the goal is to build an executable
mental model, not memorize a directory tree.

## Core Design Documents

| Question | Document |
| --- | --- |
| How is the terminal app assembled and updated? | [CLI / TUI Design](CLI_TUI_DESIGN.md) / [中文](CLI_TUI_DESIGN.zh-CN.md) |
| When does a turn stop, pause, or continue? | [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.md) / [中文](AGENT_LOOP_GUARDRAILS.zh-CN.md) |
| How can long conversations fit a model context window? | [Context Management Design](CONTEXT_MANAGEMENT_DESIGN.md) / [中文](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md) |
| Why does a write or shell call need approval? | [Permissions Design](PERMISSIONS_DESIGN.md) / [中文](PERMISSIONS_DESIGN.zh-CN.md) |
| How are function schemas and executors connected? | [Tools Design](TOOLS_DESIGN.md) / [中文](TOOLS_DESIGN.zh-CN.md) |
| How are OpenAI-compatible and Anthropic protocols normalized? | [Providers Design](PROVIDERS_DESIGN.md) / [中文](PROVIDERS_DESIGN.zh-CN.md) |
| How are local skills found and safely loaded? | [Skill System Design](SKILL_SYSTEM_DESIGN.md) / [中文](SKILL_SYSTEM_DESIGN.zh-CN.md) |
| How are external MCP tools configured and permissioned? | [MCP Client](MCP.md) / [中文](MCP.zh-CN.md) |

## Evaluation And Operations

These are procedures, rather than architecture specifications. Run them from
the repository root and inspect their generated artifacts before trusting a
score.

- [Local Pytest Benchmark](LOCAL_PYTEST_BENCHMARK.md) / [中文](LOCAL_PYTEST_BENCHMARK.zh-CN.md)
- [SWE-bench Fast Runbook](SWE_BENCH_FAST_RUNBOOK.md) / [中文](SWE_BENCH_FAST_RUNBOOK.zh-CN.md)
- [SWE-bench Lite Runbook](SWE_LITE_RUNBOOK.md) / [中文](SWE_LITE_RUNBOOK.zh-CN.md)

## Documentation Maintenance

When changing a runtime boundary, update its design document in the same pull
request. Include: the new call path, affected state, one failure mode, and a
focused test command. Do not document speculative features as available. A
short accurate limitation is much more useful than polished fiction.
