# Harbor Evaluation

## What Harbor is

Harbor is an external evaluation runtime for coding agents. A Harbor dataset is
a collection of tasks. Each task provides an instruction, an isolated execution
environment, and a verifier. Harbor resolves the dataset, starts the task
environment, runs the selected agent, invokes the verifier after the agent
exits, and records job and trial artifacts.

FirstCoder deliberately does not implement dataset-specific runners. Harbor is
the only benchmark integration maintained by this repository.

## How FirstCoder participates

`benchmark.harbor.firstcoder_agent:FirstCoderHarborAgent` is an installed-agent
adapter. For each task it stages only `pyproject.toml`, `README.md`, and
`firstcoder/`, creates an isolated agent virtual environment, and runs one
non-interactive `firstcoder --benchmark` turn in Harbor's task directory.

The adapter does not copy `.git`, `.venv`, local sessions, `.env`, or other
workspace files. It receives the task instruction but does not inspect verifier
files or inject hidden-test information into the prompt.

## Aider Polyglot feedback mode

The upstream Aider Polyglot benchmark permits one repair turn after the first
test run fails. For an Aider-comparable local run, opt in to the benchmark-only
plugin below. It keeps the first agent turn blind to verifier files, and only
after a real `reward=0` sends the verifier's test output back through the same
FirstCoder session. Timeouts, missing reward files, and provider failures do
not receive a repair turn.

For a long-running local suite, classify infrastructure failures separately from `reward=0`: network/provider errors, Docker environment failures, timeouts, and a verifier that fails before writing `reward.txt` do not carry the same interpretation as an implementation failing its tests. The run report documents examples and recovery commands.

```sh
PYTHONPATH="$PWD" .venv/bin/harbor run \
  -p .local/harbor-datasets/aider-polyglot \
  -a benchmark.harbor.firstcoder_agent:FirstCoderHarborAgent \
  --plugin benchmark.harbor.aider_feedback_plugin:AiderFeedbackPlugin \
  -m gpt-5.6-luna -n 2 -k 1 \
  --ak max_tool_rounds=120 --ak reasoning_effort=high \
  -o benchmark/runs/harbor/aider-polyglot-feedback -y
```

Do not use this plugin for Terminal-Bench or any benchmark whose official
protocol does not explicitly allow test-feedback repair rounds.

## Install Harbor

Install Harbor in FirstCoder's development environment:

```sh
.venv/bin/python -m pip install 'harbor==0.18.0'
```

Verify the CLI and Docker daemon before running a task:

```sh
.venv/bin/harbor --version
docker version
```

## Datasets

