# FirstCoder 技术文档

这里是 FirstCoder 的实现说明书。它和仓库根目录 README 分工不同：README 告诉使用者怎样启动；本目录解释按下回车后系统实际做了什么、代码在哪、以及怎样验证改动没有“看着能跑，实际翻车”。

这套文档坚持一个原则：运行时结论必须落到真实实现边界。工具描述和 JSON Schema 通过 provider 请求的原生 `tools` 字段发送，不会复制塞进 system prompt；权限安全由程序侧代码保证，不是靠 prompt 里写一句“请谨慎”。

## 推荐学习路径

第一次读代码，按下面顺序走最省力：

1. [代码阅读指南](CODEBASE_READING_GUIDE.zh-CN.md)：先得到目录地图和一条完整执行链。
2. [CLI / TUI 设计](CLI_TUI_DESIGN.zh-CN.md)：理解启动、装配、命令、流式输出和界面状态。
3. [Agent 主循环护栏](AGENT_LOOP_GUARDRAILS.zh-CN.md)：理解一条用户消息如何变成模型调用与工具结果。
4. [工具设计](TOOLS_DESIGN.zh-CN.md) 与 [权限设计](PERMISSIONS_DESIGN.zh-CN.md)：理解模型的请求怎样变成受控的本地操作。
5. [上下文管理](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md)：理解会话事实、投影、压缩与任务边界。
6. [Provider 设计](PROVIDERS_DESIGN.zh-CN.md) 与 [Skill 系统设计](SKILL_SYSTEM_DESIGN.zh-CN.md)：理解两个主要扩展点。

每篇设计文档都提供可观察的小实验和相关测试。建议边读边开源码；目标不是背文件名，而是建立能实际排障的运行模型。

## 核心设计文档

| 想回答的问题 | 文档 |
| --- | --- |
| 终端应用怎样被装配和刷新？ | [CLI / TUI 设计](CLI_TUI_DESIGN.zh-CN.md) / [English](CLI_TUI_DESIGN.md) |
| 一轮任务何时停止、暂停、继续？ | [Agent 主循环护栏](AGENT_LOOP_GUARDRAILS.zh-CN.md) / [English](AGENT_LOOP_GUARDRAILS.md) |
| 长对话怎样放进模型上下文窗口？ | [上下文管理](CONTEXT_MANAGEMENT_DESIGN.zh-CN.md) / [English](CONTEXT_MANAGEMENT_DESIGN.md) |
| 为什么写文件、执行 shell 要确认？ | [权限设计](PERMISSIONS_DESIGN.zh-CN.md) / [English](PERMISSIONS_DESIGN.md) |
| 函数 schema 和本地执行器怎样对应？ | [工具设计](TOOLS_DESIGN.zh-CN.md) / [English](TOOLS_DESIGN.md) |
| 多家模型协议怎样被统一？ | [Provider 设计](PROVIDERS_DESIGN.zh-CN.md) / [English](PROVIDERS_DESIGN.md) |
| 本地 Skill 怎样发现、路由和安全加载？ | [Skill 系统设计](SKILL_SYSTEM_DESIGN.zh-CN.md) / [English](SKILL_SYSTEM_DESIGN.md) |
| 外部 MCP 工具怎样配置并经过权限控制？ | [MCP 客户端](MCP.zh-CN.md) / [English](MCP.md) |

## 评测与运行手册

这几篇是可操作流程，不是架构承诺。请从仓库根目录执行，并在相信分数前检查生成的运行产物。

- [本地 Pytest 基准](LOCAL_PYTEST_BENCHMARK.zh-CN.md) / [English](LOCAL_PYTEST_BENCHMARK.md)
- [SWE-bench 快速手册](SWE_BENCH_FAST_RUNBOOK.zh-CN.md) / [English](SWE_BENCH_FAST_RUNBOOK.md)
- [SWE-bench Lite 手册](SWE_LITE_RUNBOOK.zh-CN.md) / [English](SWE_LITE_RUNBOOK.md)

## 维护约定

改动运行时边界时，同一个 PR 里同步更新相应设计文档：写清新调用链、受影响状态、一个失败场景和聚焦测试命令。不要把“以后可能做”写成“已经可用”。一条准确的限制说明，比一篇精致的空气文档更有价值。
