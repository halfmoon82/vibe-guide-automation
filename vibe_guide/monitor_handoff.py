"""Extracted from monitor.py (behavior unchanged)."""

import json
from typing import Any, Dict, Optional, Tuple

from . import digests, fsio
from .adapters.task_provider import ProviderActionStore
from .contracts import Runner
from .models import DAGNode
from .lifecycle import TERMINAL_TASK_STATUSES
from .state import RunSnapshot, load_events, run_dir, save_snapshot
from .task_registry import load_task_binding


def _identity_from_contract(node: DAGNode, role: str) -> Optional[str]:
    for key in (
        role + "_task_id",
        role + "_platform_task_id",
        role + "_threadId",
        role + "_thread_id",
    ):
        value = node.contract.get(key)
        if value:
            return str(value)
    nested = node.contract.get(role + "_task")
    if isinstance(nested, dict):
        for key in ("task_id", "platform_task_id", "threadId", "thread_id"):
            if nested.get(key):
                return str(nested[key])
    if role == "developer":
        for key in ("task_id", "platform_task_id", "threadId", "thread_id"):
            if node.contract.get(key):
                return str(node.contract[key])
    return None


def _handoff_superseded_identity(
    current: Dict[str, Any], role: str
) -> Optional[str]:
    """The role's current task identity when a platform_handoff superseded it."""
    superseded = current.get("handoff_superseded")
    identity = current.get(role + "_identity")
    if isinstance(superseded, dict) and identity and superseded.get(role) == identity:
        return str(identity)
    return None


def _prove_handoff_predecessor_terminal(
    monitor, snapshot: RunSnapshot, node_id: str, role: str
) -> Optional[Tuple[str, str]]:
    """Archive a terminal pre-handoff task, then prove it stopped.

    Returns ``(predecessor task id, predecessor provider)``, or ``None`` when the old task cannot
    be proven terminal (missing/stale binding, live state); the caller
    then fails closed instead of creating a second writer.  The proof
    itself is the ordinary ``_prove_old_task_stopped_or_absent``.
    """
    current = snapshot.nodes[node_id]
    try:
        binding = monitor._load_task_binding(snapshot, node_id, role)
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return None
    if not binding.task_id or binding.task_id != current.get(role + "_identity"):
        return None
    previous_status = binding.status
    if previous_status in monitor._HANDOFF_ARCHIVABLE_STATUSES[role]:
        try:
            monitor._set_binding_status(snapshot, node_id, role, "archived")
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return None
    predecessor = monitor._prove_old_task_stopped_or_absent(
        snapshot, node_id, {"successor_candidate": True, "role": role}
    )
    if not predecessor:
        return None
    monitor._record(
        snapshot,
        "handoff_predecessor_reconciled",
        {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "role": role,
            "predecessor_task_id": predecessor,
            "previous_binding_status": previous_status,
            "proof": "platform_handoff",
        },
    )
    return predecessor, binding.provider


def _prove_old_task_stopped_or_absent(
    monitor,
    snapshot: RunSnapshot,
    node_id: str,
    retry: Dict[str, Any],
) -> Optional[str]:
    """Return the predecessor identity only when a successor is safe.

    ``None`` means the old task remains ambiguous.  An empty string is a
    durable absence proof: there is no current-run registry binding and
    no provider request that could have created a task.  A terminal
    binding is accepted only for an already provider-confirmed terminal
    state; running/blocked bindings never authorize a new writer.
    """
    current = snapshot.nodes[node_id]
    if retry.get("successor_candidate") is not True:
        return None
    if (
        current.get("active_task") is not None
        or node_id in snapshot.handles
        or current.get("start_intent") is not None
    ):
        return None
    role = retry.get("role")
    if role not in {"developer", "reviewer"}:
        return None
    try:
        binding = monitor._load_task_binding(snapshot, node_id, str(role))
    except FileNotFoundError:
        try:
            requested = ProviderActionStore(monitor.paths).has_request(
                snapshot.run_id, node_id, str(role)
            )
        except (OSError, TypeError, ValueError):
            return None
        return None if requested else ""
    except (OSError, TypeError, ValueError):
        return None
    if binding.status in {"stopped", "failed", "archived"}:
        return binding.task_id or ""
    return None


