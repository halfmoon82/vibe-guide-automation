"""Adopt a dispatched-but-unregistered developer session back into its run.

A provider ``create`` can leave the supervisor without its ``start_intent`` /
``start_confirmed`` reaching the formal run (2026-10-13: a supervisor ran
trunk sources, then the run was reauthorized).  The visible session keeps
working, but the node sits at ``blocked_unknown`` with generation 0 and a
quarantined lease carrying the old intent digest, which refuses every new
writer.  This recovery re-binds that same session, never a new one, and only
from structured evidence on disk:

* the run's own provider ``create`` request and its bound result (task id and
  host), issued under an authorization this run actually held, for a node the
  later reauthorizations did not change;
* the dispatch baseline recorded with that request;
* exactly one same-run, same-generation quarantined lease on the writer root;
* a node with no registered writer, and its worktree on the contract branch.

Anything missing or contradictory raises ``ValueError`` before the first
write; the node stays ``blocked_unknown``.
"""
import json
import subprocess
import uuid
from typing import Any, Dict, Optional

from .adapters.task_provider import ProviderActionStore
from .providers import CODEX_PROVIDER
from .state import (
    RunSnapshot,
    load_events,
    load_snapshot,
    rebind_writer_lease,
    save_snapshot,
)
from .task_registry import TaskBinding, load_task_binding

ORPHAN_DISPATCH_ADOPTED = "orphan_dispatch_adopted"
ADOPTED_DISPATCH_PACKAGE = frozenset({"kind", "node_id", "action_id"})
_ROLE = "developer"
_ENTRY = "monitor.reconcile_adopted_dispatch"


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ValueError("orphan dispatch adoption: " + reason)


def _authorization_lineage(paths, run_id: str, node_id: str, digest: str, current: str) -> None:
    """The request digest was held by this run and the node is unchanged since."""
    held = False
    active = None
    for record in load_events(paths, run_id):
        data = record.get("data") or {}
        if record.get("event") == "run_started":
            active = data.get("authorization_digest")
        elif record.get("event") == "authorization_reauthorized":
            if held and node_id in set(data.get("affected_nodes") or []) | set(data.get("changed_nodes") or []):
                _require(False, "node contract changed after the dispatch authorization")
            active = data.get("authorization_digest")
        else:
            continue
        held = held or active == digest
    _require(held, "request authorization was never held by this run")
    _require(active == current, "run authorization history does not end at the current card")


