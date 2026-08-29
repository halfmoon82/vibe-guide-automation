"""Plan-bound authorization primitives shared by planning and execution stages."""

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Dict, List, Optional, Tuple

from .models import AgentCapabilities, DAGNode, Plan

AUTHORIZATION_SCHEMA_VERSION = 3
V3_ALLOWED_ACTIONS = ("commit", "push", "create_mr", "merge")
V3_EXCLUDED_ACTIONS = ("deploy",)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), default=list).encode()).hexdigest()


def executable_contract_digest(nodes: List[DAGNode]) -> str:
    payload = [{"id": n.id, "depends_on": sorted(n.depends_on),
                "integration_after": sorted(n.integration_after), "parallel_group": n.parallel_group,
                "status": n.status, "contract": n.contract, "risk_tags": n.risk_tags,
                "writer": n.writer, "worktree": n.worktree, "allowlist": n.allowlist}
               for n in sorted(nodes, key=lambda item: item.id)]
    return _digest(payload)


def canonical_node_contracts(nodes: List[DAGNode]):
    return tuple({"id": n.id, "contract": n.contract} for n in sorted(nodes, key=lambda item: item.id))


@dataclass(frozen=True)
class AuthorizationCard:
    plan_id: str
    plan_version: int
    node_ids: Tuple[str, ...]
    file_scope: Tuple[str, ...]
    worker_scope: Tuple[str, ...]
    agent_id: str
    allowed_actions: Tuple[str, ...]
    excluded_actions: Tuple[str, ...]
    node_contract_digest: str
    decision_digest: str
    active_pair_limit: int
    digest: str
    schema_version: int = AUTHORIZATION_SCHEMA_VERSION

    def to_dict(self):
        return asdict(self)


@dataclass(frozen=True)
class AuthorizationRecord:
    plan_id: str
    plan_version: int
    node_ids: Tuple[str, ...]
    file_scope: Tuple[str, ...]
    worker_scope: Tuple[str, ...]
    allowed_actions: Tuple[str, ...]
    excluded_actions: Tuple[str, ...]
    node_contract_digest: str
    decision_digest: str
    active_pair_limit: int
    digest: str
    agent_id: str = ""
    schema_version: int = AUTHORIZATION_SCHEMA_VERSION

    def to_dict(self):
        return asdict(self)


def _payload(card: Any) -> Dict[str, Any]:
    return {"schema_version": card.schema_version, "plan_id": card.plan_id,
            "plan_version": card.plan_version, "node_ids": tuple(sorted(card.node_ids)),
            "file_scope": tuple(sorted(card.file_scope)), "worker_scope": tuple(sorted(card.worker_scope)),
            "agent_id": card.agent_id, "allowed_actions": tuple(card.allowed_actions),
            "excluded_actions": tuple(card.excluded_actions), "node_contract_digest": card.node_contract_digest,
            "decision_digest": card.decision_digest, "active_pair_limit": card.active_pair_limit}


def build_authorization_card(plan: Plan, nodes: List[DAGNode], capabilities: AgentCapabilities,
                             active_pair_limit: Optional[int] = None,
                             allowed_actions: Optional[Tuple[str, ...]] = None) -> AuthorizationCard:
    if not isinstance(capabilities, AgentCapabilities):
        raise TypeError("capabilities must be AgentCapabilities")
    if tuple(sorted(plan.node_ids)) != tuple(sorted(node.id for node in nodes)):
        raise ValueError("authorization nodes must exactly match the plan")
    for node in nodes:
        if node.contract.get("adapter_id") != capabilities.agent_id:
            raise ValueError("node adapter routing does not match authorization")
    actions = tuple(allowed_actions or V3_ALLOWED_ACTIONS)
    if set(actions) != set(V3_ALLOWED_ACTIONS) or len(actions) != len(set(actions)):
        raise ValueError("authorization action scope must include exactly V3 Git actions")
    if active_pair_limit is None:
        active_pair_limit = max(1, len(nodes))
    if isinstance(active_pair_limit, bool) or not isinstance(active_pair_limit, int) or not 1 <= active_pair_limit <= 64:
        raise ValueError("active pair limit must be an integer from 1 to 64")
    files = sorted({item for node in nodes for item in (node.contract.get("allowlist") or node.allowlist)})
    workers = sorted({str(node.contract.get("writer") or node.writer) for node in nodes})
    decision_digest = _digest({"decisions": plan.decisions, "evidence_priority": plan.evidence_priority})
    provisional = AuthorizationCard(plan.plan_id, plan.version, tuple(sorted(plan.node_ids)), tuple(files), tuple(workers),
                                     capabilities.agent_id, actions, V3_EXCLUDED_ACTIONS,
                                     executable_contract_digest(nodes), decision_digest, active_pair_limit, "")
    return AuthorizationCard(digest=_digest(_payload(provisional)),
                             **{key: getattr(provisional, key) for key in provisional.__dataclass_fields__ if key != "digest"})


def is_authorization_integrity_valid(record: Any) -> bool:
    if not isinstance(record, (AuthorizationCard, AuthorizationRecord)) or record.schema_version != AUTHORIZATION_SCHEMA_VERSION:
        return False
    if tuple(record.allowed_actions) != V3_ALLOWED_ACTIONS or tuple(record.excluded_actions) != V3_EXCLUDED_ACTIONS:
        return False
    return record.digest == _digest(_payload(record))


def authorize(card: AuthorizationCard, confirmation: str) -> AuthorizationRecord:
    if confirmation != "AUTHORIZE":
        raise ValueError("authorization requires exact AUTHORIZE confirmation")
    if not is_authorization_integrity_valid(card):
        raise ValueError("authorization card digest is invalid")
    return AuthorizationRecord(card.plan_id, card.plan_version, card.node_ids, card.file_scope,
                               card.worker_scope, card.allowed_actions, card.excluded_actions,
                               card.node_contract_digest, card.decision_digest, card.active_pair_limit,
                               card.digest, card.agent_id)


def is_authorization_valid(record: AuthorizationRecord, plan: Plan, nodes: Optional[List[DAGNode]] = None) -> bool:
    if not is_authorization_integrity_valid(record):
        return False
    if record.plan_id != plan.plan_id or record.plan_version != plan.version or record.node_ids != tuple(sorted(plan.node_ids)):
        return False
    return nodes is None or record.node_contract_digest == executable_contract_digest(nodes)


def refresh_authorization_card(plan: Plan, nodes: List[DAGNode], previous: AuthorizationCard) -> AuthorizationCard:
    if not is_authorization_integrity_valid(previous) or previous.plan_id != plan.plan_id or previous.plan_version != plan.version:
        raise ValueError("reauthorization must remain on the same plan revision")
    capabilities = AgentCapabilities(previous.agent_id, False, False, False, False, False, "guide")
    return build_authorization_card(plan, nodes, capabilities, previous.active_pair_limit, previous.allowed_actions)


__all__ = ["AuthorizationCard", "AuthorizationRecord", "build_authorization_card", "authorize",
           "is_authorization_valid", "is_authorization_integrity_valid", "refresh_authorization_card",
           "executable_contract_digest", "canonical_node_contracts"]
