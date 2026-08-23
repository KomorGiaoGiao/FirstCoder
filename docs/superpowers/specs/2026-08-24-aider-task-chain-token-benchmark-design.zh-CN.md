# Aider 真实任务链 Token Benchmark 设计

## 目的

替换当前把同一句普通文本填到高水位的 `seed_old_task_context()` 压力测试。新的实验只在
`benchmark/task_boundary_compaction/` 中实现：它以一次真实执行的任务 A 会话为源，在三个
arm 中重放同一份 A 的完整会话事件，然后在隔离的任务 B 工作区执行 `new -> same` 两轮。
生产 `firstcoder/`、用户配置和上下文窗口均不修改。

## 固定题集

使用本机可读的 47 道 Java Aider Polyglot 题中的 32 道，且一题只出现一次：

| 链类型 | 条数 | A | B |
| --- | ---: | --- | --- |
| 自然切换 | 8 | 一道真实题，分为分析、实现、验证三个同任务用户轮 | 另一道独立真实题，分为解决、验证两个用户轮 |
| 批任务长链 | 4 | 三道真实题组成一次明确的“修复包”交付，九个同任务用户轮 | 一道独立真实题，两个用户轮 |

每条链的 A 录制一次，随后由相同的 session JSONL 复制出三个 B trial；A 的完整 provider
token 作为三个 arm 相同的固定基线列入结果，B 的实际调用 token 才是 arm 之间的差异。
不能把三次独立的模型生成当作相同 A，也不能把不相关题目伪标为 `same`。

## 运行与隔离

每道 Aider 题的 `environment/workspace/` 被复制到临时项目目录，题面来自 `instruction.md`。
真实验证使用该题的 Dockerfile 构建镜像，再以项目目录挂载为 `/app`、原始 `tests/` 挂载为
`/tests` 执行 `tests/test.sh`。B 总是获得新的工作区；A 的文件状态不能传给 B，只有 A 的
会话上下文可见。临时 session、项目目录与镜像标签都带 run/case 标识；项目和 session 在
trial 后删除，原始 Aider 数据不会修改。

## 三臂与有效性

`auto_only` 保留 AUTO、关闭分类；`classifier_only` 保留 AUTO、调用 Luna 分类但屏蔽
`TASK_HASH_CHANGED`；`full` 保留 AUTO、调用 Luna 并执行边界压缩。B 的首轮预期 `new`，
续轮预期 `same`；full 必须观察到一次有效任务边界压缩。分类、L4 与主模型的 token 都计入。

自然链不强行达到固定 token 水位：它回答真实普通任务切换是否节省 token。批任务链通过真实
代码、工具和测试输出形成更长上下文，但必须在每次 A 子题结束时仍低于 AUTO 高水位；若自然
AUTO 先发生，trial 标为 `confounded_auto`，不参与因果中位数。

## 报告

报告分别显示：A 固定基线、B 阶段 token、两者相加的全链路 token，以及有效完整配对的
`full - classifier_only`、`classifier_only - auto_only`、`full - auto_only` 中位数。缺失 usage、
provider 异常、边界缺失和 AUTO 混淆排除；verifier 失败保留为诊断，token-only 运行不据其宣称
质量收益。
