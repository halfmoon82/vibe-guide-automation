"""Protected same-plan/run contract amendments (r4 P1).

A confirmed plan's business contract can be amended only through a public,
protected path -- never by editing the live artifacts or starting a new
plan/run:

``propose``
    reads the business-only amendment spec, derives the engineering fields
    (unchanged node identities, worktrees, topology, card options), renders
    the diff, the DAG audit and a *candidate* authorization card under
    ``plans/<plan>/amendments/<digest>/`` and writes the immutable
    ``candidate.json``.  Nothing live changes and no task is dispatched.

``show``
    renders the candidate card with its dependency graph and records a
    display receipt bound to the candidate digest, exactly the way
    ``vibe authorize --show-card`` binds plan version + card + graph.

``confirm``
    only with the candidate-bound token (``AMEND <digest>``) and inside one
    protected transaction: re-verify lineage (live card/record/run pointer),
    owner, task bindings, writer leases and pending provider side effects;
    write the ``authorization_reauthorized`` transfer event; then swap the
    plan artifacts in.  A crash before the event restores the complete old
    state; a crash after it idempotently finishes the new projection
    (``recover``).  While the transfer marker is present every execution
    entry point fails closed (``require_no_pending_transfer``).

The same node set, plan version, run id, task identities and cursors are
preserved; old records stay under ``amendments/<digest>/``.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .adapters.task_provider import ProviderActionStore
from .authorization import (
    AuthorizationCard,
    AuthorizationRecord,
    _canonical_digest,
    authorize,
    refresh_authorization_card,
)
from .authorize_entry import (
    AuthorizationDenied,
    _atomic_json,
    materialize_workflow_evidence,
)
from .dag import dependency_graph, dependency_graph_digest
from .digests import sha256_file as _artifact_hash
from .fsio import JsonCorruptError, JsonMissingError, read_json as _fsio_read_json
from .diagnostics import assert_planning_gate, require_execution_ready
from .models import DAGNode, Plan
from .node_spec import (
    complete_node_contracts,
    reject_engineering_fields,
    render_prd_markdown,
)
from .paths import ProjectPaths
from .planner import PRD, DecisionCard, approve_prd
from .state import interprocess_lock, load_events, load_snapshot, save_snapshot

AMENDMENTS_DIRNAME = "amendments"
TRANSFER_MARKER = ".amendment-transfer.json"
DISPLAY_RECEIPT_NAME = "amendment-display-receipt.json"
CHANGE_REASON = "plan_amendment"
TOKEN_PREFIX = "AMEND "

# Node-level fields a business amendment may change.  Everything else on a
# node -- engineering contract keys, worker/worktree/branch identities --
# must come back identical after the engineering defaults are re-derived.
_BUSINESS_NODE_KEYS = ("title", "depends_on", "integration_after",
                       "parallel_group")
_BUSINESS_CONTRACT_KEYS = ("input", "output", "error_behavior",
                           "acceptance_example", "files", "dependency_reasons")

#: Snapshot node statuses that mean a live or unproven executor exists.
_ACTIVE_NODE_STATUSES = frozenset({
    "running", "review", "rework", "start_pending", "dispatching",
    "awaiting_supervisor", "blocked_unknown",
})
_TERMINAL_BINDING_STATUSES = frozenset({
    "archived", "complete", "terminated", "closed", "replaced",
})


def project_paths(root: Any) -> ProjectPaths:
    """``ProjectPaths`` for an explicit project root (fixtures and the CLI)."""
    return ProjectPaths(Path(str(root)))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _plan_root(paths: ProjectPaths, plan_id: str) -> Path:
    return paths.resolve_vibe_path(Path("plans") / plan_id)


def amendments_dir(paths: ProjectPaths, plan_id: str) -> Path:
    return _plan_root(paths, plan_id) / AMENDMENTS_DIRNAME


def candidate_dir(paths: ProjectPaths, plan_id: str, amendment_digest: str) -> Path:
    return amendments_dir(paths, plan_id) / amendment_digest


def barrier_path(paths: ProjectPaths, plan_id: str) -> Path:
    return _plan_root(paths, plan_id) / TRANSFER_MARKER


def _read_artifact(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ValueError("required artifact is missing: " + path.name)
    try:
        return _fsio_read_json(path)
    except (JsonMissingError, JsonCorruptError) as error:
        raise ValueError("required artifact is unreadable: " + path.name) from error


def _plan_data(plan_root: Path) -> Dict[str, Any]:
    data = _read_artifact(plan_root / "plan.json")
    if not isinstance(data, dict):
        raise ValueError("plan.json must be an object")
    return data


def _plan_object(plan_root: Path, data: Dict[str, Any]) -> Plan:
    nodes = [DAGNode.from_dict(dict(item))
             for item in _read_artifact(plan_root / "nodes.json")]
    return Plan(
        data["plan_id"], int(data["version"]), data["prd_path"],
        list(data["node_ids"]), data["status"],
        authorization_required=bool(data.get("authorization_required")),
        decisions=list(data.get("decisions") or []),
        nodes=nodes,
        complexity_band=str(data.get("complexity_band") or ""),
        spec_path=str(data.get("spec_path") or ""),
        integration_contract=dict(data.get("integration_contract") or {}),
        evidence_priority=list(data.get("evidence_priority")
                               or ["verified_fact", "spec", "self_report"]),
    )


def _load_record(plan_root: Path) -> AuthorizationRecord:
    path = plan_root / "authorization.json"
    if not path.is_file():
        raise ValueError(
            "amendment requires an authorized plan: authorization.json missing")
    return AuthorizationRecord.from_dict(_read_artifact(path))


def _load_card(plan_root: Path) -> AuthorizationCard:
    from .cli import _authorization_card  # call-time: cli imports this module
    return _authorization_card(_read_artifact(plan_root / "authorization-card.json"))


def _current_run_id(plan_root: Path) -> Optional[str]:
    pointer = plan_root / "current-run.json"
    if not pointer.is_file() or pointer.is_symlink():
        return None
    try:
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("current-run.json is unreadable")
    run_id = data.get("run_id") if isinstance(data, dict) else None
    if not isinstance(run_id, str) or not run_id.startswith("run-"):
        raise ValueError("current-run.json does not name a run")
    return run_id


def _node_map(plan_root: Path) -> Dict[str, Dict[str, Any]]:
    raw = _read_artifact(plan_root / "nodes.json")
    if not isinstance(raw, list) or not raw:
        raise ValueError("nodes.json must be a non-empty list")
    return {item["id"]: item for item in raw}


def _validate_amendment_spec(
    spec: Any, previous_nodes: Dict[str, Dict[str, Any]],
) -> List[DecisionCard]:
    """Business-only spec over the identical node set; returns its decisions."""
    reject_engineering_fields(spec)
    if not isinstance(spec.get("reason", None), (type(None), str)):
        raise ValueError("amendment reason must be a string")
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("amendment spec must carry the full node list")
    offered = {item.get("id") for item in nodes if isinstance(item, dict)}
    if len(offered) != len(nodes) or any(not item for item in offered):
        raise ValueError("every amendment node needs an id")
    if offered != set(previous_nodes):
        raise ValueError(
            "contract amendments keep the node set identical; "
            "nodes cannot be added, removed or renamed")
    for item in nodes:
        contract = item.get("contract")
        if not isinstance(contract, dict):
            raise ValueError("every amendment node needs a contract object")
        unknown = set(contract) - set(_BUSINESS_CONTRACT_KEYS)
        if unknown:
            raise ValueError(
                "以下字段由系统自动生成，请从产品 spec 中删除：contract."
                + ", contract.".join(sorted(unknown)))
    decisions = [DecisionCard(**item) for item in spec.get("decisions", [])]
    approval = approve_prd(
        PRD(str(spec.get("title", "")).strip(),
            str(spec.get("objective", "")).strip()),
        decisions)
    if not approval.approved:
        raise PermissionError("product decisions remain unresolved")
    return decisions


def _merge_nodes(spec: Dict[str, Any],
                 previous_nodes: Dict[str, Dict[str, Any]]) -> List[DAGNode]:
    """Overlay the business fields onto the published engineering nodes."""
    merged: List[DAGNode] = []
    for item in spec["nodes"]:
        previous = previous_nodes[item["id"]]
        node_dict = copy.deepcopy(previous)
        for key in _BUSINESS_NODE_KEYS:
            if key in item:
                node_dict[key] = item[key]
        contract = node_dict.setdefault("contract", {})
        for key, value in item.get("contract", {}).items():
            contract[key] = value
        # ``worker_profile.allowlist`` is a *derived* projection of
        # ``contract.files`` (node_spec: ``files or ["."]``).  A files
        # amendment must refresh it in place or the write-scope audit
        # unions the stale projection with the new files and dispatch
        # would still bind the old allowlist.  Every other profile field
        # stays byte-identical; a ``files`` value that cannot derive a
        # projection (a non-list truthy value) is left untouched so the
        # engineering re-verification below fails closed.
        old_files = (previous.get("contract") or {}).get("files")
        new_files = contract.get("files")
        derived = _derived_allowlist(new_files)
        if (derived is not None and new_files != old_files
                and isinstance(contract.get("worker_profile"), dict)):
            contract["worker_profile"] = dict(
                contract["worker_profile"], allowlist=derived)
        merged.append(DAGNode.from_dict(node_dict))
    return merged


def _derived_allowlist(files: Any) -> Optional[List[str]]:
    """Mirror the publish-time projection ``contract.files or ["."]``.

    Returns ``None`` when ``files`` is a non-list truthy value: nothing
    legitimate derives from it and the caller must leave the stored
    projection untouched so re-verification rejects the amendment.
    """
    if isinstance(files, list):
        return list(files) or ["."]
    if not files:
        return ["."]
    return None


def _profile_projection_diff(previous: Any,
                             merged: Any) -> Optional[str]:
    """The only engineering difference a files amendment may create.

    ``previous`` is the stored ``worker_profile``; ``merged`` is the
    merged profile plus the node's new ``contract.files`` under the
    ``"files"`` key.  Returns a field path when the profile differs by
    anything other than ``allowlist`` becoming exactly the projection
    derived from those files, ``None`` when the difference is legal.
    """
    if merged == previous:
        return None
    if not isinstance(merged, dict) or not isinstance(previous, dict):
        return "worker_profile"
    expected = _derived_allowlist(merged.get("files"))
    if expected is None:
        return "worker_profile.allowlist"
    if "allowlist" not in merged:
        return "worker_profile.allowlist"
    if merged.get("allowlist") != expected:
        return "worker_profile"
    for key in set(previous) | set(merged):
        if key in ("allowlist", "files"):
            continue
        if previous.get(key) != merged.get(key):
            return "worker_profile." + str(key)
    return None


def _business_diff(old_nodes: Dict[str, Dict[str, Any]],
                   new_nodes: List[DAGNode]) -> Dict[str, Any]:
    diff: Dict[str, Any] = {}
    for node in new_nodes:
        old = old_nodes[node.id]
        changes: Dict[str, Any] = {}
        for key in _BUSINESS_NODE_KEYS:
            if old.get(key) != getattr(node, key):
                changes[key] = {"previous": old.get(key),
                                "next": getattr(node, key)}
        old_contract = old.get("contract") or {}
        for key in _BUSINESS_CONTRACT_KEYS:
            if old_contract.get(key) != node.contract.get(key):
                changes["contract." + key] = {
                    "previous": old_contract.get(key),
                    "next": node.contract.get(key)}
        if changes:
            diff[node.id] = changes
    return diff


def _amendment_revision(plan_root: Path) -> int:
    base = amendments_dir_from_root(plan_root)
    existing = 0
    if base.is_dir():
        for item in base.iterdir():
            if item.is_dir():
                try:
                    existing += 1
                except OSError:
                    pass
    data = _read_artifact(plan_root / "plan.json")
    recorded = data.get("amendment_revision") or 0
    try:
        recorded = int(recorded)
    except (TypeError, ValueError):
        recorded = 0
    return max(existing, recorded) + 1


def amendments_dir_from_root(plan_root: Path) -> Path:
    return plan_root / AMENDMENTS_DIRNAME


def _topology_rulings(paths: ProjectPaths):
    from .cli import _observed_topology_rulings
    return _observed_topology_rulings(paths)


def _candidate_card(paths: ProjectPaths, plan: Plan, nodes: List[DAGNode],
                    previous_card: AuthorizationCard, spec: Dict[str, Any],
                    workflow: Dict[str, Any]) -> AuthorizationCard:
    local_commit = spec.get("local_commit_allowed")
    if local_commit is not None and type(local_commit) is not bool:
        raise ValueError("local_commit_allowed must be true or false")
    # A spec that omits the choice keeps the approved one; explicit false
    # withdraws the exception and must be re-signed.
    flag = previous_card.local_commit_allowed if "local_commit_allowed" not in spec \
        else (True if local_commit is True else None)
    remote = str(spec.get("remote_git_actions") or previous_card.remote_git_actions)
    if remote != previous_card.remote_git_actions:
        raise ValueError(
            "remote_git_actions cannot change through a contract amendment; "
            "the Git remote group is a separate authorization")
    engine_attestation = None
    attestation_path = _plan_root(paths, plan.plan_id) / "engine-attestation.json"
    if attestation_path.is_file() and not attestation_path.is_symlink():
        try:
            engine_attestation = json.loads(
                attestation_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("engine-attestation.json is unreadable")
    return refresh_authorization_card(
        plan, nodes, previous_card,
        workflow=workflow,
        topology_rulings=_topology_rulings(paths),
        local_commit_allowed=flag,
        engine_attestation=engine_attestation,
        heartbeat_interval_options=spec.get("_heartbeat_options"),
        rotate_hard_cap_options=spec.get("_rotate_options"),
    )


def propose(
    paths: ProjectPaths,
    plan_id: str,
    spec: Any,
    reason: str,
) -> Dict[str, Any]:
    """Stage an immutable amendment candidate; nothing live is modified."""
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("amendment requires a non-empty reason")
    plan_root = _plan_root(paths, plan_id)
    data = _plan_data(plan_root)
    if data.get("plan_id") != plan_id:
        raise ValueError("plan id mismatch for " + str(plan_id))
    record = _load_record(plan_root)
    card = _load_card(plan_root)
    if record.digest != card.digest:
        raise ValueError("live card and authorization record disagree")
    if isinstance(spec, dict) and spec.get("previous_authorization_digest") \
            and spec["previous_authorization_digest"] != record.digest:
        raise ValueError(
            "previous_authorization_digest does not match the live record")
    previous_nodes = _node_map(plan_root)
    if isinstance(spec, dict) and "nodes" not in spec:
        raise ValueError("amendment spec must carry the full node list")
    decisions = _validate_amendment_spec(spec, previous_nodes)
    require_no_pending_transfer(paths, plan_id)
    plan = _plan_object(plan_root, data)
    nodes = _merge_nodes(spec, previous_nodes)
    # Business fields only changed; every engineering field must come back
    # identical after derivation, proving identities are preserved.
    for node in nodes:
        previous = DAGNode.from_dict(previous_nodes[node.id])
        for key, value in previous.contract.items():
            if key in _BUSINESS_CONTRACT_KEYS:
                continue
            if key == "worker_profile":
                merged_profile = node.contract.get("worker_profile")
                if merged_profile == value:
                    continue
                if isinstance(merged_profile, dict):
                    merged_profile = dict(
                        merged_profile, files=node.contract.get("files"))
                drift = _profile_projection_diff(value, merged_profile)
                if drift is not None:
                    raise ValueError(
                        "contract amendment cannot change engineering field "
                        "contract.{} on node {}".format(drift, node.id))
            elif node.contract.get(key) != value:
                raise ValueError(
                    "contract amendment cannot change engineering field "
                    "contract.{} on node {}".format(key, node.id))
    workflow = None
    state = paths.vibe / "state.json"
    if state.is_file():
        from .authorize_entry import load_live_workflow
        workflow = load_live_workflow(paths, plan_id)
    candidate_card = _candidate_card(
        paths, plan, nodes, card, spec, workflow)
    graph = dependency_graph(nodes)
    revision = _amendment_revision(plan_root)
    diff = _business_diff(previous_nodes, nodes)
    candidate = {
        "schema_version": 1,
        "kind": "plan_amendment_candidate",
        "status": "pending_authorization",
        "plan_id": plan_id,
        "plan_version": plan.version,
        "run_id": _current_run_id(plan_root),
        "revision": revision,
        "reason": reason.strip(),
        "proposed_at": _now(),
        "previous_authorization_digest": record.digest,
        "previous_card_digest": card.digest,
        "previous_source_digests": {
            "plan_json": _fingerprint(plan_root / "plan.json"),
            "nodes_json": _fingerprint(plan_root / "nodes.json"),
            "prd_md": _fingerprint(plan_root / "prd.md"),
        },
        "node_ids": sorted(node.id for node in nodes),
        "diff": diff,
        "dag_audit": {"status": "reviewed", "node_count": len(nodes),
                      "plan_revision": str(plan.version),
                      "node_ids": sorted(node.id for node in nodes),
                      "dependency_graph_digest": dependency_graph_digest(graph)},
        "candidate_card": candidate_card.to_dict(),
    }
    # Stage every artifact first: the candidate digest must cover the
    # bytes the transfer will actually install (spec, card, nodes, audit,
    # diff), not just candidate.json's embedded copies.  Tampering any of
    # them later fails the load-time re-check (P1.4/B03).
    staging = Path(tempfile.mkdtemp(
        prefix=".amend-candidate-",
        dir=str(amendments_dir(paths, plan_id).parent)))
    try:
        _atomic_json(staging / "spec.json", spec)
        _atomic_json(staging / "authorization-card.json",
                     candidate_card.to_dict())
        _atomic_json(staging / "nodes.json",
                     [node.to_dict() for node in nodes])
        _atomic_json(staging / "dag-audit.json", candidate["dag_audit"])
        _atomic_json(staging / "diff.json", diff)
        candidate["artifact_digests"] = {
            name: _fingerprint(staging / name)
            for name in ("spec.json", "authorization-card.json",
                         "nodes.json", "dag-audit.json", "diff.json")
        }
        digest = _canonical_digest(candidate)
        candidate["amendment_digest"] = digest
        _atomic_json(staging / "candidate.json", candidate)
        target = candidate_dir(paths, plan_id, digest)
        if target.exists():
            raise FileExistsError(
                "amendment candidate already exists: " + digest)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(staging), str(target))
    except BaseException:
        shutil.rmtree(str(staging), ignore_errors=True)
        raise
    return candidate


def _fingerprint(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        return ""
    return _artifact_hash(path)


CANDIDATE_ARTIFACTS = ("spec.json", "authorization-card.json", "nodes.json",
                       "dag-audit.json", "diff.json")


def _load_candidate(paths: ProjectPaths, plan_id: str,
                    amendment_digest: str) -> Dict[str, Any]:
    if not isinstance(amendment_digest, str) or len(amendment_digest) != 64:
        raise ValueError("amendment digest must be a 64-char hex string")
    directory = candidate_dir(paths, plan_id, amendment_digest)
    candidate = _read_artifact(directory / "candidate.json")
    if not isinstance(candidate, dict):
        raise ValueError("amendment candidate is not an object")
    if candidate.get("amendment_digest") != amendment_digest:
        raise PermissionError("amendment candidate digest mismatch")
    body = {key: value for key, value in candidate.items()
            if key != "amendment_digest"}
    if _canonical_digest(body) != amendment_digest:
        raise PermissionError("amendment candidate was modified after proposal")
    # The digest bound the installed artifact bytes at proposal time;
    # re-verify every file the transfer (or show) will actually read.
    recorded = candidate.get("artifact_digests")
    if not isinstance(recorded, dict) or sorted(recorded) != sorted(
            CANDIDATE_ARTIFACTS):
        raise PermissionError("amendment candidate artifact ledger is invalid")
    for name, expected in recorded.items():
        if _fingerprint(directory / name) != expected:
            raise PermissionError(
                "amendment candidate artifact was modified: " + name)
    return candidate


def show(paths: ProjectPaths, plan_id: str,
         amendment_digest: str) -> Dict[str, Any]:
    """Render the candidate card + graph and record the bound receipt."""
    candidate = _load_candidate(paths, plan_id, amendment_digest)
    directory = candidate_dir(paths, plan_id, amendment_digest)
    nodes = [DAGNode.from_dict(dict(item))
             for item in _read_artifact(directory / "nodes.json")]
    graph = dependency_graph(nodes)
    receipt = {
        "schema": 1,
        "plan_id": plan_id,
        "plan_version": candidate["plan_version"],
        "amendment_digest": amendment_digest,
        "card_digest": candidate["candidate_card"]["digest"],
        "dependency_graph_digest": dependency_graph_digest(graph),
        "shown_at": _now(),
    }
    _atomic_json(
        _plan_root(paths, plan_id) / DISPLAY_RECEIPT_NAME, receipt)
    return {
        "status": "shown",
        "plan_id": plan_id,
        "amendment_digest": amendment_digest,
        "card": candidate["candidate_card"],
        "diff": candidate["diff"],
        "dag_audit": candidate["dag_audit"],
        "dependency_graph": graph,
        "display_receipt": receipt,
        "confirmation_hint": (
            "产品经理确认新卡后执行：vibe plan-amend confirm --plan {} "
            "--amendment {} --confirm 'AMEND {}'".format(
                plan_id, amendment_digest, amendment_digest)),
    }


def confirmation_token(amendment_digest: str) -> str:
    """The only token that may confirm this candidate."""
    return TOKEN_PREFIX + str(amendment_digest)


def _require_candidate_receipt(plan_root: Path, plan_id: str,
                               candidate: Dict[str, Any]) -> None:
    receipt_path = plan_root / DISPLAY_RECEIPT_NAME
    receipt = None
    if receipt_path.is_file() and not receipt_path.is_symlink():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            receipt = None
    if not isinstance(receipt, dict):
        raise PermissionError(
            "amendment candidate has not been shown: run plan-amend show first")
    if receipt.get("amendment_digest") != candidate["amendment_digest"]:
        raise PermissionError(
            "the display receipt belongs to a different amendment candidate")
    if receipt.get("card_digest") != candidate["candidate_card"]["digest"]:
        raise PermissionError("the displayed card no longer matches")
    if str(receipt.get("plan_version")) != str(candidate["plan_version"]):
        raise PermissionError("the display receipt predates this plan version")


def _live_executors(paths: ProjectPaths, run_id: str) -> List[str]:
    """Every reason the run cannot be proven quiescent; empty means still."""
    problems: List[str] = []
    try:
        snapshot = load_snapshot(paths, run_id)
    except Exception as error:
        return ["run snapshot unreadable: " + str(error)]
    if snapshot.handles:
        problems.append("active task handles: " + ",".join(sorted(snapshot.handles)))
    for node_id, node in snapshot.nodes.items():
        if node.get("status") in _ACTIVE_NODE_STATUSES:
            problems.append("node {} is {}".format(node_id, node.get("status")))
        if node.get("active_task") or node.get("active_role"):
            problems.append("node {} carries an active task".format(node_id))
        if node.get("retryable_action"):
            problems.append("node {} has an unapplied retryable action".format(node_id))
    try:
        pending = ProviderActionStore(paths).pending(run_id)
    except Exception as error:
        problems.append("provider pending scan unreadable: " + str(error))
    else:
        if pending:
            problems.append(
                "pending provider side effects: "
                + ",".join(str(item.get("action_id")) for item in pending))
    try:
        leases_dir = paths.vibe / "leases"
        if leases_dir.is_dir():
            for lease_file in sorted(leases_dir.glob("*.json")):
                try:
                    lease = json.loads(
                        lease_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    problems.append(
                        "writer lease unreadable: " + lease_file.name)
                    continue
                if not isinstance(lease, dict):
                    problems.append(
                        "writer lease malformed: " + lease_file.name)
                    continue
                lease_run = lease.get("run_id")
                status = lease.get("status")
                if lease_run is not None and lease_run != run_id:
                    continue
                # A released lease is unlinked, so anything on disk is an
                # unproven writer: ``active`` is live, ``quarantined`` is
                # "termination unproven, still owned" (state.py lease
                # lifecycle), and any other/missing status is unknown --
                # none of them proves the run quiescent.
                if status in {"active", "quarantined"}:
                    problems.append(
                        "writer lease {} is {} for node {}".format(
                            lease_file.name, status,
                            lease.get("node_id") or "?"))
                elif not status:
                    problems.append(
                        "writer lease {} has no status".format(
                            lease_file.name))
                else:
                    problems.append(
                        "writer lease {} has unknown status {}".format(
                            lease_file.name, status))
    except OSError as error:
        problems.append("writer leases unreadable: " + str(error))
    try:
        from .task_registry import _all_bindings
        for binding in _all_bindings(paths):
            if getattr(binding, "run_id", None) != run_id:
                continue
            status = str(getattr(binding, "status", "") or "")
            if status and status not in _TERMINAL_BINDING_STATUSES:
                problems.append(
                    "task binding {}:{} is {}".format(
                        getattr(binding, "node_id", "?"),
                        getattr(binding, "role", "?"), status))
    except Exception as error:
        problems.append("task registry unreadable: " + str(error))
    return problems


def _cas_lineage(paths: ProjectPaths, plan_id: str, plan_root: Path,
                 candidate: Dict[str, Any]) -> Dict[str, Any]:
    """Re-verify every value the candidate bound, right before the switch."""
    record = _load_record(plan_root)
    card = _load_card(plan_root)
    if record.digest != candidate["previous_authorization_digest"]:
        raise PermissionError(
            "the live authorization changed since the candidate was proposed")
    if card.digest != candidate["previous_card_digest"] or record.digest != card.digest:
        raise PermissionError("live card and record disagree")
    run_id = _current_run_id(plan_root)
    if run_id != candidate.get("run_id"):
        raise PermissionError(
            "the bound run changed since the candidate was proposed")
    if run_id is None:
        raise PermissionError("the plan has no live run to amend")
    snapshot = load_snapshot(paths, run_id)
    if snapshot.authorization_digest != record.digest:
        raise PermissionError(
            "the run's authorization digest no longer matches the record")
    return {"record": record, "card": card, "run_id": run_id,
            "snapshot": snapshot}


def transfer_pending(paths: ProjectPaths, plan_id: str) -> bool:
    return barrier_path(paths, plan_id).is_file()


def require_no_pending_transfer(paths: ProjectPaths, plan_id: str) -> None:
    """Fail closed while a transfer marker is present.

    Recovery is attempted first: a marker whose committed event already
    exists finishes the projection, and one with no committed event restores
    the old state.  A marker that cannot be resolved -- conflicting digest,
    unreadable artifacts, unknown run -- raises instead of letting any entry
    point consume or dispatch on a half-amended plan.
    """
    marker = barrier_path(paths, plan_id)
    if marker.is_file():
        recover(paths, plan_id)


def _marker_state(paths: ProjectPaths, plan_id: str) -> Optional[Dict[str, Any]]:
    marker = barrier_path(paths, plan_id)
    if not marker.is_file() or marker.is_symlink():
        return None
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise PermissionError("amendment transfer marker is unreadable")
    if not isinstance(data, dict) or "amendment_digest" not in data:
        raise PermissionError("amendment transfer marker is malformed")
    return data


def _event_committed(paths: ProjectPaths, run_id: str,
                     amendment_digest: str) -> bool:
    try:
        for record in load_events(paths, run_id):
            if (record["event"] == "authorization_reauthorized"
                    and record["data"].get("change_reason") == CHANGE_REASON
                    and record["data"].get("amendment_digest") == amendment_digest):
                return True
    except Exception as error:
        raise PermissionError(
            "run event log unreadable during amendment recovery: " + str(error))
    return False


def _apply_staged(plan_root: Path, staged_dir: Path) -> None:
    """Install the rendered new artifacts (each write is os.replace)."""
    for name in ("authorization-card.json", "authorization.json", "plan.json",
                 "nodes.json", "prd.md", "dag-audit.json"):
        staged = staged_dir / "new" / name
        if staged.is_file():
            os.replace(str(staged), str(plan_root / name))
    for sub in ("specs", "issues"):
        staged_sub = staged_dir / "new" / sub
        live_sub = plan_root / sub
        if staged_sub.is_dir():
            if live_sub.is_dir():
                shutil.rmtree(str(live_sub))
            shutil.copytree(str(staged_sub), str(live_sub))


def _restore_previous(plan_root: Path, staged_dir: Path) -> None:
    """Put the pre-swap artifacts back byte for byte."""
    for name in ("authorization-card.json", "authorization.json", "plan.json",
                 "nodes.json", "plan-confirmation.json", "prd.md",
                 "dag-audit.json"):
        backup = staged_dir / "old" / name
        if backup.is_file():
            shutil.copy2(str(backup), str(plan_root / name))
    for sub in ("specs", "issues"):
        backup_sub = staged_dir / "old" / sub
        live_sub = plan_root / sub
        if backup_sub.is_dir():
            if live_sub.is_dir():
                shutil.rmtree(str(live_sub))
            shutil.copytree(str(backup_sub), str(live_sub))


def _write_staging(paths: ProjectPaths, plan_root: Path,
                   candidate: Dict[str, Any], run_id: str) -> Path:
    """Copy old artifacts aside and render the amended set under one dir."""
    plan_id = candidate["plan_id"]
    staging = Path(tempfile.mkdtemp(
        prefix=".amend-staging-", dir=str(plan_root)))
    old_dir = staging / "old"
    new_dir = staging / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    for name in ("authorization-card.json", "authorization.json", "plan.json",
                 "nodes.json", "plan-confirmation.json", "prd.md",
                 "dag-audit.json"):
        source = plan_root / name
        if source.is_file():
            shutil.copy2(str(source), str(old_dir / name))
    for sub in ("specs", "issues"):
        if (plan_root / sub).is_dir():
            shutil.copytree(str(plan_root / sub), str(old_dir / sub))
    # The pre-swap artifacts are history the candidate keeps verbatim: only
    # this staging step still sees them, so archive them now.
    history = candidate_dir(
        paths, plan_id, candidate["amendment_digest"])
    for name, target in (
        ("authorization-card.json", "previous-authorization-card.json"),
        ("authorization.json", "previous-authorization.json"),
    ):
        source = old_dir / name
        if source.is_file() and not (history / target).exists():
            shutil.copy2(str(source), str(history / target))
    cand_dir = candidate_dir(paths, plan_id, candidate["amendment_digest"])
    shutil.copy2(str(cand_dir / "authorization-card.json"),
                 str(new_dir / "authorization-card.json"))
    shutil.copy2(str(cand_dir / "nodes.json"), str(new_dir / "nodes.json"))
    shutil.copy2(str(cand_dir / "dag-audit.json"), str(new_dir / "dag-audit.json"))
    spec = _read_artifact(cand_dir / "spec.json")
    decisions = [DecisionCard(**item) for item in spec.get("decisions", [])]
    plan_data = _plan_data(plan_root)
    amended_plan = dict(plan_data)
    amended_plan["amendment_revision"] = candidate["revision"]
    amended_plan["status"] = "authorized"
    _atomic_json(new_dir / "plan.json", amended_plan)
    # plan-confirmation.json binds run_id + event_sequence, so it cannot be
    # staged before the transfer event exists; _finish_projection writes it.
    (new_dir / "prd.md").write_text(
        render_prd_markdown(spec, decisions,
                            plan_data.get("evidence_priority")), encoding="utf-8")
    (new_dir / "specs").mkdir()
    (new_dir / "issues").mkdir()
    amended_nodes = [DAGNode.from_dict(dict(item))
                     for item in _read_artifact(cand_dir / "nodes.json")]
    for node in amended_nodes:
        contract = node.contract
        (new_dir / "specs" / (node.id + ".md")).write_text(
            "# Spec: {}\n\nnode_id: {}\n状态：published\n审核：reviewed\n\n输入：{}\n\n输出：{}\n\n错误行为：{}\n\n验收示例：{}\n".format(
                node.title, node.id, contract.get("input"),
                contract.get("output"), contract.get("error_behavior"),
                contract.get("acceptance_example")), encoding="utf-8")
        (new_dir / "issues" / (node.id + ".md")).write_text(
            "# Issue: {}\n\nissue_id: {}\n状态：published\n审核：reviewed\n\n并行组：{}\n".format(
                node.title, node.id, node.parallel_group or "none"),
            encoding="utf-8")
    return staging


def _finish_projection(paths: ProjectPaths, plan_id: str, plan_root: Path,
                       candidate: Dict[str, Any], run_id: str,
                       runner: Any = None) -> Dict[str, Any]:
    """Post-event projection, idempotent.

    The artifacts may already be installed (the transfer committed them
    before crashing) or not (recovery after an early exit).  Re-applying
    the staged render is a no-op when they are, so both paths converge.
    """
    staging = _write_staging(paths, plan_root, candidate, run_id)
    try:
        _apply_staged(plan_root, staging)
    finally:
        shutil.rmtree(str(staging), ignore_errors=True)
    candidate_card = _load_card(
        candidate_dir(paths, plan_id, candidate["amendment_digest"]))
    record = authorize(candidate_card, "AUTHORIZE")
    snapshot = load_snapshot(paths, run_id)
    plan_data = _plan_data(plan_root)
    _atomic_json(
        plan_root / "plan-confirmation.json",
        {"status": "confirmed", "plan_id": plan_id,
         "plan_revision": str(plan_data.get("version")),
         "authorization_digest": record.digest,
         "authorization_required": True,
         "run_id": run_id,
         "event_sequence": snapshot.event_sequence,
         "publication": "plan_amendment",
         "amendment_digest": candidate["amendment_digest"]})
    _atomic_json(plan_root / "authorization.json", record.to_dict())
    # Evidence must name the token the user actually supplied; the
    # candidate-bound AMEND token is derived from the verified digest, so
    # it is the same string confirm required -- never a fabricated
    # AUTHORIZE.
    materialize_workflow_evidence(
        paths, plan_id, confirmation_token(candidate["amendment_digest"]))
    return {"record": record, "candidate_card": candidate_card}


def confirm(
    paths: ProjectPaths,
    plan_id: str,
    amendment_digest: str,
    token: str,
    runner: Any = None,
    _fault_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Transfer the same plan/run onto the confirmed amendment.

    ``_fault_at`` exists for the crash-boundary tests: ``before_event`` and
    ``after_event`` raise around the durable event so recovery can be
    exercised deterministically; production callers never pass it.
    """
    if token != confirmation_token(amendment_digest):
        raise PermissionError(
            "authorization requires the exact candidate-bound confirmation")
    candidate = _load_candidate(paths, plan_id, amendment_digest)
    plan_root = _plan_root(paths, plan_id)
    _require_candidate_receipt(plan_root, plan_id, candidate)
    with interprocess_lock(plan_root / ".amendment.lock"):
        marker = _marker_state(paths, plan_id)
        if marker is not None:
            if marker["amendment_digest"] != amendment_digest:
                raise PermissionError(
                    "a different amendment transfer is in progress")
            # An interrupted transfer of this same candidate: a committed
            # event finishes the projection; an uncommitted one is rolled
            # back and the confirm below retries the whole transaction.
            if _event_committed(paths, marker.get("run_id"), amendment_digest):
                return _recover_locked(
                    paths, plan_id, plan_root, runner=runner)
            _rollback_uncommitted(plan_root)
            marker_path = barrier_path(paths, plan_id)
            if marker_path.is_file():
                marker_path.unlink()
        lineage = _cas_lineage(paths, plan_id, plan_root, candidate)
        run_id = lineage["run_id"]
        problems = _live_executors(paths, run_id)
        if problems:
            raise PermissionError(
                "amendment requires a quiescent run: " + "; ".join(problems))
        staging = _write_staging(paths, plan_root, candidate, run_id)
        try:
            _atomic_json(barrier_path(paths, plan_id), {
                "schema": 1,
                "plan_id": plan_id,
                "amendment_digest": amendment_digest,
                "run_id": run_id,
                "started_at": _now(),
            })
            # The marker now blocks every execution entry, but a dispatch
            # that started between the earlier check and this write could
            # still be in flight; re-prove quiescence inside the protected
            # window and refuse instead of consuming or stopping it.
            problems = _live_executors(paths, run_id)
            if problems:
                raise PermissionError(
                    "an executor became active inside the transfer window: "
                    + "; ".join(problems))
            if _fault_at == "before_event":
                raise RuntimeError("fault injected before transfer event")
            _commit_transfer_event(
                paths, plan_id, plan_root, candidate, run_id,
                lineage["record"], lineage["snapshot"], staging,
                runner=runner)
            if _fault_at == "after_event":
                raise RuntimeError("fault injected after transfer event")
            finished = _finish_projection(
                paths, plan_id, plan_root, candidate, run_id, runner=runner)
            barrier_path(paths, plan_id).unlink()
        except BaseException:
            # The committed event decides the state: once it is durable the
            # marker stays and ``recover`` finishes the new projection on
            # the next entry; before it the complete old state goes back.
            if _event_committed(paths, run_id, amendment_digest):
                raise
            _restore_previous(plan_root, staging)
            if barrier_path(paths, plan_id).is_file():
                barrier_path(paths, plan_id).unlink()
            raise
        finally:
            shutil.rmtree(str(staging), ignore_errors=True)
    return {
        "status": "amended", "plan_id": plan_id, "run_id": run_id,
        "amendment_digest": amendment_digest,
        "authorization_digest": finished["record"].digest,
        "previous_authorization_digest":
            candidate["previous_authorization_digest"],
    }


