# Terminal-Bench Adapter

This directory contains the FirstCoder adapter for Terminal-Bench 0.2.x.

It uses Terminal-Bench's installed-agent interface:

```text
Terminal-Bench task container
  -> install FirstCoder with pip
  -> run python3 -m firstcoder --benchmark --message <task>
  -> FirstCoder edits the task workspace
  -> Terminal-Bench runs its verifier
```

## Quick Smoke

Install Terminal-Bench in your local environment:

```sh
.venv/bin/python -m pip install terminal-bench
```

If you use Colima or another non-default Docker context, expose the Docker socket
to the Python SDK before running Terminal-Bench:

```sh
export DOCKER_HOST="unix://$HOME/.colima/default/docker.sock"
```

Run a small task with the import-path agent:

```sh
.venv/bin/tb run \
  --agent-import-path benchmark.terminal_bench.firstcoder_agent:FirstCoderTerminalBenchAgent \
  --task-id hello-world \
  --model openai/gpt-4.1-mini \
  --agent-kwarg max_tool_rounds=120 \
  --agent-kwarg package='https://github.com/KomorGiaoGiao/FirstCoder/archive/refs/heads/main.zip' \
  --n-concurrent 1
```

Terminal-Bench 0.2.x invokes the Docker Compose plugin as `docker compose`.
If your machine only has the legacy standalone `docker-compose` binary, install
or enable the Docker Compose plugin before running the full harness.

Provider credentials are forwarded from the host environment when present:

```sh
export FIRSTCODER_PROVIDER=openai-compatible
export FIRSTCODER_API_KEY=...
export FIRSTCODER_BASE_URL=...
export FIRSTCODER_MODEL=...
```

## Local Source Install

By default the setup script installs the published `firstcoder` package from PyPI.
The current Terminal-Bench adapter needs a package version that includes
`firstcoder --benchmark`; until that version is published, pass a package specifier
that the container can install:

```sh
.venv/bin/tb run \
  --agent-import-path benchmark.terminal_bench.firstcoder_agent:FirstCoderTerminalBenchAgent \
  --task-id hello-world \
  --model openai/gpt-4.1-mini \
  --agent-kwarg package='https://github.com/KomorGiaoGiao/FirstCoder/archive/refs/heads/main.zip'
```

## Import Smoke

You can verify the adapter without starting Docker:

```sh
.venv/bin/python -m pytest tests/test_terminal_bench_adapter.py -q
```

## Notes

- The adapter runs FirstCoder in `--benchmark` mode, which uses bypass permissions.
- Benchmark session data is stored outside the task repo at `/tmp/firstcoder-terminal-bench`.
- The initial implementation targets non-interactive Terminal-Bench tasks where the agent
  receives one instruction and completes the work through tool calls.
