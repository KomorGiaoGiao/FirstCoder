# 本地 Pytest 基准

[English](LOCAL_PYTEST_BENCHMARK.md)

## 它测什么

这是 FirstCoder 的快速本地 coding-agent 探针。JSONL 的每一行会生成一个干净的小 Git 仓库；agent 接题、修改仓库，runner 执行该题声明的 pytest 命令。它测的是“读代码、改代码、跑测试、收尾”的真实闭环，不是只测模型能不能说出一段像样的话。

它不是 SWE-bench 榜单替代品：题目小且本地化，runner 不模拟真实大仓库的全部条件。

## 前置条件

- 已安装仓库依赖，并使用本仓库虚拟环境；
- 已通过项目配置/环境变量配置 FirstCoder provider；
- 运行 benchmark 的 Python 能找到 `git` 和 pytest；
- 使用可丢弃 output 目录，因为 runner 会创建任务仓库。

例如：

```sh
export FIRSTCODER_PROVIDER=openai
export FIRSTCODER_BASE_URL=...
export FIRSTCODER_API_KEY=...
export FIRSTCODER_MODEL=...
```

不要提交凭证，也不要提交生成的 `runs/` 产物。

## 最小真实运行

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

若 workdir 已有同名 task，只有确认要抛弃那份生成仓库时才加 `--force`：

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1 --force
```

所有选中任务通过时进程退出 `0`；退出 `2` 表示 runner 正常结束，但至少一个 pytest 失败。

## 题目契约

默认数据集为 `benchmark/local_pytest/tasks.sample.jsonl`，每行形状：

```json
{
  "id": "string_normalize",
  "title": "Normalize Usernames",
  "files": {"src/text_tools.py": "...", "tests/test_text_tools.py": "..."},
  "problem_statement": "Fix src/text_tools.py ...",
  "test_command": "python -m pytest -q"
}
```

`id` 是生成目录名。每个 `files` 路径都会检查不越出该目录。runner 初始化 Git 仓库，并记录 agent 的最终 diff（含 untracked 文件），所以即使测试失败，patch 证据还在。

## 必看的输出

summary 对每题包含：

| 字段 | 为什么看 |
| --- | --- |
| `passed`、`returncode`、`pytest_output` | 真正评分证据 |
| `repo_path`、`test_command` | 可本地复现 evaluator |
| `transcript_path`、`raw_response` | 排 agent/loop 行为 |
| `model_patch` | 看到实际上改了什么 |
| `elapsed_seconds` | 发现性能回归或卡住 |

别根据 agent 的一句“已修复”就报成功。打开 summary，在 `repo_path` 重跑记录的命令，结果离谱时看 `model_patch`——这才是 benchmark 的正确打开方式。

## 常用选项

| 选项 | 作用 |
| --- | --- |
| `--tasks PATH` | 使用其他 JSONL 题集 |
| `--max-tasks N` | 只取前 N 题 |
| `--provider NAME` / `--model-name NAME` | 配置/标记 adapter |
| `--session-root PATH` | 与普通 session 分开保存 benchmark 日志 |
| `--force` | 重建已有生成仓库 |

## 失败怎么排

1. 先读 `pytest_output`，分清 agent 失败还是题目 fixture 本身坏了。
2. `cd repo_path`，亲自跑记录的 `test_command`。
3. 看 `model_patch` 和 transcript：改对文件了吗？跑测试了吗？卡在权限/tool 失败了吗？
4. 用新 workdir/session root 重跑单题，再动 agent。
5. 将发现的问题落成 focused unit/integration test。

## Runner 自身验证

```sh
.venv/bin/python -m pytest tests/test_local_pytest_benchmark.py tests/test_eval_adapter.py -q
```

关联：[SWE-bench Lite 手册](SWE_LITE_RUNBOOK.zh-CN.md)，用于更接近真实仓库的外部评测。
