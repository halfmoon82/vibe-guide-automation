"""Extracted from monitor.py (behavior unchanged)."""

import json
from typing import Any, Dict, Optional, Tuple

from . import digests, fsio
from .adapters.task_provider import ProviderActionStore
from .contracts import Runner
from .models import DAGNode
from .state import RunSnapshot, run_dir, save_snapshot


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
