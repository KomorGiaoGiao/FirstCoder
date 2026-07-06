# Permissions Design

[中文版本](PERMISSIONS_DESIGN.zh-CN.md)

## Overview

The permission layer separates what the model asks to do from what the program is willing to execute. It is part of the real tool-execution control flow, not a passive policy helper.

In practice, permission preflight can do three things:

- allow a tool call to execute immediately
- deny it and return a synthetic tool result
- pause the turn and wait for user confirmation

## Key Files

- `firstcoder/permissions/types.py`: core permission data types
- `firstcoder/permissions/policy.py`: default policy implementation
- `firstcoder/permissions/grants.py`: grant storage
- `firstcoder/permissions/manager.py`: preflight, confirmation building, and confirmation resolution
- `firstcoder/tools/permission_registry.py`: integration point between tools and permissions
- `firstcoder/agent/session.py`: pending permission execution state
- `firstcoder/agent/loop.py`: pause/resume integration in the loop

## Core Types

The main types are defined in `firstcoder/permissions/types.py`.

Important enums and dataclasses include:

- `PermissionAction`
- `PermissionMode`
- `PermissionDecisionKind`
- `PermissionPersistence`
- `PermissionScopeType`
- `PermissionConfirmationChoice`
- `PermissionRequest`
- `PermissionGrant`
- `PermissionDecision`

The important distinction is:

- `PermissionDecisionKind` answers whether the current execution is allowed, denied, or must ask
- `PermissionPersistence` answers how long an approved decision should live

## Permission Manager

`PermissionManager` is the central program-side entrypoint.

Its job is to combine:

- the default policy
- persisted grants
- the current permission mode

`preflight(request)` performs the real pre-execution decision path:

1. normalize the request against the project root
2. check matching persisted grants
3. if no grant matches, apply the default policy

`build_confirmation(request)` converts an `ASK` result into a structured `UserInputRequest` for the UI.

`resolve_confirmation(request, choice)` converts the user’s answer back into a final decision and optionally persists an allow-always grant.

## Permission Modes

The current default-policy modes are:

- `conservative`
- `standard`
- `aggressive`
- `bypass`

`bypass` is still a program-side mode, not a model capability. The model does not gain new powers directly; the program simply becomes more permissive in preflight.

## Default Policy

The default behavior lives in `firstcoder/permissions/policy.py`.

Important real rules include:

- reading ordinary project files is usually allowed
- writing inside the project often asks, unless aggressive mode and tool metadata allow auto execution
- deleting outside the project root is denied
- reading sensitive environment variables is denied
- readonly git commands inside the project are allowed
- shell commands with control operators require confirmation
- network requests ask by default

Sensitive paths currently include things like:

- `.git`
- `.env`
- `.pem`
- `.key`

## Grant Model

Long-lived grants are represented by `PermissionGrant` and stored through `PermissionGrantStore` implementations.

The project runtime typically uses `FilePermissionGrantStore`, which persists grants into a JSON file under the session data root.

The grant model is scope-based. `allow always` decisions are not stored as free-form approvals; they are converted into a scope such as:

- exact path
- command prefix
- host
- env key

The scope for a request is computed in `default_scope_for_request(...)`.

## Tool Integration

Permissions are enforced through `PermissionAwareToolRegistry`, not by asking tools to call the permission manager manually.

The execution path is:

1. a tool call is preflighted
2. a `PermissionRequest` is built from the tool’s `ToolPermissionSpec`
3. `PermissionManager.preflight(...)` returns `ALLOW`, `DENY`, or `ASK`
4. the wrapper either:
   - executes the tool
   - returns a denied result
   - returns a structured confirmation result

This means permissions are coupled to the registry wrapper, not scattered across individual tool executors.

## Pause And Resume

`ASK` is a real pause state in the agent loop.

When the loop encounters an `ASK` result:

1. it stores a `PendingPermissionExecution`
2. it returns `pending_input` to the caller
3. the UI or interactive CLI collects the user answer
4. `resume_with_user_input(...)` resolves that answer
5. the original tool either executes or is turned into a denied result

This behavior matters because provider-visible message sequences must stay legal. The loop guarantees that an assistant tool call will still get a matching tool result after the pause.

## Persistence And Resume Behavior

Long-lived grants are persisted, but pending permission state is not modeled as a dedicated top-level permission event stream.

Instead, session resume reconstructs a pending permission execution from the unmatched tail of the assistant tool-call history.

This design keeps permission persistence narrow:

- durable grants live in grant storage
- pending execution is reconstructed from conversation facts when possible

## Design Notes

- Permissions are part of the tool execution protocol, not just a UI prompt layer.
- The manager combines grants and policy behind a single preflight entrypoint.
- `ASK` pauses the turn and later resumes the original tool execution path.
- Persisted grants are scope-based and intentionally conservative.
