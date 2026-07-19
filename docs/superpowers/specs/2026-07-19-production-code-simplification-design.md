# FirstCoder 生产代码安全精简设计

## 目标与统计口径

在不删除功能、不破坏公开 Python API、CLI/TUI 行为、配置格式和 benchmark 能力的前提下，减少 `firstcoder/` 下 Python 生产代码的净行数。

- 基线以本轮开始前的 `firstcoder/**/*.py` 总行数 25,616 行为准。
- 当前已通过合并 Anthropic catalog provider 构造路径减少 23 行。
- 测试、README、设计文档、benchmark 和构建产物不计入减行数字。
- 测试只作为行为保护，不为减少行数而删除。
- 约 1000 行是争取目标，不以删除功能、公开接口或引入晦涩元编程换取数字。

## 不变量

精简完成后必须保持以下边界：

1. `firstcoder` 各包现有 `__all__` 公开导出保持可导入。
2. CLI、Textual TUI、session create/resume/fork/share、权限确认、写前 diff、上下文压缩、skills、MCP、providers 与 benchmark 行为保持不变。
3. OpenAI-compatible Chat Completions 和 Anthropic Messages 仍是并行 provider 路径；不得用一个了解厂商全部协议的巨大解析器替代清晰适配器。
4. 同步入口和异步入口的返回类型、暂停/恢复语义、tool-call 顺序、取消、限制与压缩恢复行为保持不变。
5. 不删除仅因静态扫描或测试覆盖未命中而被怀疑的代码；CLI、SDK、Textual 回调和兼容入口必须由其真实调用契约判断。

## 推荐结构

### 1. Agent Loop 单一状态转换核心

同步与异步入口继续保留。将同步/异步工具循环中完全相同的“结果判定、取消收尾、assistant 落库、auto compact、pending input 包装”等状态转换集中到普通内部 helper；只有真正需要 `await` 的 provider 调用和工具执行保留两条薄适配路径。

禁止通过把同步 API 全部改为 `asyncio.run()` 来复用异步实现，因为现有同步 provider、线程并发与已经运行 event loop 的调用边界不同。目标是共享纯状态转换，不改变调度模型。

### 2. Provider 共享协议原语

把厂商无关且结构一致的原语放入 `firstcoder/providers/streaming.py`：

- 可从 SDK object 或 dict 读取字段；
- 合并 `TokenUsage`；
- 将已累积的 tool-call JSON 安全转为 `ToolCall`；
- 统一生成丢弃不完整 tool call 的 diagnostics。

OpenAI 和 Anthropic 仍各自拥有事件遍历、消息格式、finish reason 映射、参数构造和厂商错误处理。

### 3. Event 与 session 装配复用

为 `SessionEventWriter` 增加一个窄的 `_append_event(event_type, payload)` 内部入口，复用 event id、session id 和 store append 装配。Skill audit 事件通过 writer 的明确公共方法写入，不再绕过 writer 手写 `SessionEvent`。

Session create/resume/fork 服务只复用 bootstrap 参数构造和 record 状态校验；不引入继承层级，不合并三项业务本身。

### 4. Runtime、factory 与小型 helper 去重

- Runtime 抽取 turn 开始/结束后的共同 bookkeeping，但同步/异步执行函数保持显式。
- Model factory 抽取“从 profile 构造 provider 并应用到 runner/summarizer”的普通 helper。
- Context fingerprint、tool-call part、positive integer parser 等只在拥有同一语义时合并。
- TUI 已经有通用 interval timer helper；除非能减少状态字段或相同收尾逻辑，否则不继续抽象动画代码。

## 明确不采用的方案

- 不使用 decorator 或动态注册表批量生成工具实现。
- 不把工具函数合并成通过名称分派的万能执行器。
- 不删除 docstring、类型标注、错误信息或空行来人为制造主要减行数字。
- 不删除 eval、SWE-bench、视觉主题或其他低频功能。
- 不删除仓库内部暂未调用、但属于公开导出或明确兼容层的 API。

## 实施与验证

实施按四个独立批次进行：Agent Loop、providers、event/session、runtime/factory/helpers。每批遵循：

1. 找到已有行为测试；缺少保护时先增加测试，并用临时破坏或目标断言证明测试能失败。
2. 完成一个最小重构切片。
3. 运行该模块聚焦测试。
4. 运行 `.venv/bin/python -m pytest tests -q`。
5. 运行 `.venv/bin/python -m compileall -q firstcoder` 与 `git diff --check`。
6. 记录 `find firstcoder -name '*.py' -type f -print0 | xargs -0 wc -l` 和相对 25,616 行基线的净减少量。

最终验收不是“达到某个数字即完成”，而是所有计划内高价值重复均已评估或处理、完整测试通过、公开边界保留，并报告真实净减少量及未采用候选的原因。
