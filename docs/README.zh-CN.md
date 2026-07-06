# 技术文档

这个目录保存 FirstCoder 的技术文档和阅读引导文档。

它主要服务两类需求：

- 理解系统是怎么工作的
- 找到一条合适的源码阅读路径

英文版入口见 [README.md](README.md)。

## 从这里开始

- [代码阅读指南](CODEBASE_READING_GUIDE.zh-CN.md)：推荐的文档和源码阅读顺序

## 核心设计文档

- [CLI / TUI 设计](CLI_TUI_DESIGN.zh-CN.md) / [English](CLI_TUI_DESIGN.md)：应用结构、运行时组装、状态流和终端交互模型
- [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.zh-CN.md) / [English](AGENT_LOOP_GUARDRAILS.md)：循环边界、验证边界和运行时安全规则
- [上下文管理设计](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md) / [English](CONTEXT_MANAGEMENT_DESIGN.md)：压缩层级、任务边界流程和上下文投影
- [权限系统设计](PERMISSIONS_DESIGN.zh-CN.md) / [English](PERMISSIONS_DESIGN.md)：审批模型、grant 和暂停 / 恢复行为
- [Providers 设计](PROVIDERS_DESIGN.zh-CN.md) / [English](PROVIDERS_DESIGN.md)：provider 抽象和适配器模型
- [技能系统设计](SKILL_SYSTEM_DESIGN.zh-CN.md) / [English](SKILL_SYSTEM_DESIGN.md)：skill 发现、路由、加载和审计行为
- [工具系统设计](TOOLS_DESIGN.zh-CN.md) / [English](TOOLS_DESIGN.md)：工具模型、registry 包装和权限耦合

## Benchmark 与 Runbook

- [本地 Pytest Benchmark](LOCAL_PYTEST_BENCHMARK.zh-CN.md) / [English](LOCAL_PYTEST_BENCHMARK.md)
- [SWE Bench Fast Runbook](SWE_BENCH_FAST_RUNBOOK.zh-CN.md) / [English](SWE_BENCH_FAST_RUNBOOK.md)
- [SWE Lite Runbook](SWE_LITE_RUNBOOK.zh-CN.md) / [English](SWE_LITE_RUNBOOK.md)
