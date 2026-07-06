# 代码阅读指南

这份指南适合想从外到内理解 FirstCoder 的读者。

## 推荐顺序

1. 先读 [../README.zh-CN.md](../README.zh-CN.md)，理解项目定位和快速开始方式。
2. 再读 [CLI / TUI 设计](CLI_TUI_DESIGN.md)，理解运行时如何组装、如何暴露给用户。
3. 再读 [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.md) 和 [工具系统设计](TOOLS_DESIGN.md)，理解主执行循环。
4. 再读 [权限系统设计](PERMISSIONS_DESIGN.md) 和 [上下文管理设计](CONTEXT_MANAGEMENT_DESIGN.md)，理解安全边界和长会话行为。
5. 最后读 [Providers 设计](PROVIDERS_DESIGN.md) 和 [技能系统设计](SKILL_SYSTEM_DESIGN.md)，理解扩展边界。

## 文档到源码的映射

读完上面的文档后，可以按下面的映射进入代码：

- `firstcoder/app/`：Textual TUI、命令路由和运行时组装
- `firstcoder/agent/`：agent loop、运行时编排和 turn 处理
- `firstcoder/tools/`：内置工具、schema 和工具执行流程
- `firstcoder/permissions/`：权限策略、审批和 grants
- `firstcoder/context/`：事件日志、投影、checkpoint 和压缩
- `firstcoder/session/`：session catalog、恢复流程、transcript 和分享
- `firstcoder/providers/`：provider 抽象和具体适配器
- `firstcoder/skills/`：skill 的发现、加载和路由

## 阅读建议

- 先看运行时组装，再分别看各个子系统，不要一开始就横跳所有模块。
- 一次读完一个子系统，比在多个目录之间来回切换更容易建立整体理解。
- 读源码时把对应设计文档一起打开，效果最好。
- benchmark 和 runbook 更适合在主架构看清楚之后再读。