#: Binding states whose task has handed its work back: same table the
#: successor proof archives (``Monitor._HANDOFF_ARCHIVABLE_STATUSES``).
_DRAINED_STATUSES = {
    "developer": frozenset({"delivered", "accepted"}),
    "reviewer": frozenset({"review", "delivered", "accepted"}),
}
HANDOFF_WORKER_STOPPED_EVENT = "handoff_worker_stopped"


def _registered_stops(events) -> set:
    stops = set()
    for event in events:
        if event.get("event") != HANDOFF_WORKER_STOPPED_EVENT:
            continue
        data = event.get("data") or {}
        stops.add((data.get("node_id"), data.get("role"), data.get("predecessor_task_id")))
    return stops


def _holds_live_task(snapshot: RunSnapshot, node_id: str, current, role: str, task_id: str) -> bool:
    """Whether the run still holds ``task_id`` as the node's live task."""
    active = current.get("active_task")
    if isinstance(active, dict) and (
        active.get("task_id") == task_id
        or (active.get("role") == role and not active.get("task_id"))
    ):
        return True
    return node_id in snapshot.handles and current.get("active_role") == role


def _pre_move_identities(snapshot: RunSnapshot, events, to_platform) -> Optional[Dict[str, Any]]:
    """Per node, the task identities bound *before* the run moved to ``to_platform``.

    ``None`` unless the run already recorded a platform_handoff
    reauthorization and is now authorized on ``to_platform``: a retry of a
    handoff that was interrupted after the run moved, or a re-run of one that
    finished.  Then the new platform's own tasks -- created after the move --
    are not old-platform workers.  The tasks bound before the move are exactly
    the node's ``handoff_superseded`` identities (written by that
    reauthorization), so only those keep blocking.
    """
    if not to_platform:
        return None
    authorization = snapshot.authorization if isinstance(snapshot.authorization, dict) else {}
    if authorization.get("agent_id") != to_platform:
        return None
    if not any(
        event.get("event") == "authorization_reauthorized"
        and (event.get("data") or {}).get("change_reason") == "platform_handoff"
        for event in events
    ):
        return None
    return {
        node_id: dict(current.get("handoff_superseded") or {})
        for node_id, current in snapshot.nodes.items()
        if isinstance(current, dict)
    }