def _commit_transfer_event(paths: ProjectPaths, plan_id: str, plan_root: Path,
                           candidate: Dict[str, Any], run_id: str,
                           record: AuthorizationRecord, snapshot: Any,
                           staging: Path,
                           runner: Any = None) -> None:
    """Publish the durable transfer inside ``Monitor.reauthorize``.

    The monitor validates previous lineage, records the
    ``authorization_reauthorized`` event with ``change_reason``
    ``plan_amendment`` and applies the new projection to the snapshot --
    task identities, cursors and node continuity included.  For the
    amendment reason the monitor refuses active executors and never
    schedules: the amended run only dispatches after the caller's own
    next monitor/resume.
    """
    from .cli import _authorization_card, _public_runner
    from .monitor import Monitor
    from .runners.fake import FakeRunner

    cand_dir = candidate_dir(paths, plan_id, candidate["amendment_digest"])
    new_card = _authorization_card(_read_artifact(cand_dir / "authorization-card.json"))
    new_nodes = [DAGNode.from_dict(dict(item))
                 for item in _read_artifact(cand_dir / "nodes.json")]
    plan_data = _plan_data(plan_root)
    plan = _plan_object(plan_root, plan_data)
    # The monitor must see the amended nodes: its projection binds them.
    plan.nodes = new_nodes
    new_record = authorize(new_card, "AUTHORIZE")
    # Install the new artifacts first so the monitor's digest reads see the
    # amended sources while the event is being committed; the caller's
    # staging still holds the pre-swap bytes for rollback.
    _apply_staged(plan_root, staging)
    monitor = Monitor(paths, plan, new_nodes)
    if runner is None:
        runner = FakeRunner()
    monitor.reauthorize(run_id, new_record, runner, CHANGE_REASON,
                        amendment_digest=candidate["amendment_digest"])


