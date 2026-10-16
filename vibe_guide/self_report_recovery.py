"""Re-judge a self-reported delivery that was refused only for its cursor.

Through 5.0.11 the delivery gate demanded a cursor on the developer binding,
and only a supervisor-written wait result ever sets one.  A `vibe
worker-deliver` report is consumed by a wait with no cursor, so a complete
delivery was refused as ``blocked_unknown`` ("current cursor is missing") and
nothing re-read it: the archive was already consumed, and repair/requeue route
``reconcile_binding`` to manual recovery.

The fixed gate accepts the self-report receipt in place of the cursor.  This
recovery feeds the *same* bound result back through the normal event path, so
every other gate (identity, host, worktree, in-session review, contract
tests) judges it again.  It only applies when, from structured evidence:

* the node still names its developer task as the active, registered handle,
  and the binding is that task and generation with no cursor;
* the generation's archive was consumed by a same-generation wait aimed at
  that task/host, whose bound result is the archived delivery;
* since the task started, the node recorded only the refusal and its
  re-polls (no delivery, scope, decision or other lifecycle event), and the
  pre-fix gate refuses that result for the cursor alone;
* the fixed gate passes on that result.

Anything else raises ``ValueError`` before the first write.  A newer wait the
monitor issued afterwards (same handle, no result) is retired with an audit
marker; one that already has a result refuses the recovery.
"""
from typing import Any, Dict

from .adapters.task_provider import ProviderActionStore
from .contracts import RunHandle
from .evidence import evaluate_delivery_evidence
from .state import interprocess_lock, load_events, load_snapshot, run_dir, save_snapshot
from .supervisor import SupervisorLease

SELF_REPORT_DELIVERY_RECONCILED = "self_report_delivery_reconciled"
SELF_REPORTED_DELIVERY_PACKAGE = frozenset({"kind", "node_id"})
_ROLE = "developer"
_ENTRY = "monitor.reconcile_self_reported_delivery"
#: The only node events the cursor refusal and its re-polls leave behind.
_STUCK_PATTERN = frozenset({"blocked_unknown", "retry_scheduled", "unknown", "timeout",
                            "acceptance_rejected"})


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("self-reported delivery recovery: " + reason)


def reconcile_self_reported_delivery(monitor, run_id: str, node_id: str, runner):
    supervisor = SupervisorLease(monitor.paths)
    with interprocess_lock(run_dir(monitor.paths, run_id) / "adopt.lock"):
        # A live supervisor cycle holds an older snapshot and would overwrite
        # the recovery; refuse instead of racing it.
        _require(supervisor.acquire(run_id), "a supervisor is running this run; retry after it stops")
        try:
            return _reconcile_locked(monitor, run_id, node_id, runner)
        finally:
            supervisor.release(run_id)


