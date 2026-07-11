# 代码阅读指南

[English](CODEBASE_READING_GUIDE.md)

这不是目录背诵表，而是一条沿着真实运行系统读代码的路线。适用于你想回答“这个需求到底该改哪”而不想在相邻文件里玄学试错时。

## 先跟一轮真实任务

用“找出 loop limit 并解释”作为样例，调用链是：

```text
firstcoder/cli.py
  -> firstcoder/app/factory.py:create_firstcoder_app
  -> firstcoder/app/runtime.py:AgentChatRunner
  -> firstcoder/agent/loop.py:AgentLoop
  -> ContextBuilder 组装 ChatRequest(messages, tools)
  -> provider.complete/astream
  -> session.tool_registry.execute
  -> 事实追加写入 .firstcoder 的 session JSONL
  -> runtime 发送界面更新
```

这条链给出最重要的分工：loop 负责协调；provider 翻译模型协议；tool 做本地操作；context 保存并投影事实；TUI 只负责展示。不要在 loop 里直接调用厂商 SDK，也不要让 tool 执行器改 UI——那种“先跑起来再说”很容易变成祖传屎山。

## 模块地图

| 区域 | 先读 | 负责什么 | 不负责什么 |
| --- | --- | --- | --- |
| `app/` | `factory.py`、`runtime.py`、`tui.py` | 应用装配和终端展示 | 模型协议转换 |
| `agent/` | `loop.py`、`session.py`、`loop_limits.py` | 单轮编排、暂停与恢复 | shell/HTTP 的具体实现 |
| `context/` | `store.py`、`writer.py`、`context_builder.py` | 事实持久化和模型可见投影 | UI widget |
| `tools/` | `builtin.py`、`registry.py`、`session_registry.py` | schema、分发和会话包装 | 最终权限策略 |
| `permissions/` | `manager.py`、`policy.py`、`grants.py` | allow/ask/deny 决策与授权 | 真正执行工具 |
| `providers/` | `types.py`、`factory.py`、各 adapter | 内部格式到厂商协议的转换 | 会话存储 |
| `skills/` | `discovery.py`、`router.py`、`loader.py` | Skill 发现、路由与加载审计 | 注册工具 |
| `session/` | `catalog.py`、`resume.py`、`fork.py` | 会话发现与生命周期服务 | agent 轮次语义 |

## 四步读法

1. **先命名行为。** 用 `rg -n "关键词" firstcoder tests` 搜可见命令、工具名或错误，不要先猜文件。
2. **再找边界。** 顺着 import 找到接口（`ChatProvider`、`ToolRegistryLike`、数据类），然后才进入具体实现。
3. **跟数据走。** 重点看 `ChatRequest`、`ChatResponse`、`ToolCall`、`ToolResult`、session event。它们比单看控制流更能说明边界。
4. **紧接着读测试。** 异常路径和不变量通常在测试里说得最清楚；改前先跑最小相关测试。

## 常用入口命令

```sh
rg -n "class AgentLoop|def create_firstcoder_app" firstcoder tests
rg -n "ChatRequest\(|tool_registry\.execute|preflight\(" firstcoder tests
.venv/bin/python -m pytest tests -q
```

请用 `pytest tests`，不要在仓库根目录裸跑 `pytest`：基准运行产物里可能有自己无关的 `tests/`，会被误收集。

## 第一次改动怎样选层

- 新厂商参数放 `providers/` 和配置层，不要污染 loop。
- 新本地能力从一个 `Tool` 开始，并声明权限 spec；别在 `AgentLoop` 里写一堆特判。
- 审批规则改 `permissions/policy.py` 或 grant。
- 历史可见性改 context 的投影/压缩，不能破坏性删除原 event log。
- 新 slash command 放 `app/` 命令 handler，并复用 session 服务。

## 三个高频误解

**“system prompt 就是全部上下文。”** 不是。它是稳定前缀；`ContextBuilder` 会单独投影会话历史，工具 schema 在 `ChatRequest.tools`。

**“bypass 就是没有安全代码。”** 不是。它只是 policy mode；工具仍经 registry，并返回结构化结果。

**“压缩把历史删了。”** 不是。JSONL 事实还在；checkpoint 改的是本次 provider 看见的视图。

## 一个上手练习

打开 `firstcoder/agent/loop_limits.py`，先不改任何代码，用 `rg -n "max_tool_rounds|TOOL_ROUND_LIMIT" tests` 找到测试并运行，再沿 stop reason 回读 `loop.py`。这就是本项目推荐的工作方式：先说清一个小不变量、用测试确认、再只改拥有该职责的最小层。