def _rollback_uncommitted(plan_root: Path) -> None:
    """Restore the pre-swap artifacts left behind by a killed transfer.

    The swap inside ``_commit_transfer_event`` runs before the durable
    event, so a process death in that window leaves the new artifacts
    installed with no committed event -- and the only copy of the old
    bytes is the staging directory the dead process never removed.
    """
    staging_dirs = sorted(
        (item for item in plan_root.glob(".amend-staging-*")
         if item.is_dir()),
        key=lambda item: item.stat().st_mtime)
    if staging_dirs:
        _restore_previous(plan_root, staging_dirs[-1])
    for item in staging_dirs:
        shutil.rmtree(str(item), ignore_errors=True)


def recover(paths: ProjectPaths, plan_id: str, runner: Any = None) -> Dict[str, Any]:
    """Resolve a transfer marker: finish the committed side, undo the rest.

    The marker can only be judged under the same ``.amendment.lock`` the
    owning ``confirm`` holds for the whole marker->event->projection
    transaction: a marker visible while that lock is held belongs to a
    live transfer and this call simply waits; once the lock is free any
    remaining marker is proven crash residue.  A lock that cannot be
    acquired in time means the transfer is still in progress -- the
    non-owner path fails closed, it never rolls anything back.
    """
    plan_root = _plan_root(paths, plan_id)
    try:
        with interprocess_lock(plan_root / ".amendment.lock"):
            return _recover_locked(paths, plan_id, plan_root, runner=runner)
    except TimeoutError as error:
        raise PermissionError(
            "amendment transfer is still in progress; this entry cannot "
            "resolve it") from error


