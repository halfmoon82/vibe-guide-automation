# Heartbeat native action consumer and recovery contract

The heartbeat is a trigger. A servicing turn reads the current run snapshot,
reconciles only fully bound superseded requests, invokes each pending
`native_tool`, and writes the matching result before calling `resume`.

Local binding or profile contract errors record a structured phase, reason code,
and action reference and retry the same task generation. Reviewer continuations
name the current generation, business review scope, delivery/status paths, and
completion marker. Dormant planned downstream nodes have no write ownership
until an active task, start intent, or lease exists; unknown writer evidence
remains fail closed. Stale reconciliation preserves the immutable request and
audit record and never fabricates provider success.

Scope correction: updating the shipped protocol digest registry is a mechanical
package companion to the authorized protocol edit; it changes no runtime
behavior or product acceptance criteria.