def handoff_drain_blockers(paths, snapshot: RunSnapshot, unfinished, to_platform=None) -> list:
    """Old-platform workers a handoff must not run past (PRD 5.2.6 D2).

    Read-only.  A worker is drained when its task binding has handed the work
    back (delivered/accepted/review) and the run no longer holds it as the
    node's live task, or ended (archived/failed/stopped/
    blocked_design), or when a stop was registered for exactly that task.  A
    create nobody claimed is not a worker: the handoff hides it.  Everything
    else -- a running binding, an unreadable registry, an identity with no
    binding, a claimed create -- cannot be shown to have stopped and blocks.

    When the run already moved to ``to_platform`` (an interrupted handoff
    being retried), a task the new platform created after the move is not an
    old-platform worker and never blocks; tasks bound before the move still
    must be drained (``_pre_move_identities``).
    """
    store = ProviderActionStore(paths)
    events = load_events(paths, snapshot.run_id)[: snapshot.event_sequence]
    stops = _registered_stops(events)
    pre_move = _pre_move_identities(snapshot, events, to_platform)
    confirmed = {
        (event.get("data") or {}).get("node_id")
        for event in events if event.get("event") == "start_confirmed"
    }
    blockers = []
    for node_id in sorted(unfinished):
        current = snapshot.nodes.get(node_id) or {}
        intent = current.get("start_intent") if isinstance(current.get("start_intent"), dict) else None
        seen = False
        for role in ("developer", "reviewer"):
            identity = current.get(role + "_identity")
            role_intent = intent if intent and intent.get("role") == role else None
            if not identity and role_intent is None:
                continue
            seen = True
            task_id = str(identity or role_intent.get("task_id") or "")
            entry = {"node_id": node_id, "role": role, "task_id": task_id}
            if (node_id, role, task_id) in stops:
                continue
            if pre_move is not None and (pre_move.get(node_id) or {}).get(role) != task_id:
                continue  # created on the new platform after the run moved
            try:
                binding = load_task_binding(paths, node_id, role, run_id=snapshot.run_id)
            except FileNotFoundError:
                binding = None
            except (OSError, TypeError, ValueError):
                blockers.append(dict(entry, status="unknown", provider="",
                                     reason="任务登记读不出，无法判定是否已停"))
                continue
            if binding is not None and binding.task_id == task_id:
                if binding.status in TERMINAL_TASK_STATUSES:
                    continue
                # A handed-back status alone is not proof: a running reviewer's
                # binding already reads "review" (``Monitor._running_status``),
                # the same value its consumed delivery leaves.  Only when the
                # run no longer holds this task as its live one is it drained.
                if binding.status in _DRAINED_STATUSES[role] and not _holds_live_task(
                        snapshot, node_id, current, role, task_id):
                    continue
                blockers.append(dict(entry, status=binding.status, provider=binding.provider or "",
                                     reason="任务登记仍是 {}".format(binding.status)))
                continue
            if binding is None and role_intent is not None and node_id not in confirmed:
                try:
                    claim = store.claim_evidence(snapshot.run_id, node_id)
                except (OSError, TypeError, ValueError):
                    claim = "unreadable"
                if claim is None:
                    continue  # unclaimed create: hidden by the handoff
                blockers.append(dict(entry, status="start_pending", provider="",
                                     reason="信箱里的创建请求已被领取（{}），会话可能已在写".format(claim)))
                continue
            blockers.append(dict(entry, status="unknown", provider=getattr(binding, "provider", "") or "",
                                 reason="运行记着该任务，但任务登记里没有对应的绑定，无法判定是否已停"))
        if seen:
            continue
        if current.get("active_task") or node_id in snapshot.handles:
            blockers.append({"node_id": node_id, "role": str(current.get("active_role") or "developer"),
                             "task_id": str((current.get("active_task") or {}).get("task_id") or ""),
                             "status": "unknown", "provider": "",
                             "reason": "运行仍挂着活动句柄，无法判定是否已停"})
            continue
        # No identity recorded yet, but the mailbox shows a claimed request
        # (a create answered, a lock, a session record): a session may exist.
        try:
            claim = store.claim_evidence(snapshot.run_id, node_id)
        except (OSError, TypeError, ValueError):
            claim = "unreadable"
        if claim is not None or node_id in confirmed:
            blockers.append({"node_id": node_id, "role": "developer", "task_id": "",
                             "status": "in_flight", "provider": "",
                             "reason": "信箱里有该节点已被领取的请求（{}），会话可能已在写".format(
                                 claim or "start_confirmed")})
    return blockers


