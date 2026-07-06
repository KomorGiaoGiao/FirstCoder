# Technical Docs

This directory contains the technical and reader-facing documentation for FirstCoder.

It is organized around two needs:

- understanding how the system works
- finding a good path through the codebase

For Chinese readers, see [README.zh-CN.md](README.zh-CN.md).

## Start Here

- [Codebase Reading Guide](CODEBASE_READING_GUIDE.md): recommended order for reading the docs and source tree

## Core Design Docs

- [CLI / TUI Design](CLI_TUI_DESIGN.md) / [中文](CLI_TUI_DESIGN.zh-CN.md): app structure, runtime assembly, state flow, and terminal interaction model
- [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.md) / [中文](AGENT_LOOP_GUARDRAILS.zh-CN.md): loop limits, verification boundaries, and runtime safety rules
- [Context Management Design](CONTEXT_MANAGEMENT_DESIGN.md) / [中文](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md): compaction layers, task-boundary flow, and context projection
- [Permissions Design](PERMISSIONS_DESIGN.md) / [中文](PERMISSIONS_DESIGN.zh-CN.md): approval model, grants, and pause/resume behavior
- [Providers Design](PROVIDERS_DESIGN.md) / [中文](PROVIDERS_DESIGN.zh-CN.md): provider abstraction and adapter model
- [Skill System Design](SKILL_SYSTEM_DESIGN.md) / [中文](SKILL_SYSTEM_DESIGN.zh-CN.md): skill discovery, routing, loading, and audit behavior
- [Tools Design](TOOLS_DESIGN.md) / [中文](TOOLS_DESIGN.zh-CN.md): tool model, registry wrapping, and permission coupling

## Benchmarks And Runbooks

- [Local Pytest Benchmark](LOCAL_PYTEST_BENCHMARK.md) / [中文](LOCAL_PYTEST_BENCHMARK.zh-CN.md)
- [SWE Bench Fast Runbook](SWE_BENCH_FAST_RUNBOOK.md) / [中文](SWE_BENCH_FAST_RUNBOOK.zh-CN.md)
- [SWE Lite Runbook](SWE_LITE_RUNBOOK.md) / [中文](SWE_LITE_RUNBOOK.zh-CN.md)
