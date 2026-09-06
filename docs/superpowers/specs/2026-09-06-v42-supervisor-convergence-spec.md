# V4.2 Supervisor Architecture Convergence Spec

## Scope

This revision hardens the existing `Monitor` state machine without changing
the frozen historical V4.2 run. It adds a single canonical task-lifecycle
migration boundary, strict Codex public-schema projection, durable pending-task
reconciliation, a supervisor lease/heartbeat, and evidence-gated delivery.

## Invariants

- `clientThreadId` is `SETUP_PENDING`; only an observed real `threadId` can be
  promoted, and promotion reuses the same generation and writer.
- `delivered` and `DELIVERY_COMPLETE` normalize to `DELIVERED`.
- Legacy fields are read only by `migrate_task_record`; provider calls receive
  only their native public schema.
- A stale or conflicting lease, cursor, host, worktree, branch, marker, or
  delivery path yields `blocked_unknown` and never unlocks a dependency.
- Supervisor progress is persisted in the run directory and is independent of
  the parent chat session. Deploy remains outside this scope.

## Verification boundary

Local lifecycle, schema-projection, lease, recovery, and evidence tests are
validating evidence for this revision. A real visible Codex App lifecycle is
not claimed unless `create_thread`, `navigate`, `wait`, and `resume` are
observed with matching provider and host identity.