def record_handoff_worker_stopped(monitor, run_id: str, package: Dict[str, Any]) -> RunSnapshot:
    """Register that one old-platform worker has stopped, for a handoff.

    The stop is the operator's declaration (the old platform may no longer
    open at all), recorded with how it was established.  The task binding,
    when there is one, turns ``stopped`` so the later successor proof sees a
    terminal predecessor; the event is the evidence a re-run of the handoff
    reads.  Nothing is dispatched and no other node is touched.
    """
    from .handoff_rebind import STOP_EVIDENCE_FIELDS, STOP_EVIDENCE_HOW, STOP_EVIDENCE_KIND

    if not isinstance(package, dict) or set(package) != set(STOP_EVIDENCE_FIELDS):
        raise ValueError(
            "handoff_worker_stopped package must have exactly: " + ", ".join(STOP_EVIDENCE_FIELDS)
        )
    if package["kind"] != STOP_EVIDENCE_KIND:
        raise ValueError("handoff_worker_stopped package kind is invalid")
    node_id, role, task_id = package["node_id"], package["role"], package["task_id"]
    if node_id not in monitor.nodes:
        raise ValueError("handoff_worker_stopped names a node outside the run")
    if role not in ("developer", "reviewer"):
        raise ValueError("handoff_worker_stopped role must be developer or reviewer")
    if package["how_stopped"] not in STOP_EVIDENCE_HOW:
        raise ValueError("handoff_worker_stopped how_stopped must be one of: " + ", ".join(STOP_EVIDENCE_HOW))
    for key in ("task_id", "stopped_on", "note"):
        value = package[key]
        if not isinstance(value, str) or not value.strip() or value.startswith("<"):
            raise ValueError("handoff_worker_stopped {} must be filled in".format(key))
    monitor._reset_binding_cache()
    snapshot = monitor_load_snapshot(monitor, run_id)
    current = snapshot.nodes[node_id]
    if current.get("status") == "accepted":
        raise ValueError("handoff_worker_stopped: node is already accepted")
    intent = current.get("start_intent") if isinstance(current.get("start_intent"), dict) else {}
    known = {current.get(role + "_identity")}
    if intent.get("role") == role:
        known.add(intent.get("task_id"))
    if task_id not in known:
        raise ValueError("handoff_worker_stopped task_id is not this node's current {} task".format(role))
    previous_status = ""
    try:
        binding = monitor._load_task_binding(snapshot, node_id, role)
    except FileNotFoundError:
        binding = None
    if binding is not None and binding.task_id == task_id:
        previous_status = binding.status
        if binding.status not in TERMINAL_TASK_STATUSES:
            monitor._set_binding_status(snapshot, node_id, role, "stopped")
    active = current.get("active_task")
    if isinstance(active, dict) and active.get("role") == role and active.get("task_id") == task_id:
        # The same release ``_stop_active_for_transition`` makes after a
        # provider-confirmed stop; here the stop happened on the old platform,
        # whose handle the new platform's runner cannot stop again.
        snapshot.handles.pop(node_id, None)
        current["active_role"] = None
        current["active_task"] = None
        current["old_task_reconciled"] = True
    monitor._record(
        snapshot,
        HANDOFF_WORKER_STOPPED_EVENT,
        {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "role": role,
            "predecessor_task_id": task_id,
            "previous_binding_status": previous_status,
            "source": package["stopped_on"],
            "reason": package["how_stopped"],
            "proof": {"how_stopped": package["how_stopped"], "stopped_on": package["stopped_on"],
                      "note": package["note"]},
        },
    )
    monitor._save_snapshot(snapshot)
    return snapshot


def monitor_load_snapshot(monitor, run_id):
    from .state import load_snapshot

    return load_snapshot(monitor.paths, run_id)


def _hide_stale_handoff_creates(monitor, snapshot: RunSnapshot, runner: Runner) -> None:
    """Hide the old platform's unclaimed creates of every unfinished node.

    Independent of the node's lease: a dispatch that failed once released
    its lease, so the lease-rebind path never runs for it, yet its old
    create is still pending beside the new platform's.  Only requests
    written under an authorization a platform_handoff replaced, by a
    provider other than the one now dispatching, and that nothing claimed
    are hidden; the new platform's own create is never matched (its
    provider is the runner's).  Idempotent, so a
    re-run of the handoff heals a mailbox an earlier version left with two.
    """
    provider = getattr(runner, "provider", None)
    replaced = monitor._handoff_replaced_authorizations(snapshot)
    if not isinstance(provider, str) or not provider or not replaced:
        return
    store = ProviderActionStore(monitor.paths)
    for node_id in sorted(snapshot.nodes):
        if snapshot.nodes[node_id].get("status") == "accepted":
            continue
        try:
            slots = [
                (digest, role, generation)
                for digest in replaced
                for role, generation in store.unresolved_create_slots(
                    snapshot.run_id, node_id, digest, provider
                )
            ]
            if not slots or not monitor._node_never_claimed(snapshot, node_id, store):
                continue
            for digest, role, generation in slots:
                superseded = store.supersede_unclaimed_creates(
                    snapshot.run_id, node_id, role, generation, digest, provider
                )
                if superseded:
                    monitor._record(
                        snapshot,
                        "handoff_stale_creates_hidden",
                        {
                            "run_id": snapshot.run_id,
                            "node_id": node_id,
                            "role": role,
                            "generation": generation,
                            "superseded_creates": superseded,
                        },
                    )
        except (OSError, TypeError, ValueError):
            continue