def _recover_locked(paths: ProjectPaths, plan_id: str, plan_root: Path,
                    runner: Any = None) -> Dict[str, Any]:
    """``recover`` with ``.amendment.lock`` already held (or provably free)."""
    marker = _marker_state(paths, plan_id)
    if marker is None:
        return {"status": "clean", "plan_id": plan_id}
    digest = marker["amendment_digest"]
    run_id = marker.get("run_id")
    try:
        candidate = _load_candidate(paths, plan_id, digest)
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise PermissionError(
            "amendment marker names an unreadable candidate: " + str(error))
    if marker.get("run_id") != candidate.get("run_id"):
        raise PermissionError(
            "amendment marker and candidate disagree about the run")
    if _event_committed(paths, run_id, digest):
        finished = _finish_projection(
            paths, plan_id, plan_root, candidate, run_id, runner=runner)
        marker_path = barrier_path(paths, plan_id)
        if marker_path.is_file():
            marker_path.unlink()
        return {
            "status": "completed", "plan_id": plan_id, "run_id": run_id,
            "amendment_digest": digest,
            "authorization_digest": finished["record"].digest,
        }
    _rollback_uncommitted(plan_root)
    marker_path = barrier_path(paths, plan_id)
    if marker_path.is_file():
        marker_path.unlink()
    return {"status": "rolled_back", "plan_id": plan_id,
            "run_id": run_id, "amendment_digest": digest}
