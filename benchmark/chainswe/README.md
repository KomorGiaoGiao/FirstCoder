# ChainSWE Docker Runner

`runner.py` owns the persistent FirstCoder session, model commits, and isolated
hidden-test replay. `docker_runner.py` is only the thin host launcher that
selects the official image and prepares the container.

## Run the default chain

The default is the three-issue `meltano_sdk_20230727` chain. Download the
official ChainSWE JSONL locally, set the API-key environment variable used by
your FirstCoder GPT-5.6 configuration, then launch it:

```sh
export FIRSTCODER_GPT56_API_KEY="..."
.venv/bin/python -m benchmark.chainswe.docker_runner \
  --chains /tmp/chainswe-data.jsonl
```

The launcher reads the selected chain's `docker_image` from that JSONL (for the
default chain this is
`clisterqj/swechain:swerebenchv2__meltano-sdk__1864-b1b3bd2`) and lets Docker
pull it if necessary. Artifacts go to a new `runs/chainswe/<chain>-<timestamp>/`
directory:

- `summary.json`: atomically updated after every hidden verification with safe
  per-issue progress (pass/fail, elapsed time, exit code, and context metrics).
  It is replaced with the complete per-issue trace and chain score after the
  final issue. Hidden patches, commands, and verifier output are never exposed
  in the in-progress snapshot.
- `session-data/`: the persistent FirstCoder session data for the chain.

The task checkout stays in the official image at `/sdk`; the runner resets it
to the selected base commit before starting. The local source tree is mounted
read-only and only the chosen run directory is mounted read-write. The dataset
is never mounted into the container: the host selects one chain and sends its
record once through Docker standard input before FirstCoder starts. This keeps
hidden `test_patch` and `test_cmds` unavailable to the agent's shell.
The task prompt permits local validation but prohibits edits to tests, fixtures,
and benchmark/verifier files, so the official hidden patches remain applicable
when they are replayed in the isolated verifier worktree.

## Provider defaults and overrides

The defaults mirror the current local GPT-5.6 configuration:

```text
provider:      openai-compatible
provider name: custom
model:         gpt-5.6-terra
base URL:      https://yurenapi.com/v1
key variable:  FIRSTCODER_GPT56_API_KEY
```

Override public provider settings or select a different key variable when
needed:

```sh
.venv/bin/python -m benchmark.chainswe.docker_runner \
  --chains /tmp/chainswe-data.jsonl \
  --chain-id another_continuous_id \
  --run-dir runs/chainswe/another-smoke \
  --provider openai-compatible \
  --provider-name custom \
  --model your-model \
  --base-url https://example.com/v1 \
  --api-key-env YOUR_PROVIDER_API_KEY \
  --max-tool-rounds 120
```

The API-key value is never placed in a command argument, launcher status
message, or `summary.json`: Docker receives only `--env YOUR_PROVIDER_API_KEY`
and the bootstrap maps it to FirstCoder's standard runtime variable inside the
container. The benchmark agent is still a trusted process with shell access in
its task container; this forwarding mechanism is not a sandbox for a malicious
agent or task.

## Runtime notes

The image requires `linux/amd64` and the launcher explicitly requests that
platform. On Apple Silicon/Colima it therefore runs under x86 emulation and can
be substantially slower. It uses the image's
`/opt/conda/envs/swebench_matterhorn/bin/python` only to create a separate
`/opt/firstcoder-agent/.venv`, copies the read-only mounted FirstCoder project
to an agent-owned directory before installing it, and runs the benchmark runner
through that venv. The task environment's `PATH`
is not changed, so hidden verifier `test_cmds` still resolve against the
official task environment. No Docker, Harbor, or ChainSWE behavior is imported
into `firstcoder/`.
