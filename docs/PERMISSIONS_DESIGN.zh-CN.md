# 权限设计

[English](PERMISSIONS_DESIGN.md)

## 权限真正保证什么

权限在敏感工具操作前回答一个程序侧问题：**这个具体请求现在能不能执行？** 它不依赖模型“自觉”。真实执行链是：

```text
ToolPermissionSpec -> PermissionRequest -> PermissionManager
  -> 匹配 grant 或 DefaultPermissionPolicy
  -> ALLOW | ASK | DENY
  -> PermissionAwareToolRegistry 采取动作
```

registry executor 只会在 `ALLOW` 后进入；直接文件修改还有一道程序侧边界：`ToolExecutor` 会在真正调度前构造可信的写前 review。做安全 review 时应该盯住这些边界，而不是 system prompt 里写得多严肃。

## 具体例子：写文件

1. 模型请求 `write(path, content)`。
2. permission-aware registry 用 write 的 spec 构造 request，含 action、规范化 target、cwd、policy hint。
3. `PermissionManager.preflight` 先匹配 grant，未命中才按当前 mode 进入默认 policy。
4. `DENY` 变 tool result；`ASK` 会展示可信 diff 和结构化 `UserInputRequest`（定义在 `firstcoder.runtime.user_input`），turn 暂停。支持的直接文件修改即使得到 `ALLOW`，仍会暂停一次，等待只用于 review 的 Apply 确认。
5. 用户回答后，`resolve_confirmation` 会重验保存的文件快照，再执行原 pending call 或写 denied result。

模型只会看到最后的 tool message；它不能越过 registry 执行工具，也不能自己写 grant 文件。

## 数据模型

`permissions/types.py` 是词汇表：

| 类型 | 作用 |
| --- | --- |
| `PermissionAction` | filesystem、shell、network、env 等动作分类 |
| `PermissionRequest` | 一次具体 target/action 的决策输入 |
| `PermissionDecisionKind` | 当前请求的 `ALLOW`、`ASK`、`DENY` |
| `PermissionPersistence` | 批准是一次性的还是长期的 |
| `PermissionGrant` | 可持久化、有范围的 allow 规则 |
| `PermissionScopeType` | exact path、command prefix、host、env key 等范围 |
| `PermissionMode` | standard、aggressive、bypass |

不要混淆 decision 和 persistence：“本次允许”是 allow + 短期持久性；“始终允许”只有当前请求支持时才会生成有 scope 的 grant。

## Policy、Mode 与 Grant

`DefaultPermissionPolicy` 在未匹配 grant 时给出兜底决策。它会看目标：项目内普通读取通常比外部删除、读取敏感环境变量、含控制操作符的 shell 安全。完整规则以 `permissions/policy.py` 为准；调用方别复制半套逻辑，直接加测试。

Mode 影响 policy：

- `standard`：常规项目模式；
- `aggressive`：更容易允许被标记为可自动执行的动作；
- `bypass`：最宽松的 policy mode。

`bypass` 不是删除代码路径。请求仍会规范化、registry 仍会 dispatch、结果仍会记录，真正规则仍由 policy/hard check 定义。把它当成明确工作模式，不要当成模型偷偷开挂。

### 可信写前 review

`write`、`edit`、`apply_patch`、`delete` 会在 executor 运行前被 review；`shell` 刻意不做，因为无法安全预计算任意命令的影响。review 从原始 `ToolCall` 构造、保存预期文件快照，并把有界 unified diff 交给 UI；UI 只能回传 request id 和选择，不能换一份要执行的调用 payload。

| Mode / decision | 直接文件修改行为 |
| --- | --- |
| standard + `ASK` | 可信 diff + 常规权限确认；批准后执行 |
| aggressive 或匹配 grant + `ALLOW` | 可信 diff + 仅 review 的 Apply；不会新增长期 grant |
| bypass | 发出非阻塞 `prewrite_review` 事件，然后立即执行 |
| benchmark adapter | 非交互运行可显式设 `require_prewrite_review = False` |

暂停后的实际执行前会再次校验保存的快照。预览过期会被阻止，而不是覆盖外部并发修改；这能降低误覆盖风险，但不是文件系统级原子事务。

`FilePermissionGrantStore` 将 grant 存到 data root 的 `permissions.json`。`allow always` 会通过 `default_scope_for_request` 算出 scope，不会保存成无限制的自然语言“以后都行”。

## 共享请求类型归属

`UserInputRequest` / `UserInputOption` 定义在 `firstcoder.runtime.user_input`，这样 `permissions`、`tools` 和 UI 可以共享它们，**不必** import `firstcoder.agent`。`agent.user_input` 只保留 agent turn 的结果类型。

## 暂停、恢复与回放

`ASK` 必须保留 assistant 的原始 tool call。`AgentSession` 保存 `PendingPermissionExecution`，交互调用者得到 `pending_input`；恢复时 loop 解析选择并完成同一条 tool-call transaction，provider 序列因此合法。

长期 grant 和 pending call 生命周期不同：grant 是权限数据；pending action 在可能时从 resume 的未匹配 assistant tool-call 历史恢复，不另起一套平行对话日志。

## 可验证练习

```sh
.venv/bin/python -m pytest tests/test_permissions_policy.py \
  tests/test_permissions_manager.py tests/test_permissions_grants.py \
  tests/test_permission_registry.py tests/test_permission_commands.py \
  tests/test_prewrite_review.py tests/test_review_view.py -q
```

重点读 `tests/test_permission_registry.py`：它证明 deny 或 ask 时 executor 不会被调用，只有正确 resume 才能进入执行。

## 排障清单

| 现象 | 检查 |
| --- | --- |
| 不该弹窗却弹了 | tool spec 派生的 action/target，再看 policy mode |
| 已永久授权却无效 | grant 的 scope 规范化与 grant store 位置 |
| 用户允许后未执行 | pending execution id 与 resume 调用 |
| review 无法恢复 | 原始调用仍应 pending，且文件快照未被外部修改 |
| 危险动作直接跑了 | 是否实际安装 wrapper、tool 是否有 permission spec |
| permission result 导致 provider 历史报错 | 是否追加了配对 tool call id |

## 改动规则

动作分类规则放 `policy.py`，scope 算法放 manager/types，工具专属 target 提取放 `ToolPermissionSpec`。不要让每个工具都手搓确认弹窗。回归测试至少覆盖预期 allow 和最近的危险邻居。

关联：[架构说明](ARCHITECTURE.zh-CN.md)、[工具设计](TOOLS_DESIGN.zh-CN.md)、[Agent 主循环护栏](AGENT_LOOP_GUARDRAILS.zh-CN.md)。
