"""Move a run's unfinished nodes to the platform it is handed off to.

``vibe monitor --platform-handoff`` used to keep the authorization alive and
the accepted nodes accepted, and nothing else: every node contract and the
authorization card kept naming the platform the run was leaving, so the first
dispatch on the new platform was refused as a provider-action identity drift.

This module is the pure half of the fix.  It decides *which* platform the run
moves to and *which* nodes move; the CLI owns observation (is the target
platform really attested and dispatchable) and persistence.  Nothing here
reads or writes a file, and nothing here is reachable from an ordinary
reauthorization.
"""
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from pathlib import Path

from .authorization import node_adapter_id, rebind_dispatch_contract
from .capability_contract import CapabilityContract, build_contract
from .models import DAGNode

# What ``vibe init`` writes into a contract nobody has ever stated a platform for.
UNSTATED_PROVIDER = "unknown"


class HandoffRebindError(Exception):
    """The new platform cannot be proven; the caller keeps ``blocked_unknown``."""


class HandoffNothingToMove(HandoffRebindError):
    """The run has no unfinished node: a handoff would only rewrite history."""


def require_unfinished_nodes(unfinished: Sequence[str], card_agent: str) -> None:
    """Refuse a handoff of a run whose every node is already accepted.

    Nothing is left to dispatch, so moving the card would only relabel the
    platform that actually did the work (socialmedia run-63e7cfc2: 8/8
    accepted on Codex, then flipped to workbuddy by a later handoff).
    """
    if not list(unfinished):
        raise HandoffNothingToMove(
            "platform handoff: 本运行已没有未完成节点，无需交接；授权卡平台保持 {}".format(card_agent)
        )


def resolve_handoff_target(
    *,
    explicit: Optional[str],
    card_agent: str,
    run_agent: str,
    run_drift: bool,
    capability_changed: bool,
    contract_provider: Optional[str],
    dispatchable_others: Sequence[str],
) -> Optional[str]:
    """The platform a handoff moves to, or ``None`` when nothing moves.

    Evidence order, strongest first; a platform is never guessed:

    1. ``--adapter``: the operator named it.
    2. The run itself already names a platform the files do not: an earlier
       handoff was interrupted after the run moved, and nodes.json / the card
       are only catching up (``run_drift``).  The run's authorization is the
       committed truth, so it wins over anything observed now.
    3. The capability contract changed since the run last saw it, and either
       the contract's own provider is an attested dispatchable platform other
       than the card's, or exactly one such platform exists.  Several
       candidates is ambiguity, which is an error, never a pick.

    4. The contract never named a platform at all (``unknown``, which is all
       ``init`` ever writes and nothing but a handoff refreshes): then the
       capability digest cannot change by itself, so absence of change is no
       evidence, and the one other dispatchable platform is the target.

    Otherwise nothing moves.  A contract that already names a platform keeps
    rule 4 off, so re-running a finished handoff cannot flip the run back to
    the platform it left.
    """
    if explicit:
        return explicit
    if run_agent and (run_agent != card_agent or run_drift):
        return run_agent
    if not capability_changed and contract_provider != UNSTATED_PROVIDER:
        return None
    others = sorted(set(dispatchable_others) - {card_agent})
    if contract_provider and contract_provider in others:
        return contract_provider
    if len(others) > 1:
        raise HandoffRebindError(
            "platform handoff: 多个已登记平台都可派发（{}），无法判断交接到哪个；"
            "请加 --adapter <新平台> 指定".format(", ".join(others))
        )
    return others[0] if others else None


def contract_for_platform(
    contract: CapabilityContract, project_root: Path, target: str
) -> Optional[CapabilityContract]:
    """The session contract after a handoff to ``target``, or ``None`` if it already names it.

    Only ``provider`` changes.  The contract-level ``expires_at`` is renewed
    (now + TTL, so ``contract_digest`` is recomputed): the refreshed contract is
    a new observation made by the handoff, and handing a run a contract that is
    about to lapse would only schedule the next failure.  Each fact keeps its own
    ``checked_at``/``expires_at`` as it was: the handoff observed no capability,
    so it must not make an old observation look fresh.  ``host_id`` has no source in this code base
    other than the contract itself (``attest`` states facts, never a host), so
    it is kept rather than invented; every stated fact is kept as it was.
    """
    if contract.provider == target:
        return None
    return build_contract(
        Path(project_root),
        provider=target,
        host_id=contract.host_id,
        facts=dict(contract.capabilities),
    )


def stranded_nodes(
    nodes: Iterable[DAGNode], unfinished: Iterable[str], contract_provider: Optional[str]
) -> List[str]:
    """Unfinished nodes bound to a platform other than the one the contract names.

    Only meaningful when no handoff target could be named: a contract that
    states a platform while unfinished nodes sit on another means the user
    believes the run moved and it did not.  A contract that states nothing
    (``unknown``) has no platform to disagree with.
    """
    if not contract_provider or contract_provider == UNSTATED_PROVIDER:
        return []
    pending = set(unfinished)
    return sorted(
        node.id for node in nodes
        if node.id in pending and node_adapter_id(node) != contract_provider
    )


