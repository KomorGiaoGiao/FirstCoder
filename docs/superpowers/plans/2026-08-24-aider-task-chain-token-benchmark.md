# Aider 真实任务链 Token Benchmark 实施计划

> **面向执行型 Agent：** REQUIRED SUB-SKILL: Use `superpowers:executing-plans` to implement this plan task-by-task. 步骤以复选框记录。

**目标：** 使用 12 条真实 Aider 任务链取代字符串 seed，并对三臂运行可比较的 token-only 测试。

**架构：** 新增 Aider 任务适配器和链 manifest；A 先录制到临时 JSONL，再复制并 resume 到每个独立 B trial。Docker verifier 只读原始题目数据，B project 与 A project 永远不同。

**技术栈：** Python 3.12、pytest、现有 AgentSession/JsonlSessionStore、Docker、Aider Polyglot 本地数据。

---

### Task 1：定义 Aider 任务与任务链 schema

**Files:**
- Modify: `benchmark/task_boundary_compaction/models.py`
- Modify: `benchmark/task_boundary_compaction/cases.py`
- Create: `benchmark/task_boundary_compaction/fixtures/aider_chain_cases.json`
- Test: `tests/test_task_boundary_compaction_cases.py`

- [ ] 先为 `AiderTask`、`TaskChainCase` 写失败测试：拒绝非 Aider 根目录、重复题、空 A/B、B 与 A 重叠；断言每条 B 恰有 `new -> same`。
- [ ] 加载 manifest，解析 `instruction.md`、workspace、tests 与 Dockerfile，不读取 solution；materialize 单题及多题 batch 到新目录。
- [ ] 运行 `pytest tests/test_task_boundary_compaction_cases.py -q` 并提交。

### Task 2：录制与重放真实任务 A

**Files:**
- Create: `benchmark/task_boundary_compaction/chain.py`
- Modify: `benchmark/task_boundary_compaction/runner.py`
- Test: `tests/test_task_boundary_compaction_chain.py`

- [ ] 先写失败测试：用 fake provider 录制三轮 A，复制 store 后以新的 B 工具 registry `AgentSession.resume()`；断言原 A 消息/工具结果保留、active hash 保留、B 没有继承 A 项目文件。
- [ ] 实现 capture：A 的全部用户轮运行于一个项目；复制 `data/` 到每个 arm 后 resume，不产生第二次 A provider 请求。
- [ ] 运行链测试和既有 runner 测试。

### Task 3：接入 Docker verifier 与真实 12 条 manifest

**Files:**
- Modify: `benchmark/task_boundary_compaction/runner.py`
- Modify: `benchmark/task_boundary_compaction/README.md`
- Test: `tests/test_task_boundary_compaction_runner.py`

- [ ] 先写失败测试：验证 docker 命令将 B project 挂载 `/app`、题目的 tests 挂载 `/tests`，且镜像标签只含安全 run/case 标识。
- [ ] 实现超时、stdout/stderr 哈希和始终清理 container；增加 `--suite aider-chain`、`--aider-root`。
- [ ] 写入 8 条自然链与 4 条 3 子题批链，所有 32 道题唯一；更新 README 的命令及适用边界。
- [ ] 跑全部 benchmark 单元测试。

### Task 4：验证和真实 token-only 运行

**Files:**
- Output: `benchmark/runs/task-boundary-compaction/aider-chain-luna-<run-id>/summary/summary.json`

- [ ] 运行 benchmark 定向测试；运行完整 pytest。
- [ ] 对 12 条链、三 arm 用 Terra 主模型与 Luna 分类器运行，检查每个 full 正例的边界事件和 provider usage 完整性。
- [ ] 以 report 汇总完全配对 token 差，报告自然链和批链的分层结果；不将 verifier 失败写成质量结论。
