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
  -m PROVIDER/MODEL \
  -n 1 -k 1 --ak max_tool_rounds=120 \
  --agent-setup-timeout-multiplier 3 \
  --ae FIRSTCODER_PROVIDER=openai-compatible \
  --ae FIRSTCODER_PROVIDER_NAME=PROVIDER \
  --ae FIRSTCODER_MODEL=MODEL \
  --ae FIRSTCODER_BASE_URL=https://provider.example/v1 \
  --ae "FIRSTCODER_API_KEY=\${FIRSTCODER_API_KEY}" \
  --ae FIRSTCODER_DISABLE_GLOBAL_SKILLS=1 \
  -o benchmark/runs/harbor/smoke -y'
```

`-m` records model metadata in Harbor. The `FIRSTCODER_*` variables configure
the FirstCoder process inside the task. Do not add `--upload` unless publishing
results is explicitly intended.

## Results

Harbor stores the resolved configuration, trial status, agent logs, verifier
logs, rewards, and timing under the selected jobs directory. Inspect a completed
local run with:

```sh
.venv/bin/harbor view benchmark/runs/harbor/smoke
```

A successful dataset download or container start is not a passing result. Use
the trial reward and verifier logs as the completion evidence.

## Windows

Use Docker Desktop in Linux containers mode for normal Harbor task images. Run
the commands from a shell whose working directory is the FirstCoder repository,
keep `PYTHONPATH` pointed at that checkout, and start with one task and `-n 1`.
Verify the agent log and verifier result before increasing concurrency.
