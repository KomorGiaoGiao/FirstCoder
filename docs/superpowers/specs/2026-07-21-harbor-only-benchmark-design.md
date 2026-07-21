# Harbor-Only Benchmark Design

## Goal

Make Harbor the only benchmark integration in FirstCoder. FirstCoder remains a
coding agent; Harbor owns benchmark dataset retrieval, task containers,
verification, and result storage.

## Scope

Remove every non-Harbor benchmark interface:

- all `benchmark/` subpackages except `benchmark/harbor/`;
- `firstcoder/eval/`, including its prediction-generation and task models;
- tests that exercise the removed benchmark code;
- runbooks and documentation for SWE-bench, SWE-bench-fast, Terminal-Bench,
  ChainSWE, EvalPlus, AtCoder, and the local pytest benchmark;
- README and documentation links that advertise those interfaces.

Generated benchmark run artifacts remain ignored and are not part of this
change.

## Retained Integration

Keep `benchmark.harbor.firstcoder_agent:FirstCoderHarborAgent` as the sole
benchmark entry point. It stages the minimal FirstCoder runtime into a Harbor
task container, installs it in an isolated environment, and invokes one
non-interactive `firstcoder --benchmark` turn in Harbor's task work directory.

Harbor remains responsible for:

1. resolving a published dataset or local Harbor task;
2. creating the task's isolated environment;
3. passing the task instruction to the agent without verifier internals;
4. running the verifier after the agent exits; and
5. storing job, trial, agent, and verifier artifacts.

## Documentation

The remaining Harbor guide will explain what Harbor is, how it relates to
FirstCoder, and how to:

1. install Harbor;
2. inspect or download a Harbor dataset;
3. run one task with the FirstCoder Harbor adapter;
4. pass provider configuration through Harbor agent environment variables
   without placing a secret in repository files; and
5. inspect local result artifacts.

The guide will call out that Windows must use Docker Desktop's Linux-container
mode for ordinary Linux benchmark tasks, and recommend a single task with
`-n 1` before expanding a run.

## Verification

- `rg` confirms no remaining references to removed benchmark names outside git
  history and generated artifacts.
- `pytest tests/test_harbor_adapter.py -q` passes.
- `pytest tests -q` is run after deletion; unrelated pre-existing failures, if
  any, are reported separately.
- `git diff --check` passes.
