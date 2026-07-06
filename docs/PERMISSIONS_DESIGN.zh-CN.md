# 权限系统设计

[English Version](PERMISSIONS_DESIGN.md)

## 概述

权限层负责把“模型想做什么”和“程序允许做什么”分离开来。它不是一个被动的策略辅助器，而是工具执行控制流的一部分。

一次权限预检在真实运行时里会产生三种结果：

- 直接允许工具执行
- 直接拒绝，并返回一个合成的 tool result
- 暂停当前回合，等待用户确认

## 关键文件

- `firstcoder/permissions/types.py`：权限系统核心类型
- `firstcoder/permissions/policy.py`：默认策略实现
- `firstcoder/permissions/grants.py`：grant 存储
- `firstcoder/permissions/manager.py`：预检、确认请求构造、确认结果解析
- `firstcoder/tools/permission_registry.py`：tools 与 permissions 的集成入口
- `firstcoder/agent/session.py`：pending permission execution 状态
- `firstcoder/agent/loop.py`：loop 中的暂停 / 恢复逻辑

## 核心类型

主要类型定义在 `firstcoder/permissions/types.py` 中。

关键枚举和 dataclass 包括：

- `PermissionAction`
- `PermissionMode`
- `PermissionDecisionKind`
- `PermissionPersistence`
- `PermissionScopeType`
- `PermissionConfirmationChoice`
- `PermissionRequest`
- `PermissionGrant`
- `PermissionDecision`

这里最重要的区分是：

- `PermissionDecisionKind` 决定这次执行是 allow、deny 还是 ask
- `PermissionPersistence` 决定允许结果的生效范围有多长

## Permission Manager

`PermissionManager` 是程序侧统一入口。

它负责组合：

- 默认策略
- 持久化 grants
- 当前权限模式

`preflight(request)` 的真实流程是：

1. 先根据 project root 规范化请求
2. 检查是否命中持久化 grant
3. 如果没有命中 grant，则走默认策略

`build_confirmation(request)` 会把一个 `ASK` 结果转换成 UI 可展示的 `UserInputRequest`。

`resolve_confirmation(request, choice)` 则把用户回答解析成最终决策，并在需要时写入长期 grant。

## 权限模式

默认策略当前支持四种模式：

- `conservative`
- `standard`
- `aggressive`
- `bypass`

`bypass` 仍然是程序侧模式，而不是模型能力。模型并不会直接获得更多权限，只是程序在预检时变得更宽松。

## 默认策略

默认策略实现在 `firstcoder/permissions/policy.py`。

当前的关键规则包括：

- 读取普通项目内文件通常允许
- 项目内写入通常需要询问，除非是 aggressive 模式且工具元数据允许 auto 执行
- 删除项目根目录外路径会被拒绝
- 读取敏感环境变量会被拒绝
- 项目内只读 git 命令默认允许
- 含 shell 控制符的命令需要确认
- 网络请求默认需要确认

当前识别的敏感路径包括：

- `.git`
- `.env`
- `.pem`
- `.key`

## Grant 模型

长期 grant 通过 `PermissionGrant` 表示，并由 `PermissionGrantStore` 实现负责保存。

项目级运行时通常使用 `FilePermissionGrantStore`，把 grants 持久化到 session 数据目录下的 JSON 文件中。

grant 是按 scope 建模的，不是“随便记录一个已批准动作”。例如 `allow always` 会被转换成保守范围，例如：

- exact path
- command prefix
- host
- env key

这个 scope 由 `default_scope_for_request(...)` 计算出来。

## 与工具系统的集成

权限不是靠每个工具自己调用 manager 实现的，而是通过 `PermissionAwareToolRegistry` 统一执行。

真实执行路径是：

1. 对工具调用做 preflight
2. 根据工具的 `ToolPermissionSpec` 构造 `PermissionRequest`
3. 调用 `PermissionManager.preflight(...)`
4. wrapper 决定：
   - 执行工具
   - 返回拒绝结果
   - 返回结构化确认结果

也就是说，权限控制附着在 registry wrapper 上，而不是散落在每个 executor 里。

## 暂停与恢复

`ASK` 在 agent loop 里是一个真实暂停态。

当 loop 遇到 `ASK` 时：

1. 保存 `PendingPermissionExecution`
2. 向调用方返回 `pending_input`
3. UI 或交互式 CLI 收集用户回答
4. `resume_with_user_input(...)` 解析回答
5. 原始工具调用被继续执行，或者被转换成 denied result

这个设计很重要，因为 provider 可见消息序列必须保持合法。即使中间暂停，loop 也会保证 assistant tool call 最终能得到匹配的 tool result。

## 持久化与恢复行为

长期 grants 会被持久化，但 pending permission 状态并没有被建模成独立的权限事件流。

当前 resume 行为是从 assistant tool-call 历史尾部“未匹配的 tool call”中重建 pending permission execution。

也就是说，这一层的持久化边界是：

- grant 作为长期规则被持久化
- pending execution 尽量从会话事实中恢复

## 设计说明

- 权限是工具执行协议的一部分，而不只是一个 UI 弹窗层。
- manager 把 grants 和默认策略统一封装在一个 preflight 入口后面。
- `ASK` 会暂停当前回合，并恢复原始工具执行路径。
- 持久化 grants 采用基于 scope 的保守模型。