def unfinished_node_ids(
    nodes: Iterable[DAGNode], snapshot_nodes: Mapping[str, Mapping[str, Any]]
) -> List[str]:
    """Nodes whose dispatch binding may move: everything not yet accepted.

    Whether a node still has a live writer on the old platform is not decided
    here.  ``Monitor.reauthorize`` has to prove the old writer stopped (or
    superseded it as a handoff predecessor) before any second writer exists;
    this rewrite only changes which platform the node's contract names.
    """
    movable: List[str] = []
    for node in nodes:
        current = snapshot_nodes.get(node.id)
        if not isinstance(current, Mapping):
            raise HandoffRebindError(
                "platform handoff: 运行快照里没有节点 {}".format(node.id)
            )
        if current.get("status") != "accepted":
            movable.append(node.id)
    return movable


def rebind_nodes(
    nodes: Sequence[DAGNode], movable: Iterable[str], target: str,
    topologies: Optional[Mapping[str, str]] = None,
) -> Tuple[List[DAGNode], List[str]]:
    """Copies of ``nodes`` with only the movable ones' routing moved to ``target``.

    Accepted nodes are returned as the very same objects: their dispatch
    binding is history and is never rewritten.  ``topologies`` names the
    dispatch topology each moved node gets on ``target``; only a contract the
    authorization stamped carries one, so old contracts are unaffected.
    Returns the new node list and the ids that actually changed platform.
    """
    movable_ids = set(movable)
    result: List[DAGNode] = []
    moved: List[str] = []
    for node in nodes:
        if node.id not in movable_ids or node_adapter_id(node) == target:
            result.append(node)
            continue
        contract = rebind_dispatch_contract(
            node.contract, target, (topologies or {}).get(node.id)
        )
        result.append(DAGNode.from_dict(dict(node.to_dict(), contract=contract)))
        moved.append(node.id)
    return result, sorted(moved)


# ------------------------------------------------------------ drain (PRD D2)

#: The one supported command that registers a stopped old-platform worker.
STOP_EVIDENCE_KIND = "handoff_worker_stopped"
#: How the old worker was established as stopped.  The second value is the
#: way out when the old platform can no longer be opened at all.
STOP_EVIDENCE_HOW = ("stopped_on_old_platform", "old_platform_unreachable")
#: Exactly these keys, nothing else (``Monitor.reconcile_package`` refuses the rest).
STOP_EVIDENCE_FIELDS = (
    "kind", "node_id", "role", "task_id", "stopped_on", "how_stopped", "note",
)


class HandoffNotDrained(HandoffRebindError):
    """A worker may still be writing on the old platform: the handoff is refused."""

    def __init__(self, report: Dict[str, Any]):
        self.report = report
        super().__init__(report["message"])


class HandoffProjectMismatch(HandoffRebindError):
    """The calling session is in another project than the run."""


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def project_identity_problem(
    root: Path, recorded_worktrees: Iterable[str], contract_matches: bool
) -> Optional[str]:
    """Why the calling project is not the run's project, or ``None``.

    Two independent records name the run's project: the session capability
    contract (bound to the project root by digest) and the absolute worktree
    paths the run created.  Either one disagreeing means another project --
    a copy of the repository, say -- and a handoff there would point a second
    copy of the run at the same branches.
    """
    resolved = Path(root).resolve(strict=False)
    if not contract_matches:
        return (
            "platform handoff: 项目身份不同：本会话的项目根 {} 与会话能力合同登记的项目不一致；"
            "请在运行所在的项目里执行交接".format(resolved)
        )
    foreign = sorted(
        item for item in recorded_worktrees
        if isinstance(item, str) and Path(item).is_absolute()
        and not _within(Path(item).resolve(strict=False), resolved)
    )
    if foreign:
        return (
            "platform handoff: 项目身份不同：本会话的项目根是 {}，运行登记的工作树在 {}；"
            "请在运行所在的项目里执行交接".format(resolved, foreign[0])
        )
    return None


def stop_evidence_template(blocker: Mapping[str, Any], from_platform: str) -> Dict[str, Any]:
    """The evidence package to fill in for one live worker (every field named)."""
    return {
        "kind": STOP_EVIDENCE_KIND,
        "node_id": blocker["node_id"],
        "role": blocker["role"],
        "task_id": blocker["task_id"],
        "stopped_on": from_platform or "<旧平台>",
        "how_stopped": "|".join(STOP_EVIDENCE_HOW),
        "note": "<怎么确认它已停：在旧平台停掉的会话名/时间，或旧平台打不开的原因>",
    }


