# Permissions Design

[中文版本](PERMISSIONS_DESIGN.zh-CN.md)

## What Permissions Guarantee

Permissions answer one program-side question before a sensitive tool operation:
**may this concrete request run now?** They do not rely on model compliance.
The enforcement chain is:

```text
ToolPermissionSpec -> PermissionRequest -> PermissionManager
  -> matching grant or DefaultPermissionPolicy
  -> ALLOW | ASK | DENY
  -> PermissionAwareToolRegistry action
```

The tool executor runs only on the `ALLOW` branch. That is the boundary that
matters when reviewing safety—not the wording in a system message.

## Concrete Example: Writing a File

1. A model asks to call `write(path, content)`.
2. The permission-aware registry uses the write tool's spec to build a request
   containing action, normalized target, cwd, and policy hints.
3. `PermissionManager.preflight` first checks matching grants, then the default
   policy under the active mode.
4. `ALLOW` executes; `DENY` becomes a tool result; `ASK` becomes a structured
   `UserInputRequest` (from `firstcoder.runtime.user_input`) and the turn pauses.
5. After an answer, `resolve_confirmation` rechecks guards and either executes
   the original pending call or appends a denied result.

The model sees the resulting tool message and can adapt. It never directly
writes a grant file or calls an executor behind the registry.

## Data Model

`permissions/types.py` contains the vocabulary:

| Type | Role |
| --- | --- |
| `PermissionAction` | category such as filesystem, shell, network, or env access |
| `PermissionRequest` | one concrete target/action to decide |
| `PermissionDecisionKind` | `ALLOW`, `ASK`, or `DENY` for this request |
| `PermissionPersistence` | whether an approval is once or durable |
| `PermissionGrant` | a durable, scoped allow rule |
| `PermissionScopeType` | exact path, command prefix, host, env key, and similar scope |
| `PermissionMode` | conservative, standard, aggressive, or bypass policy setting |

Do not conflate decision and persistence: “allow this once” is an allowed
decision with short persistence; “allow always” creates a scoped grant only
when the request supports it.

## Policy, Modes, and Grants

`DefaultPermissionPolicy` makes the fallback decision after grants. The policy
is target-aware: ordinary reads inside a project are generally safer than
external deletion, sensitive environment reads, or a shell command with control
operators. The exact rules live in `permissions/policy.py`; add tests instead
of restating a partial copy in callers.

Modes adjust that policy:

- `conservative`: asks more often;
- `standard`: normal project behavior;
- `aggressive`: permits selected auto-eligible actions more readily;
- `bypass`: a maximally permissive policy mode.

`bypass` is not removal of code paths. Requests are still normalized, registry
dispatch still occurs, results are still logged, and hard safety checks/policy
rules still define the actual behavior. Treat it as an explicit operating mode,
not an invisible model superpower.

`FilePermissionGrantStore` persists grants in the data root's `permissions.json`.
An “allow always” is converted to a calculated scope via
`default_scope_for_request`, never stored as an unbounded free-form approval.

## Shared Request Type Ownership

`UserInputRequest` / `UserInputOption` are defined in
`firstcoder.runtime.user_input` so `permissions`, `tools`, and UI code can share
them **without** importing `firstcoder.agent`. Compatibility re-exports may still
exist under `agent.user_input` for older call sites.

## Pause, Resume, and Replay

`ASK` must preserve the assistant's original tool call. `AgentSession` records
`PendingPermissionExecution`; the interactive caller receives `pending_input`.
On resume, the loop resolves the user choice and completes the same tool-call
transaction. This keeps the provider-visible sequence valid.

Durable grants and pending calls have different lifetimes. Grants are stored as
permission data. A pending action is reconstructed from unmatched assistant
tool-call history during resume when possible, rather than creating a second
parallel conversation log.

## Verification Exercises

```sh
.venv/bin/python -m pytest tests/test_permissions_policy.py \
  tests/test_permissions_manager.py tests/test_permissions_grants.py \
  tests/test_permission_registry.py tests/test_permission_commands.py -q
```

Read `tests/test_permission_registry.py` for the essential proof: the executor
is not invoked for deny or ask until the correct resume path occurs.

## Debugging Checklist

| Symptom | Check |
| --- | --- |
| unexpected prompt | exact action/target derived by the tool spec, then policy mode |
| expected durable approval ignored | grant scope normalization and grant store location |
| user approved but tool did not run | pending execution id and resume call |
| a dangerous action ran | registry wrapper was actually installed and tool has a permission spec |
| permission result breaks provider history | matching tool call id was appended |

## Change Rules

Put classification rules in `policy.py`, scope calculation in manager/types, and
tool-specific target extraction in `ToolPermissionSpec`. Do not have every tool
reimplement a permission dialog. Add regression tests for both the intended
allow path and the nearest unsafe neighbor.

Related: [Architecture](ARCHITECTURE.md), [Tools](TOOLS_DESIGN.md), and [Agent Loop Guardrails](AGENT_LOOP_GUARDRAILS.md).
