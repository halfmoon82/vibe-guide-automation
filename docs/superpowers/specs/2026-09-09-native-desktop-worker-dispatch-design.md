# Native desktop worker dispatch design

## Problem

V4.2 currently gives several providers the same visible-task shape even when
their desktop control plane has not been observed. A client setup handle,
provider self-report, timeout, or generic mailbox response can therefore look
like a worker that is ready to write. This produces `retry_pending` without a
durable distinction between setup, native capability, and task identity.

## Decision

VibeGuide remains the DAG supervisor, but task lifecycle calls are delegated to
each provider's native desktop adapter. The common contract is limited to
`probe`, `create`, `locate`, `enter`, `resume`, and `wait`; request and result
schemas are provider-specific and validated before dispatch. Codex uses the
Codex App public tools (`create_thread`, `navigate_to_codex_page`,
`send_message_to_thread`, `wait_threads`). Other providers are dispatchable only
after an equivalent native desktop adapter is actually exposed and observed.

Static manifests describe what must be probed, never what is available. A
provider is `verified_available` only after all lifecycle operations return
structured evidence with matching provider task ID, host, worktree/cwd, and
enterability. Missing, empty, timed-out, or malformed observations remain
`unknown`/`retry_pending`; they never become unavailable or successful.

There is no implicit background fallback for a visible DAG. `clientThreadId`
and similar setup handles are `SETUP_PENDING` and cannot authorize business
writes. Recovery retries the same task generation and cursor. A successor is
forbidden until the original task is observed terminated and its lease released.

## Runtime flow

`probe -> create -> locate/visibility -> bind -> enter -> wait`.

The supervisor persists every request and result in the run evidence. It only
marks a developer `DELIVERED` after a native completion result and only unlocks
dependencies after an independent reviewer accepts the delivery.

## Verification

Tests cover provider-specific public schemas, strict setup-handle handling,
missing native operations, timeout/empty response classification, no background
fallback, same-task recovery, and the existing DAG writer/reviewer invariants.
Real desktop lifecycle evidence is reported separately from fixture tests.