def drain_report(
    blockers: Sequence[Mapping[str, Any]], *, plan_id: str, run_id: str,
    project_root: Path, from_platform: str, to_platform: str,
) -> Dict[str, Any]:
    """The refusal a handoff returns while old-platform workers may be live."""
    root = str(Path(project_root).resolve(strict=False))
    evidence_file = ".vibe/handoff-stop-<节点>-<角色>.json"
    command = (
        "vibe reconcile --project-root {root} --plan {plan} --run-id {run} --evidence {file}"
    ).format(root=root, plan=plan_id, run=run_id, file=evidence_file)
    nodes = sorted({str(item["node_id"]) for item in blockers})
    return {
        "message": (
            "platform handoff: 旧平台 {} 上还有 worker 可能在写（{}），交接已拒绝，运行状态未改动。"
            "排空后重跑交接：①等它交付；②在旧平台停掉它（旧平台打不开时写明原因），"
            "再用 `{}` 登记终止证据".format(from_platform or "未知", ", ".join(nodes), command)
        ),
        "from_platform": from_platform,
        "to_platform": to_platform,
        "live_workers": [dict(item) for item in blockers],
        "nodes": nodes,
        "drain_options": [
            {"option": "wait_for_delivery",
             "text": "等该 worker 交付：旧平台监工接收交付后重跑交接"},
            {"option": "register_stop_evidence",
             "text": "在旧平台停掉该 worker（旧平台打不开时 how_stopped 写 old_platform_unreachable），"
                     "把下面的证据写进项目内文件后执行 stop_evidence_command，再重跑交接"},
        ],
        "stop_evidence_command": command,
        "stop_evidence_fields": list(STOP_EVIDENCE_FIELDS),
        "stop_evidence_templates": [
            stop_evidence_template(item, from_platform) for item in blockers
        ],
    }


_CLOSURE_READY = ("new_registered", "address_verified", "heartbeat_switched", "inflight_taken")
_CLOSURE_RECOVERY = {
    "pin": "用宿主 advertised 的置顶工具对新班线程重试一次，拿到结构化回执后再重跑接班收口",
    "archive": "仅对旧班线程补归档（不要另开接班会话、不要重复消费任务），拿到回执后重跑收口",
}


def _receipt_state(receipt: Any, tool: str, thread: str, ctx: Mapping[str, Any]) -> str:
    """ok / unknown / mismatch for one host receipt; prose, timeouts never count as ok."""
    if receipt is None:
        return "unknown"
    if not isinstance(receipt, Mapping) or receipt.get("native") is not True \
            or receipt.get("tool") != tool or receipt.get("ok") is not True:
        return "unknown"
    for key, want in ctx.items():
        if receipt.get(key) != want:
            return "mismatch"
    return "ok" if receipt.get("thread_id") == thread else "mismatch"


def evaluate_shift_closure(
    *, run_id: str, plan_id: str, epoch: int, host_id: str, project: str,
    old_thread: str, new_thread: str, worker_threads: Iterable[str] = (),
    new_registered: bool, address_verified: bool, heartbeat_switched: bool, inflight_taken: bool,
    pin_receipt: Any = None, archive_receipt: Any = None,
) -> Dict[str, Any]:
    """Decide whether a supervisor shift is closed: new shift pinned, old shift archived.

    Pure.  Only native host receipts bound to this run/epoch/host/project count;
    the old shift may be archived only after the new one is registered, reachable,
    owns heartbeat/wake-up routing and has taken the in-flight requests.  Missing
    or unknown steps keep the shift open and name the recovery action.
    """
    result: Dict[str, Any] = {
        "state": "pending", "archive_allowed": False, "open_new_session": False,
        "missing": [], "recovery": [], "receipts": {}, "reason": "",
    }
    if not old_thread or not new_thread or old_thread == new_thread \
            or old_thread in set(worker_threads) or new_thread in set(worker_threads):
        result.update(state="refused", reason="目标不是旧班监工（与新班相同、或是 worker）：拒绝触碰")
        return result
    ctx = {"host_id": host_id, "run_id": run_id, "epoch": epoch, "project": project}
    flags = dict(new_registered=new_registered, address_verified=address_verified,
                 heartbeat_switched=heartbeat_switched, inflight_taken=inflight_taken)
    not_ready = [name for name in _CLOSURE_READY if not flags[name]]
    pin = _receipt_state(pin_receipt, "pin_thread", new_thread, ctx)
    archive_ready = not not_ready and pin == "ok"
    archive = _receipt_state(archive_receipt, "archive_thread", old_thread, ctx) if archive_ready else "unknown"
    if "mismatch" in (pin, archive) and (pin_receipt is not None or archive_receipt is not None):
        if pin == "mismatch" or (archive_ready and archive == "mismatch"):
            result.update(state="refused", reason="回执与 run/epoch/host/项目/线程不匹配：拒绝")
            return result
    missing = list(not_ready)
    if pin != "ok":
        missing.append("pin")
    if archive != "ok":
        missing.append("archive")
    result["missing"] = missing
    result["archive_allowed"] = archive_ready
    result["recovery"] = [_CLOSURE_RECOVERY[m] for m in missing if m in _CLOSURE_RECOVERY]
    if not_ready:
        result["recovery"].append("先完成：" + "、".join(not_ready) + "；之前不得归档旧班")
    if not missing:
        result.update(state="complete", receipts={"pin": pin_receipt, "archive": archive_receipt})
    else:
        unknown_receipt = any(
            (r is not None and s == "unknown") for r, s in ((pin_receipt, pin), (archive_receipt, archive)))
        result["state"] = "unknown" if unknown_receipt else "pending"
    return result


