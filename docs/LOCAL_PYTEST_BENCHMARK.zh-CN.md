# 本地 Pytest Benchmark

[English Version](LOCAL_PYTEST_BENCHMARK.md)

这是一个给 FirstCoder 使用的轻量级 coding-agent benchmark。它不会像 SWE-bench 那样拉 Docker 镜像，而是把每道题生成为一个本地小型 Python 仓库，让 FirstCoder 改代码，再通过本地 `pytest` 判分。

## 适用场景

它适合当前这个阶段的本地验证：

- 快速观察 agent loop 会不会读题、找文件、改代码、跑测试并停止
- 不依赖 Docker，也不依赖远端镜像
- 方便自己追加题目，用较低成本反复调试提示词、工具循环和权限路径

## 运行前准备

先确保 provider 相关环境变量已经配置好，例如：

```sh
export FIRSTCODER_PROVIDER=openai
export FIRSTCODER_BASE_URL=...
export FIRSTCODER_API_KEY=...
export FIRSTCODER_MODEL=...
```

## 运行样例

```sh
.venv/bin/python benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

如果你使用的是 Codex bundled Python，也可以替换成对应解释器路径：

```sh
/Users/x/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3 \
  benchmark/local_pytest/runner.py \
  --workdir runs/local-pytest-smoke \
  --summary-out runs/local-pytest-smoke-summary.json \
  --max-tasks 1
```

## 题目格式

题库使用 JSONL，每行一道题。样例格式如下：

```json
{
  "id": "string_normalize",
  "title": "Normalize Usernames",
  "files": {
    "src/text_tools.py": "def normalize_username(value: str) -> str:\n    return value.strip()\n",
    "tests/test_text_tools.py": "from src.text_tools import normalize_username\n..."
  },
  "problem_statement": "Fix src/text_tools.py ...",
  "test_command": "python -m pytest -q"
}
```

默认样例文件在：

```text
benchmark/local_pytest/tasks.sample.jsonl
```

## 输出结果

summary 文件会记录：

- 题目 id 和标题
- 题目仓库路径
- pytest 是否通过
- FirstCoder transcript 路径
- 生成的 git diff
- 总耗时和 pytest 输出

这不是排行榜型 benchmark，更适合做本地调试探针。通常建议先把这个 benchmark 跑顺，再把相同的 loop 放到更重的 SWE-bench Lite 或 `swe-bench-fast` 评估里。