def reconcile_package(monitor, run_id, package, *, runner_factory, invalidation_path, authorization_digest):
    """Route a reconciliation package by ``kind`` to the matching recovery.

    ``runner_factory`` builds the runner lazily (only the kinds that dispatch
    need one); ``invalidation_path`` is the plan's invalidation marker and
    ``authorization_digest`` the digest of the card currently published.
    """
    kind = package.get("kind") if isinstance(package, dict) else None
    if kind == HANDOFF_WORKER_STOPPED_EVENT:
        return record_handoff_worker_stopped(monitor, run_id, package)
    if kind == "scope_correction":
        return monitor.reconcile_scope(run_id, package)
    if kind == "created_successor":
        required = {"kind", "node_id", "action_id", "request_digest",
                    "predecessor_archive", "preservation_path", "preservation_digest"}
        if set(package) != required or package["node_id"] not in monitor.nodes:
            raise ValueError("created successor reconciliation package is invalid")
        snapshot = monitor.reconcile_created_successor(run_id, package["node_id"],
            runner_factory(), **{key: package[key] for key in required - {"kind", "node_id"}})
        if invalidation_path.exists():
            # Recheck the unchanged authorization and corrected runtime
            # projection; the old marker's prose is never permission.
            monitor._require_snapshot_authorization(snapshot)
            monitor._validate_execution_topology(snapshot)
            if invalidation_path.is_symlink() or authorization_digest != snapshot.authorization_digest:
                raise ValueError("reconciled authorization does not match the current card")
            raw_invalidation = invalidation_path.read_bytes()
            marker = json.loads(raw_invalidation)
            if (not isinstance(marker, dict) or marker.get("status") != "blocked_design"
                    or marker.get("change_reason") != "executable_contract_changed"):
                raise ValueError("invalidation requires a separate authorization decision")
            digest = digests.sha256_bytes(raw_invalidation)
            archive = run_dir(monitor.paths, run_id) / "recovery" / ("invalidation-" + digest + ".json")
            if archive.parent.is_symlink() or archive.is_symlink():
                raise ValueError("invalidation archive may not traverse a symlink")
            fsio.atomic_write_bytes(archive, raw_invalidation)
            monitor._record_topology_projection(snapshot, "monitor.reconcile_successor",
                recovery_evidence={"invalidation_path": str(archive),
                    "invalidation_digest": digest, "authorization_digest": snapshot.authorization_digest})
            save_snapshot(monitor.paths, snapshot)
            if invalidation_path.read_bytes() != raw_invalidation:
                raise ValueError("invalidation changed during reconciliation")
            invalidation_path.unlink()
        return snapshot
    if kind == "adopted_dispatch":
        from .orphan_recovery import ADOPTED_DISPATCH_PACKAGE, adopt_orphan_dispatch
        if set(package) != ADOPTED_DISPATCH_PACKAGE or package["node_id"] not in monitor.nodes:
            raise ValueError("adopted dispatch reconciliation package is invalid")
        if invalidation_path.exists() or invalidation_path.is_symlink():
            raise ValueError("authorization is invalidated; reauthorize before adopting")
        return adopt_orphan_dispatch(monitor, run_id, package["node_id"],
                                     package["action_id"], runner_factory())
    if kind == "self_reported_delivery":
        from .self_report_recovery import SELF_REPORTED_DELIVERY_PACKAGE, reconcile_self_reported_delivery
        if set(package) != SELF_REPORTED_DELIVERY_PACKAGE or package["node_id"] not in monitor.nodes:
            raise ValueError("self-reported delivery reconciliation package is invalid")
        if invalidation_path.exists() or invalidation_path.is_symlink():
            raise ValueError("authorization is invalidated; reauthorize before reconciling")
        return reconcile_self_reported_delivery(monitor, run_id, package["node_id"], runner_factory())
    return monitor.reconcile_evidence(run_id, package)


def record_event(monitor, snapshot, event_name, data):
    """Public entry for callers outside ``Monitor`` that must append a run event."""
    monitor._record(snapshot, event_name, data)


def current_plan_digests(monitor):
    """Public entry for the (prd, spec) digests the monitor currently sees."""
    return monitor._current_plan_digests()
