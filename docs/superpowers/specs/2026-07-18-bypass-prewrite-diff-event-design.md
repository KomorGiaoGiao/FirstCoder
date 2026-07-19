# Bypass Prewrite Diff Event Design

## Goal

In bypass mode, keep mutations fully automatic while still showing the user the
same red/green prewrite diff card available in interactive permission modes.
Displaying the card must not pause execution, request confirmation, or require
an Apply action.

## Behavior

For tools supported by the existing prewrite review builder (`write`, `edit`,
`delete`, and `apply_patch`):

1. Build the prewrite review before executing the mutation.
2. Emit a non-blocking `prewrite_review` tool event containing the review
   payload.
3. Let the TUI render the payload as the existing diff card.
4. Execute the mutation immediately.
5. Emit the existing started/finished tool events and persist the normal tool
   result.

Bypass mode must never create `pending_permission_execution`, return a pending
input, display a permission prompt, or wait for Apply/confirmation.

Standard and aggressive mode behavior remains unchanged.

## Event Contract

Extend `ToolExecutionEvent.kind` with `prewrite_review` and add an optional
`prewrite_review` payload field. This event represents display-only runtime
information; it is not a permission decision and does not enter the session
message history.

The TUI handles this event separately from ordinary tool activity:

- render the existing prewrite diff card;
- do not set `waiting · permission`;
- do not render permission choices;
- continue receiving the subsequent started/finished events normally.

## Sync and Streaming Paths

Both `execute_interactive()` and `execute_interactive_async()` must use the same
review-building helper before bypass mutations. The helper emits one review
event and returns whether execution may continue, keeping the two paths
behaviorally identical.

Bypass parallel batching must not batch prewrite-review mutation tools, because
each diff must be computed and displayed before its corresponding mutation.
Read-only and non-reviewable bypass tools may keep their current batching
behavior.

## Failure Handling

If a supported mutation's prewrite review cannot be built, do not execute the
mutation. Emit/persist the existing `prewrite_review_failed` tool result so
bypass never performs a blind write.

If no tool-event handler is installed, review generation and safety behavior
remain the same; only the visual card is absent.

## Tests

Add regression coverage proving:

- synchronous bypass emits review, then started and finished, without pending
  input, and writes the file;
- streaming bypass has the same behavior;
- the TUI renders `prewrite_review` as a diff card without permission UI;
- reviewable bypass mutations are excluded from parallel batches;
- a review-build failure blocks the mutation;
- standard/aggressive permission and review behavior remains unchanged.