Browse published datasets at [Harbor Hub](https://hub.harborframework.com/datasets).
Download a dataset into Harbor's local cache when you want to inspect its task
names and environment definitions:

```sh
.venv/bin/harbor dataset download DATASET_NAME --cache
```

The dataset name, task filter, image architecture, and resource requirements are
part of a reproducible run. Inspect them before starting a large job.

## Run one task

Keep the provider key in a host environment variable. The example below maps
that host value into Harbor's agent environment without writing the value into
the repository. Replace the dataset, task, provider, model, and endpoint with
your own values:

```sh
zsh -lic 'export PYTHONPATH="$PWD"; .venv/bin/harbor run \
  -d DATASET_NAME \
  -i TASK_NAME \
  -a benchmark.harbor.firstcoder_agent:FirstCoderHarborAgent \
  -m Yuren/gpt-5.6-terra \
  -n 1 -k 1 --ak max_tool_rounds=120 --ak reasoning_effort=medium \
  --agent-setup-timeout-multiplier 3 \
  --ae FIRSTCODER_PROVIDER_NAME=PROVIDER \
  --ae FIRSTCODER_MODEL=gpt-5.6-terra \
  --ae FIRSTCODER_BASE_URL=https://provider.example/v1 \
  --ae "FIRSTCODER_API_KEY=\${FIRSTCODER_API_KEY}" \
  --ae FIRSTCODER_DISABLE_GLOBAL_SKILLS=1 \
  -o benchmark/runs/harbor/smoke -y'
```

`-m` records model metadata in Harbor. The `FIRSTCODER_*` variables configure
the FirstCoder process inside the task. Do not add `--upload` unless publishing
results is explicitly intended.

`reasoning_effort` is optional and is passed to FirstCoder as a provider-specific
model request field. Whether values such as `low`, `medium`, or `high` are
accepted depends on the selected provider/model.

## Reuse dependencies across trials

By default Harbor gives every trial a fresh container, so FirstCoder's Python
dependencies are downloaded again for each task. The adapter installs into a
shared pip/uv cache at `/opt/firstcoder-cache`. Bind-mount a host directory
there with Harbor's `--mounts` so wheels download once and are reused across
trials and concurrent containers:

```sh
mkdir -p "$HOME/.cache/firstcoder-harbor"
.venv/bin/harbor run \
  ... \
  --mounts '[{"type":"bind","source":"'"$HOME"'/.cache/firstcoder-harbor","target":"/opt/firstcoder-cache"}]' \
  ...
```

The mount stores downloaded archives only, not the virtual environment: each
trial rebuilds its own venv (`--clear`) so concurrent trials never corrupt a
shared environment. The install step retries the download up to three times
with backoff, so a single flaky fetch does not error the trial. Without the
mount the adapter still runs correctly, using a per-container cache that is
discarded when the container is removed.

### Optional wheelhouse

共享 cache 仍需要首次联网下载。若要降低并发 trial 对 PyPI/镜像网络的依赖，可先为
Harbor 的 Linux/Python 3.11 容器准备 wheelhouse：

```powershell
.\.venv\Scripts\python.exe -m benchmark.harbor.prepare_wheelhouse
```

然后把 cache 以可写方式、wheelhouse 以只读方式挂载：

```powershell
$mounts = .\.venv\Scripts\python.exe -m benchmark.harbor.preflight `
  --cache-dir "$HOME\.cache\firstcoder-harbor" `
  --wheelhouse "$HOME\.cache\firstcoder-harbor\wheelhouse" `
  --print-mounts
```

adapter 会优先在 `/opt/firstcoder-wheelhouse` 查找依赖，缺少 wheel 时仍可回退到索引和
共享 cache。若 wheelhouse 已验证完整，可向 agent 环境传入
`FIRSTCODER_WHEELHOUSE_ONLY=1`，此时缺 wheelhouse 或缺依赖会明确失败，不会静默联网。

## Preflight

大规模运行前先做不会打印变量值的预检：

```powershell
.\.venv\Scripts\python.exe -m benchmark.harbor.preflight `
  --env-file .env.harbor `
  --cache-dir "$HOME\.cache\firstcoder-harbor" `
  --wheelhouse "$HOME\.cache\firstcoder-harbor\wheelhouse" `
  --model MODEL_ID `
  --image-file benchmark/harbor/terminal-bench-ab-images.txt
```

预检覆盖 Docker daemon/version、必要 provider 变量是否存在、provider endpoint 的 HTTP
连通性、`/models` 目录中的模型可用性、cache 可写性、wheelhouse 状态和镜像本地状态。
`--model` 可覆盖专用环境文件中的模型而不改动凭据；加 `--pull-images` 会串行预拉缺失镜像；
加 `--require-images` 会把未预拉镜像视为失败。预检只报告变量名与状态，不输出
`.env.harbor` 中的凭据值。

## Fixed Terminal-Bench A/B set

`benchmark/harbor/terminal-bench-ab-tasks.txt` 固化了六个回归任务：

- `chess-best-move`
- `configure-git-webserver`
- `compile-compcert`
- `qemu-alpine-ssh`
- `adaptive-rejection-sampler`
- `tune-mjcf`

运行器固定使用 `terminal-bench@2.0`、单并发、单次尝试和同一 adapter 配置，避免不同阶段
因任务集或并发变化失去可比性：

```powershell
.\.venv\Scripts\python.exe -m benchmark.harbor.run_terminal_bench_ab `
  --env-file .env.harbor `
  --cache-dir "$HOME\.cache\firstcoder-harbor" `
  --wheelhouse "$HOME\.cache\firstcoder-harbor\wheelhouse" `
  --model MODEL_ID `
  --reasoning-effort MODEL_SUPPORTED_VALUE `
  --label phase6
```

`reasoning_effort` 不设默认值，只在当前模型明确支持时传入。先用 `--dry-run` 查看命令；
需要预拉固定任务镜像时加 `--pull-images`。不要给 Terminal-Bench 启用 Aider feedback plugin。

## Results

Harbor stores the resolved configuration, trial status, agent logs, verifier
logs, rewards, and timing under the selected jobs directory. Inspect a completed
local run with:

```sh
.venv/bin/harbor view benchmark/runs/harbor/smoke
```

A successful dataset download or container start is not a passing result. Use
the trial reward and verifier logs as the completion evidence.

使用离线汇总器同时报告基础设施、reward-only 和端到端指标：

```powershell
.\.venv\Scripts\python.exe -m benchmark.harbor.summarize `
  benchmark/runs/harbor/RUN_NAME/TIMESTAMP
```

加 `--json` 可输出机器可读结果；加 `--compare CANDIDATE_RUN` 可输出 A/B 的百分点变化。
汇总器会读取每个 trial 的 `result.json`，并在存在
`agent/firstcoder-session.jsonl` 时聚合 `agent_turn_telemetry` 的最终回合快照。遥测事件不会
进入 provider 消息，也不包含提示词、工具参数、工具输出或 secret。

## Windows

Use Docker Desktop in Linux containers mode for normal Harbor task images. Run
the commands from a shell whose working directory is the FirstCoder repository,
keep `PYTHONPATH` pointed at that checkout, and start with one task and `-n 1`.
Verify the agent log and verifier result before increasing concurrency.
