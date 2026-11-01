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

Role: the recovery follows the node's anchor role -- ``reviewer`` for a
reviewer-only node (integration review), ``developer`` for every other node.

Hybrid state: when that wait was already retired (as answered by the
self-report receipt) but the handle still names it as ``pending_action`` and
the binding holds a cursor, the same proof applies and recovery clears the
dangling action instead of refusing for the cursor.  A cursor with a live wait
or a wait retired for another reason is still refused.

Parked reviewer: a reviewer that reported and was then stopped (a review-loop
park, ``change_reviewer_or_split_node``) leaves an archived delivery nothing
consumed, because the parked node has no live handle to wait on.
``consume_parked_delivery`` runs inside ``Monitor.resume``: it finds the
reviewer's handle metadata, consumes the archive once through a wait of the
same task/generation, and feeds the bound result back through the normal
event path.  The consumed archive is the once-only guard, so a second resume
finds nothing to do.
"""
from typing import Any, Dict

from .adapters.task_provider import ProviderActionStore, ProviderUnavailable
from .contracts import RunHandle
from .evidence import evaluate_delivery_evidence
from .state import interprocess_lock, load_events, load_snapshot, run_dir, save_snapshot
from .supervisor import SupervisorLease

SELF_REPORT_DELIVERY_RECONCILED = "self_report_delivery_reconciled"
SELF_REPORTED_DELIVERY_PACKAGE = frozenset({"kind", "node_id"})
_RETIRE_REASON = "answered_by_self_report_receipt"
_ENTRY = "monitor.reconcile_self_reported_delivery"
#: Events a bound self-report may replay as, by role.  A reviewer reports its
#: conclusion (accepted / review_finding) rather than a developer handoff.
_REPLAY_EVENTS = {
    "developer": frozenset({"delivered", "complete"}),
    "reviewer": frozenset({"delivered", "complete", "accepted", "review_finding"}),
}
#: The only node events the cursor refusal and its re-polls leave behind.
_STUCK_PATTERN = frozenset({"blocked_unknown", "retry_scheduled", "unknown", "timeout",
                            "acceptance_rejected"})


def _retired_reason(store, action_id: str, run_id: str):
    """The reason of a valid stale marker beside this wait, else None."""
    action = store.action(action_id)
    path = store.root / "stale" / (action_id + ".json")
    if path.is_symlink() or not path.is_file() or action.get("run_id") != run_id:
        return None
    try:
        marker = store._read(path)
    except (OSError, ValueError):
        return None
    if (isinstance(marker, dict) and marker.get("status") == "stale"
            and marker.get("action_id") == action_id
            and marker.get("request_digest") == action.get("request_digest")):
        return marker.get("reason")
    return None


def _role_of(monitor, node_id: str, current: Dict[str, Any]) -> str:
    return monitor._primary_role(node_id, current)


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
    role = _role_of(monitor, node_id, current)
    generation_key = "review_generation" if role == "reviewer" else "developer_generation"
    active = current.get("active_task")
    _require(isinstance(active, dict) and active.get("role") == role,
             "node has no active {} task".format(role))
    handle_id = active.get("handle_id")
    generation = active.get("generation")
    task_id = active.get("task_id")
    _require(isinstance(handle_id, str) and handle_id and snapshot.handles.get(node_id) == handle_id,
             "active task handle is not the registered handle")
    _require(type(generation) is int and generation >= 1
             and current.get(generation_key) == generation
             and current.get(role + "_identity") == task_id,
             "active task is not the node's current {} generation".format(role))
    _require(current.get("start_intent") is None, "node has a pending start intent")
    retry = current.get("retryable_action")
    _require(retry is None or (isinstance(retry, dict) and retry.get("same_task") is True
                               and retry.get("task_id") in (None, task_id)
                               and not retry.get("successor")),
             "node has a pending retry of another kind")

    binding = monitor._load_task_binding(snapshot, node_id, role)
    _require(binding.task_id == task_id and binding.generation == generation,
             "task binding is not the active task")

    store = ProviderActionStore(monitor.paths)
    archive = store._read(store._delivery_path(snapshot.run_id, node_id, role, generation))
    payload = archive.get("payload")
    _require(isinstance(payload, dict), "archived self-report is missing")
    delivery = payload.get("delivery_evidence")
    receipt = store.self_report_receipt(snapshot.run_id, node_id, role, generation,
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
             and metadata.get("role") == role and metadata.get("task_id") == task_id
             and metadata.get("generation") == generation,
             "handle metadata names another task")
    later = metadata.get("pending_action")
    if later:
        _require(later != receipt["action_id"] and metadata.get("pending_operation") == "wait",
                 "handle has a pending non-wait action")
        later_action = store.action(later)
        _require(later_action.get("run_id") == snapshot.run_id
                 and later_action.get("issue_id") == node_id
                 and later_action.get("role") == role
                 and later_action.get("generation") == generation
                 and later_action.get("sequence", 0) > receipt.get("sequence", 0),
                 "pending wait is not a later wait of this task")
        _require(store.result(later) is None,
                 "a later wait was already answered; run resume instead")
    # A cursor means the normal wait path applies, except in the hybrid state:
    # the handle's pending wait was already retired as answered by this
    # self-report, but the handle still points at it.
    retired = bool(later) and _retired_reason(store, later, snapshot.run_id) is not None
    if binding.cursor not in (None, ""):
        _require(retired, "binding has a cursor; the normal wait path applies")
        _require(_retired_reason(store, later, snapshot.run_id) == _RETIRE_REASON,
                 "pending wait was retired for another reason")
    return {"role": role, "active": active, "handle_id": handle_id, "generation": generation,
            "task_id": task_id, "receipt": receipt, "result": result,
            "metadata": metadata, "later": later, "retired": retired, "store": store}


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
        if not proof["retired"]:
            store.retire_wait(proof["later"], _RETIRE_REASON)
        metadata["pending_action"] = None
        metadata["pending_operation"] = None

    current = snapshot.nodes[node_id]
    current["status"] = "review" if proof["role"] == "reviewer" else "running"
    current["retryable_action"] = None
    current["quarantine"] = None
    current["reason"] = None
    current["recovery_action"] = None
    receipt = proof["receipt"]
    role = proof["role"]
    monitor._record(snapshot, SELF_REPORT_DELIVERY_RECONCILED, {
        "run_id": run_id, "node_id": node_id, "role": role,
        "task_id": proof["task_id"], "generation": proof["generation"],
        "handle_id": proof["handle_id"],
        "action_id": receipt["action_id"], "request_digest": receipt["request_digest"],
        **({"source": "retired_wait_with_cursor"} if proof["retired"] else {}),
    })
    handle = RunHandle(proof["handle_id"])
    claims = {"node_id": node_id, "role": role, "task_id": proof["task_id"],
              "handle_id": proof["handle_id"], "generation": proof["generation"]}
    # The same projection a poll applies to this bound result.
    events = runner._wait_result(handle, metadata, claims, proof["result"])
    _require(len(events) == 1 and events[0].event in _REPLAY_EVENTS[role],
             "bound result does not replay as a delivery")
    monitor._apply_event(snapshot, node_id, proof["handle_id"], events[0], runner)
    monitor._record_topology_projection(snapshot, _ENTRY)
    monitor._refresh_run_status(snapshot)
    save_snapshot(monitor.paths, snapshot)
    return snapshot


_PARKED_SOURCE = "parked_resume"


def _find_handle(store, runner, snapshot, node_id: str, role: str, task_id: str,
                 generation: int):
    """The (handle_id, metadata) a stopped task left behind, or (None, None)."""
    directory = store._directory("handles")
    for path in sorted(directory.glob("*.json")):
        try:
            metadata = store._read(path)
        except (OSError, ValueError):
            continue
        if (isinstance(metadata, dict) and metadata.get("run_id") == snapshot.run_id
                and metadata.get("node_id") == node_id and metadata.get("role") == role
                and metadata.get("task_id") == task_id
                and metadata.get("generation") == generation):
            return path.stem, metadata
    return None, None


def consume_parked_delivery(monitor, snapshot, node_id: str, runner) -> bool:
    """Consume a parked reviewer's unconsumed self-report once, inside resume.

    Returns True when a delivery was consumed and replayed (``snapshot`` was
    mutated; the caller saves), False when there is nothing to do.  Any doubt
    returns False before the first write: the parked node stays parked.
    """
    current = snapshot.nodes.get(node_id)
    if not isinstance(current, dict) or current.get("status") != "blocked_unknown":
        return False
    if (current.get("active_task") is not None or current.get("start_intent") is not None
            or node_id in snapshot.handles
            or isinstance(current.get("retryable_action"), dict)):
        return False
    role = "reviewer"
    generation = current.get("review_generation")
    task_id = current.get(role + "_identity")
    if type(generation) is not int or generation < 1 or not isinstance(task_id, str) or not task_id:
        return False
    if not (current.get("reviewer_replacement_required") is True
            or monitor._primary_role(node_id, current) == "reviewer"):
        return False
    try:
        binding = monitor._load_task_binding(snapshot, node_id, role)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False
    if (binding.task_id != task_id or binding.generation != generation
            or binding.status not in {"stopped", "failed", "archived"}):
        return False
    store = ProviderActionStore(monitor.paths)
    try:
        archive = store._read(store._delivery_path(snapshot.run_id, node_id, role, generation))
    except (OSError, ValueError):
        return False
    if (not isinstance(archive, dict) or archive.get("consumed_by")
            or archive.get("run_id") != snapshot.run_id or archive.get("issue_id") != node_id
            or archive.get("role") != role or archive.get("generation") != generation
            or not isinstance(archive.get("payload"), dict)):
        return False
    handle_id, metadata = _find_handle(store, runner, snapshot, node_id, role, task_id, generation)
    if (handle_id is None or not hasattr(runner, "_issue_wait") or not hasattr(runner, "_wait_result")
            or metadata.get("pending_action")):
        return False
    try:
        action = runner._issue_wait(RunHandle(handle_id), metadata)
    except (KeyError, OSError, TypeError, ValueError, PermissionError, ProviderUnavailable):
        return False
    # Issuing a wait hands an unconsumed archive to it; consume explicitly only
    # when that did not happen.  Either way the archive ends up consumed by
    # exactly this wait, which is what makes the recovery once-only.
    result = store.result(action["action_id"])
    if result is None:
        store._consume_archived_delivery(snapshot.run_id, node_id, role, generation)
        result = store.result(action["action_id"])
    try:
        taken = store._read(store._delivery_path(snapshot.run_id, node_id, role, generation))
    except (OSError, ValueError):
        taken = None
    if (not isinstance(result, dict) or not isinstance(taken, dict)
            or taken.get("consumed_by") != action["action_id"]):
        if result is None:
            store.retire_wait(action["action_id"], "parked_delivery_not_consumed")
            metadata["pending_action"] = None
            metadata["pending_operation"] = None
            store._atomic(runner._handle_path(handle_id), metadata)
        return False
    # Re-register the stopped task only long enough to apply its conclusion.
    current["status"] = "review"
    current["active_role"] = role
    current["active_task"] = {"role": role, "task_id": task_id, "generation": generation,
                              "handle_id": handle_id}
    current["reviewer_replacement_required"] = False
    current["recovery_action"] = None
    current["reason"] = None
    snapshot.handles[node_id] = handle_id
    monitor._record(snapshot, SELF_REPORT_DELIVERY_RECONCILED, {
        "run_id": snapshot.run_id, "node_id": node_id, "role": role,
        "task_id": task_id, "generation": generation, "handle_id": handle_id,
        "action_id": action["action_id"], "source": _PARKED_SOURCE,
    })
    claims = {"node_id": node_id, "role": role, "task_id": task_id,
              "handle_id": handle_id, "generation": generation}
    events = runner._wait_result(RunHandle(handle_id), metadata, claims, result)
    if len(events) != 1:
        monitor._mark_blocked_unknown(snapshot, node_id, "parked delivery did not replay as one event")
        return True
    monitor._apply_event(snapshot, node_id, handle_id, events[0], runner)
    return True