def _collect(monitor, snapshot, node_id: str, runner) -> Dict[str, Any]:
    _require(node_id in snapshot.nodes and node_id in monitor.nodes, "node is unknown")
    current = snapshot.nodes[node_id]
    _require(current.get("status") in {"blocked_unknown", "retry_pending"},
             "node is not blocked_unknown or retry_pending")
    active = current.get("active_task")
    _require(isinstance(active, dict) and active.get("role") == _ROLE,
             "node has no active developer task")
    handle_id = active.get("handle_id")
    generation = active.get("generation")
    task_id = active.get("task_id")
    _require(isinstance(handle_id, str) and handle_id and snapshot.handles.get(node_id) == handle_id,
             "active task handle is not the registered handle")
    _require(type(generation) is int and generation >= 1
             and current.get("developer_generation") == generation
             and current.get("developer_identity") == task_id,
             "active task is not the node's current developer generation")
    _require(current.get("start_intent") is None, "node has a pending start intent")
    retry = current.get("retryable_action")
    _require(retry is None or (isinstance(retry, dict) and retry.get("same_task") is True
                               and retry.get("task_id") in (None, task_id)
                               and not retry.get("successor")),
             "node has a pending retry of another kind")

    binding = monitor._load_task_binding(snapshot, node_id, _ROLE)
    _require(binding.task_id == task_id and binding.generation == generation,
             "task binding is not the active task")
    _require(binding.cursor in (None, ""),
             "binding has a cursor; the normal wait path applies")

    store = ProviderActionStore(monitor.paths)
    archive = store._read(store._delivery_path(snapshot.run_id, node_id, _ROLE, generation))
    payload = archive.get("payload")
    _require(isinstance(payload, dict), "archived self-report is missing")
    delivery = payload.get("delivery_evidence")
    receipt = store.self_report_receipt(snapshot.run_id, node_id, _ROLE, generation,
                                        binding.task_id, binding.host or binding.hostId, delivery)
    _require(receipt is not None, "no consumed self-report receipt for this task and generation")
    result = store.result(receipt["action_id"])

    # Why the node is blocked is not readable from its reason (persisted
    # provider text is redacted), so it is re-derived from evidence: since
    # this task started, the node saw only the stuck pattern.
    started = None
    events = load_events(monitor.paths, snapshot.run_id)
    for index, event in enumerate(events):
        data = event.get("data") or {}
        if data.get("node_id") != node_id:
            continue
        provenance = event.get("provenance") or {}
        if ((event.get("event") == "start_confirmed" and provenance.get("task_id") == task_id
             and provenance.get("generation") == generation)
                or (event.get("event") == "orphan_dispatch_adopted"
                    and data.get("task_id") == task_id and data.get("generation") == generation)):
            started = index
    _require(started is not None, "no start of this task is recorded")
    for event in events[started + 1:]:
        if (event.get("data") or {}).get("node_id") == node_id:
            _require(event.get("event") in _STUCK_PATTERN,
                     "node saw a {} event after the task started".format(event.get("event")))

    # With no cursor on the binding, the fixed gate passing means the pre-fix
    # gate refused this result for the cursor alone.
    gate = evaluate_delivery_evidence({**current, "status": "DELIVERED"}, binding, delivery,
                                      self_report_receipt=True)
    _require(gate.complete, "delivery evidence is still incomplete: " + "; ".join(gate.reasons))

    _require(hasattr(runner, "_wait_result") and hasattr(runner, "_handle_path"),
             "the run's runner cannot replay a wait result")
    metadata = store._read(runner._handle_path(handle_id))
    _require(metadata.get("run_id") == snapshot.run_id and metadata.get("node_id") == node_id
             and metadata.get("role") == _ROLE and metadata.get("task_id") == task_id
             and metadata.get("generation") == generation,
             "handle metadata names another task")
    later = metadata.get("pending_action")
    if later:
        _require(later != receipt["action_id"] and metadata.get("pending_operation") == "wait",
                 "handle has a pending non-wait action")
        later_action = store.action(later)
        _require(later_action.get("run_id") == snapshot.run_id
                 and later_action.get("issue_id") == node_id
                 and later_action.get("role") == _ROLE
                 and later_action.get("generation") == generation
                 and later_action.get("sequence", 0) > receipt.get("sequence", 0),
                 "pending wait is not a later wait of this task")
        _require(store.result(later) is None,
                 "a later wait was already answered; run resume instead")
    return {"active": active, "handle_id": handle_id, "generation": generation,
            "task_id": task_id, "receipt": receipt, "result": result,
            "metadata": metadata, "later": later, "store": store}


def _reconcile_locked(monitor, run_id: str, node_id: str, runner):
    monitor._reset_binding_cache()
    snapshot = load_snapshot(monitor.paths, run_id)
    monitor._require_snapshot_authorization(snapshot)
    monitor._reconcile_unapplied_events(snapshot)
    monitor._validate_execution_topology(snapshot)
    proof = _collect(monitor, snapshot, node_id, runner)
    _require(len(load_events(monitor.paths, run_id)) == snapshot.event_sequence,
             "the run changed while reconciling; retry")
    store = proof["store"]
    metadata = proof["metadata"]
    if proof["later"]:
        store.retire_wait(proof["later"], "answered_by_self_report_receipt")
        metadata["pending_action"] = None
        metadata["pending_operation"] = None

    current = snapshot.nodes[node_id]
    current["status"] = "running"
    current["retryable_action"] = None
    current["quarantine"] = None
    current["reason"] = None
    current["recovery_action"] = None
    receipt = proof["receipt"]
    monitor._record(snapshot, SELF_REPORT_DELIVERY_RECONCILED, {
        "run_id": run_id, "node_id": node_id, "role": _ROLE,
        "task_id": proof["task_id"], "generation": proof["generation"],
        "handle_id": proof["handle_id"],
        "action_id": receipt["action_id"], "request_digest": receipt["request_digest"],
    })
    handle = RunHandle(proof["handle_id"])
    claims = {"node_id": node_id, "role": _ROLE, "task_id": proof["task_id"],
              "handle_id": proof["handle_id"], "generation": proof["generation"]}
    # The same projection a poll applies to this bound result.
    events = runner._wait_result(handle, metadata, claims, proof["result"])
    _require(len(events) == 1 and events[0].event in {"delivered", "complete"},
             "bound result does not replay as a delivery")
    monitor._apply_event(snapshot, node_id, proof["handle_id"], events[0], runner)
    monitor._record_topology_projection(snapshot, _ENTRY)
    monitor._refresh_run_status(snapshot)
    save_snapshot(monitor.paths, snapshot)
    return snapshot
