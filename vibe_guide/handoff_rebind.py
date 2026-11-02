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