# ---------------------------------------------------------------------------
# P1c: bounded rework and automatic relay to a new developer.
#
# Pure bookkeeping only.  ``Monitor`` owns observation (is the old writer
# stopped, what is HEAD) and persistence; nothing here touches a file.  The
# ledger lives on the node, so a restart neither double counts nor resets it.
# ---------------------------------------------------------------------------

import hashlib
import json

REWORK_ROUND_LIMIT = 3
#: Reasons that never spend an implementation round: the developer was not at
#: fault (missing upstream material, environment/permission faults, a pure
#: format correction, or a supervisor-side problem).
NON_COUNTING_REASONS = frozenset({
    "upstream_missing", "environment", "permission", "format_only",
    "supervisor", "transport_timeout", "scope_not_landed",
})


def new_rework_ledger() -> Dict[str, Any]:
    """Node-lineage totals survive a relay; ``local_rounds`` restarts per worker."""
    return {"local_rounds": 0, "total_rounds": 0, "relays": 0,
            "counted": [], "failed_approaches": [], "cost": 0}


def finding_fingerprint(finding: Any) -> str:
    """Identity of a finding independent of the id the reviewer gave it."""
    text = finding.get("finding") if isinstance(finding, Mapping) else finding
    normal = " ".join(str(text or "").lower().split())
    return hashlib.sha256(normal.encode("utf-8")).hexdigest()[:16]


def _ledger(raw: Any) -> Dict[str, Any]:
    ledger = new_rework_ledger()
    if isinstance(raw, Mapping):
        for key in ledger:
            if key in raw:
                ledger[key] = raw[key]
    ledger["counted"] = list(ledger["counted"] or [])
    ledger["failed_approaches"] = list(ledger["failed_approaches"] or [])
    return ledger


def record_review_failure(raw: Any, *, action_id: str, finding: Any,
                          reason_class: Optional[str] = None,
                          cost: int = 0) -> Tuple[Dict[str, Any], bool]:
    """``(ledger, counted)``.  A repeated ``action_id`` or a non-counting
    reason leaves the round counters untouched (cost is still accumulated once).
    """
    ledger = _ledger(raw)
    if action_id in ledger["counted"]:
        return ledger, False
    ledger["counted"].append(action_id)
    if isinstance(cost, int) and not isinstance(cost, bool) and cost > 0:
        ledger["cost"] += cost
    if reason_class in NON_COUNTING_REASONS:
        return ledger, False
    ledger["local_rounds"] += 1
    ledger["total_rounds"] += 1
    fingerprint = finding_fingerprint(finding)
    if fingerprint not in ledger["failed_approaches"]:
        ledger["failed_approaches"].append(fingerprint)
    return ledger, True


def relay_due(raw: Any) -> bool:
    return _ledger(raw)["local_rounds"] >= REWORK_ROUND_LIMIT


def cost_exhausted(raw: Any, cap: Optional[int]) -> bool:
    return cap is not None and _ledger(raw)["cost"] >= cap


def start_relay(raw: Any) -> Dict[str, Any]:
    """Ledger after handing the write right to a new developer."""
    ledger = _ledger(raw)
    ledger["local_rounds"] = 0
    ledger["relays"] += 1
    return ledger


def relay_pack(*, node_id: str, head: str, dirty_files: Sequence[str],
               ledger: Mapping[str, Any], predecessor_task_id: str,
               reviewer_identity: str) -> Dict[str, Any]:
    """Frozen hand-over for the new developer; the reviewer stays the same."""
    led = _ledger(ledger)
    return {
        "node_id": node_id,
        "frozen_head": head,
        "uncommitted_files": sorted(str(item) for item in dirty_files),
        "predecessor_task_id": predecessor_task_id,
        "reviewer_identity": reviewer_identity,
        "total_rounds": led["total_rounds"],
        "relays": led["relays"],
        "failed_approaches": led["failed_approaches"],
        "cost": led["cost"],
    }
