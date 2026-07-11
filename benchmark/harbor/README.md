# Harbor Adapter

This optional adapter runs the current FirstCoder checkout as a Harbor installed
agent. It is separate from `firstcoder/`: Harbor is a benchmark runtime, not a
core FirstCoder dependency.

The adapter stages only `pyproject.toml`, `README.md`, and `firstcoder/` into a
task container. It does not copy `.git`, `.venv`, local sessions, `.env`, or
other workspace files. Provider settings must be passed through Harbor's
agent-scoped environment; do not put an API key in a command, source file, or
job configuration.

Install Harbor only in the development virtual environment:

```sh
.venv/bin/python -m pip install 'harbor==0.18.0'
```

Run one local smoke task using the active GPT-5.6-compatible configuration. The
key remains a Harbor template and is resolved from the login shell at runtime:

```sh
zsh -lic '.venv/bin/harbor run \
  -d terminal-bench-sample@2.0 \
  -i regex-log \
  -a benchmark.harbor.firstcoder_agent:FirstCoderHarborAgent \
  -m yurenapi/gpt-5.6-terra \
  -n 1 -k 1 --ak max_tool_rounds=90 \
  --ae FIRSTCODER_PROVIDER=openai-compatible \
  --ae FIRSTCODER_PROVIDER_NAME=yurenapi \
  --ae FIRSTCODER_MODEL=gpt-5.6-terra \
  --ae FIRSTCODER_BASE_URL=https://yurenapi.com/v1 \
  --ae "FIRSTCODER_API_KEY=\${FIRSTCODER_GPT56_API_KEY}" \
  --ae FIRSTCODER_DISABLE_GLOBAL_SKILLS=1 \
  -o benchmark/runs/harbor/regex-log -y'
```

After the smoke succeeds, remove `-i regex-log` to run all ten tasks in
`terminal-bench-sample@2.0`. Keep `-n 1` on this machine. Do not add `--upload`
unless publishing results is explicitly intended.

`-m` records model metadata in Harbor; it does not configure FirstCoder. The
explicit `FIRSTCODER_*` variables above are the source of truth. The adapter
disables machine-global skills by default so a local unrelated skill cannot
alter a benchmark task; project-local task instructions still apply.
