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
    nodes: Sequence[DAGNode], movable: Iterable[str], target: str
) -> Tuple[List[DAGNode], List[str]]:
    """Copies of ``nodes`` with only the movable ones' routing moved to ``target``.

    Accepted nodes are returned as the very same objects: their dispatch
    binding is history and is never rewritten.  Returns the new node list and
    the ids that actually changed platform.
    """
    movable_ids = set(movable)
    result: List[DAGNode] = []
    moved: List[str] = []
    for node in nodes:
        if node.id not in movable_ids or node_adapter_id(node) == target:
            result.append(node)
            continue
        contract = rebind_dispatch_contract(node.contract, target)
        result.append(DAGNode.from_dict(dict(node.to_dict(), contract=contract)))
        moved.append(node.id)
    return result, sorted(moved)
