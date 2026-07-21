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

Run one local smoke task using the FirstCoder configuration currently used on
this machine: the Yuren OpenAI-compatible endpoint and `gpt-5.6-sol`. The key
remains a Harbor template and is resolved from the login shell at runtime:

> These values mirror this repository's local FirstCoder configuration. Replace
> the model, base URL, key variable, and provider name when using another
> provider.

```sh
zsh -lic 'export PYTHONPATH="$PWD"; .venv/bin/harbor run \
  -d terminal-bench-sample@2.0 \
  -i regex-log \
  -a benchmark.harbor.firstcoder_agent:FirstCoderHarborAgent \
  -m Yuren/gpt-5.6-sol \
  -n 1 -k 1 --ak max_tool_rounds=90 \
  --agent-setup-timeout-multiplier 3 \
  --ae FIRSTCODER_PROVIDER=openai-compatible \
  --ae FIRSTCODER_PROVIDER_NAME=Yuren \
  --ae FIRSTCODER_MODEL=gpt-5.6-sol \
  --ae FIRSTCODER_BASE_URL=https://yurenapi.cn/v1 \
  --ae FIRSTCODER_PARALLEL_TOOL_CALLS=true \
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