def _leases_on(paths, node_id: str, worktree: str):
    leases = []
    for path in sorted((paths.vibe / "leases").glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _require(False, "a writer lease is unreadable")
        if not isinstance(data, dict):
            _require(False, "a writer lease is malformed")
        if data.get("status") in {"active", "quarantined"} and (
                data.get("node_id") == node_id or data.get("worktree") == worktree):
            leases.append(data)
    return leases


def _collect(monitor, snapshot: RunSnapshot, record, node_id: str, action_id: str, runner) -> Dict[str, Any]:
    paths = monitor.paths
    _require(node_id in snapshot.nodes and node_id in monitor.nodes, "node is unknown")
    current = snapshot.nodes[node_id]
    retry = current.get("retryable_action")
    _require(current.get("status") == "blocked_unknown", "node is not blocked_unknown")
    _require(current.get("active_task") is None and current.get("start_intent") is None
             and node_id not in snapshot.handles, "node already has a registered writer")
    _require(retry is None or (isinstance(retry, dict) and retry.get("role") == _ROLE
                               and not retry.get("successor") and not retry.get("continuation")),
             "node has a pending retry of another kind")

    store = ProviderActionStore(paths)
    action = store.action(action_id)
    generation = action.get("generation")
    _require(action.get("operation") == "create" and action.get("run_id") == snapshot.run_id
             and action.get("issue_id") == node_id and action.get("role") == _ROLE,
             "action is not this run's developer create for the node")
    _require(action.get("provider") == getattr(runner, "provider", None),
             "action provider does not match the run's provider")
    _require(type(generation) is int and generation >= 1
             and current.get("developer_generation", 0) == generation - 1,
             "action generation is not the node's next generation")
    _require(store._latest_generation(snapshot.run_id, node_id, _ROLE) == generation,
             "a later generation was already dispatched")
    _require(current.get("developer_identity") is None, "node already names a developer task")
    for path in store._directory("requests").glob("action-*.json"):
        other = store._read(path)
        _require(not (isinstance(other, dict) and other.get("action_id") != action_id
                      and other.get("operation") == "create"
                      and other.get("run_id") == snapshot.run_id
                      and other.get("issue_id") == node_id and other.get("role") == _ROLE
                      and other.get("generation") == generation),
                 "more than one create exists for this generation")

    request = action["request"]
    child = request.get("child_binding")
    _require(isinstance(child, dict), "create request has no child binding")
    _require(child.get("parent_run_id") == snapshot.run_id and child.get("node_id") == node_id
             and child.get("role") == _ROLE, "child binding names another run or node")
    _require(child.get("worktree") == current.get("worktree")
             and child.get("branch") == current.get("branch"),
             "child binding names another writer root")
    _require(child.get("capability_contract_digest") == snapshot.capability_contract_digest,
             "child binding capability contract is stale")
    digest = request.get("authorization_digest")
    _require(isinstance(digest, str) and digest == child.get("authorization_digest"),
             "create request authorization is inconsistent")
    _require(request.get("plan_id") == monitor.plan.plan_id
             and request.get("plan_version") == monitor.plan.version,
             "create request names another plan")
    _authorization_lineage(paths, snapshot.run_id, node_id, digest, record.digest)
    topology = monitor._node_dispatch_topology(monitor.nodes[node_id])
    _require(request.get("topology", topology) == topology,
             "create request topology differs from the current ruling")
    allowlist = child.get("allowlist")
    _require(isinstance(allowlist, list) and all(isinstance(item, str) for item in allowlist),
             "child binding allowlist is invalid")

    result = store.result(action_id)
    binding = result.get("binding") if isinstance(result, dict) else None
    _require(isinstance(binding, dict), "create has no bound result")
    task_id = binding.get("threadId") or binding.get("task_id")
    host = binding.get("hostId") or binding.get("host")
    _require(isinstance(task_id, str) and task_id and isinstance(host, str) and host,
             "create result has no task or host identity")
    for operation, expected in (("locate", {"threadId": task_id}),
                                ("visibility", {"targets": [{"threadId": task_id, "hostId": host}]})):
        related = store.related_action(action, operation, required=False)
        if related is None:
            continue
        for key, value in expected.items():
            _require(related["request"].get(key) == value, operation + " names another task")
        observed = store.result(related["action_id"])
        if observed is not None:
            flag = "located" if operation == "locate" else "visible"
            _require(observed.get(flag) is True, operation + " result contradicts the create")
    for other_id, other in snapshot.nodes.items():
        _require(task_id not in (other.get("developer_identity"), other.get("reviewer_identity")),
                 "task id is already bound to " + other_id)
    try:
        existing = load_task_binding(paths, node_id, _ROLE, run_id=snapshot.run_id)
    except FileNotFoundError:
        existing = None
    _require(existing is None or (existing.task_id == task_id and existing.generation == generation
                                  and existing.status == "start_pending"),
             "node already has another developer task binding")

    head = store._dispatch_record(snapshot.run_id, node_id, _ROLE, generation).get("head")
    _require(isinstance(head, str) and head, "dispatch baseline is missing")

    worktree = str(current.get("worktree"))
    root = monitor._worktree_path(current)
    try:
        observed_branch = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        observed_branch = None
    _require(observed_branch == current.get("branch"), "worktree is not on the contract branch")
    for other_id, other in snapshot.nodes.items():
        _require(other_id == node_id or not (other.get("active_task") or other_id in snapshot.handles)
                 or monitor._worktree_path(other).resolve() != root.resolve(),
                 "writer root is active for " + other_id)

    contract = dict(monitor.nodes[node_id].contract)
    contract.update(current.get("contract_overrides", {}))
    intent_digest = monitor._start_intent_digest(
        snapshot, node_id, _ROLE, task_id, generation, contract, current)
    leases = _leases_on(paths, node_id, worktree)
    _require(len(leases) == 1, "expected exactly one lease on the writer root")
    lease = leases[0]
    _require(lease.get("node_id") == node_id and lease.get("worktree") == worktree
             and lease.get("run_id") == snapshot.run_id and lease.get("role") == _ROLE
             and lease.get("generation") == generation,
             "lease belongs to another run, role or generation")
    _require(lease.get("status") == "quarantined"
             or (lease.get("status") == "active" and lease.get("intent_digest") == intent_digest),
             "lease is held by another intent")
    return {
        "action": action, "child": child, "task_id": task_id, "host": host,
        "generation": generation, "head": head, "topology": topology,
        "intent_digest": intent_digest, "worktree": worktree,
        # None when an interrupted adoption already moved the lease.
        "previous_intent_digest": lease.get("intent_digest") if lease.get("status") == "quarantined" else None,
        "request_authorization_digest": digest, "existing": existing,
    }


def adopt_orphan_dispatch(monitor, run_id: str, node_id: str, action_id: str, runner) -> RunSnapshot:
    monitor._reset_binding_cache()
    snapshot = load_snapshot(monitor.paths, run_id)
    record = monitor._require_snapshot_authorization(snapshot)
    # An adoption interrupted before its snapshot save is replayed first, so
    # it is seen as a registered writer rather than adopted a second time.
    monitor._reconcile_unapplied_events(snapshot)
    proof = _collect(monitor, snapshot, record, node_id, action_id, runner)
    monitor._validate_execution_topology(snapshot)
    current = snapshot.nodes[node_id]
    generation = proof["generation"]
    task_id = proof["task_id"]
    child = proof["child"]

    if proof["previous_intent_digest"] is not None:
        _require(rebind_writer_lease(
            monitor.paths, node_id, proof["worktree"], run_id, _ROLE, generation,
            proof["previous_intent_digest"], proof["intent_digest"]),
            "lease changed while it was being re-bound")
    binding = proof["existing"] or TaskBinding(
        provider=runner.provider, mode="visible", issue_id=node_id, role=_ROLE,
        topology=proof["topology"], task_id=task_id, host=proof["host"],
        worktree=proof["worktree"], branch=str(current.get("branch", "")),
        status_file=str(current.get("status_file", "")),
        handoff_file=str(current.get("handoff_file", "")),
        threadId=task_id if runner.provider == CODEX_PROVIDER else None,
        hostId=proof["host"] if runner.provider == CODEX_PROVIDER else None,
        run_id=run_id, status="start_pending", visible=True, generation=generation,
        allowlist=list(child["allowlist"]),
        capability_contract_digest=snapshot.capability_contract_digest,
        route_digest=child.get("route_digest"), model=child.get("model"),
        reasoning=child.get("reasoning"),
    )
    monitor._save_task_binding(binding)
    snapshot.tasks["{}:{}".format(node_id, _ROLE)] = binding.to_dict()

    current["developer_identity"] = task_id
    current["developer_generation"] = generation
    current["retryable_action"] = None
    monitor._record(snapshot, ORPHAN_DISPATCH_ADOPTED, {
        "run_id": run_id, "node_id": node_id, "role": _ROLE,
        "task_id": task_id, "host": proof["host"], "generation": generation,
        "action_id": action_id, "request_digest": proof["action"]["request_digest"],
        "request_authorization_digest": proof["request_authorization_digest"],
        "intent_digest": proof["intent_digest"],
        "previous_intent_digest": proof["previous_intent_digest"],
        "dispatch_head": proof["head"],
    })
    intent = {"intent_id": uuid.uuid4().hex, "role": _ROLE, "phase": "develop",
              "task_id": task_id, "generation": generation,
              "intent_digest": proof["intent_digest"]}
    current["status"] = "start_pending"
    current["active_role"] = _ROLE
    current["active_task"] = {"role": _ROLE, "task_id": task_id,
                              "generation": generation, "handle_id": None}
    current["start_intent"] = intent
    monitor._record(snapshot, "start_intent",
                    {"run_id": run_id, "node_id": node_id, **intent}, current["active_task"])
    # Every save keeps the started set in step with the recorded topology;
    # resume treats a drift as an invalidated authorization.
    monitor._record_topology_projection(snapshot, _ENTRY)
    save_snapshot(monitor.paths, snapshot)

    # The provider runner only persists handle metadata for a non-continuation
    # start: no message reaches the working session.
    handle = runner.start({
        "node_id": node_id, "role": _ROLE, "generation": generation, "run_id": run_id,
        "task_id": task_id, "continuation": False, "successor": False,
        "dispatcher": "monitor", "plan_id": monitor.plan.plan_id,
        "plan_version": monitor.plan.version,
        "complexity_band": getattr(monitor.plan, "complexity_band", ""),
        "execution_engine": getattr(record, "execution_engine", "monitor"),
        "engine_mode": getattr(record, "engine_mode", "parallel"),
        "engine_evidence_ref": getattr(record, "engine_evidence_ref", ""),
        "authorization_digest": record.digest,
        "capability_contract_digest": snapshot.capability_contract_digest,
    }, monitor._worktree_path(current))
    _require(isinstance(handle.run_id, str) and handle.run_id
             and handle.run_id not in snapshot.handles.values(),
             "provider handle is invalid or duplicated")
    monitor._set_binding_status(snapshot, node_id, _ROLE, "running")
    current["active_task"]["handle_id"] = handle.run_id
    current["start_intent"] = None
    current["status"] = "running"
    current["quarantine"] = None
    current["reason"] = None
    snapshot.handles[node_id] = handle.run_id
    monitor._record(snapshot, "start_confirmed", {
        "run_id": run_id, "node_id": node_id, "role": _ROLE, "phase": "develop",
        "task_id": task_id, "generation": generation, "handle_id": handle.run_id,
    }, current["active_task"])
    monitor._record_topology_projection(snapshot, _ENTRY)
    monitor._refresh_run_status(snapshot)
    save_snapshot(monitor.paths, snapshot)
    return snapshot


def replay_orphan_adoption(monitor, snapshot: RunSnapshot, node_id: str,
                           data: Dict[str, Any], provenance: Dict[str, Any]) -> None:
    """Re-apply the identity half of an adoption whose snapshot was lost.

    The following ``start_intent`` / ``start_confirmed`` events replay through
    their normal handlers once the node names the adopted task again.
    """
    current = snapshot.nodes[node_id]
    generation: Optional[int] = data.get("generation")
    _require(provenance.get("role") == "system", "replay lacks system provenance")
    _require(type(generation) is int and current.get("developer_generation", 0) == generation - 1
             and current.get("active_task") is None and node_id not in snapshot.handles,
             "replay does not follow the recorded node state")
    binding = load_task_binding(monitor.paths, node_id, _ROLE, run_id=snapshot.run_id)
    _require(binding.task_id == data.get("task_id") and binding.generation == generation,
             "replay task binding differs from the adoption")
    current["developer_identity"] = binding.task_id
    current["retryable_action"] = None
