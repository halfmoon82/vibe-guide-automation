from dataclasses import asdict, dataclass
import hashlib
import json
import secrets
from typing import Any, Dict, List, Tuple

from .models import AgentCapabilities, DAGNode, Plan


_ALLOWED_ACTIONS = ("accept", "commit", "develop", "review", "rework", "test")
_EXCLUDED_ACTIONS = ("create_mr", "deploy", "merge", "push")


def _canonical_digest(data: Dict[str, Any]) -> str:
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authorization_payload(
    plan_id: str,
    plan_version: int,
    node_ids: Tuple[str, ...],
    file_scope: Tuple[str, ...],
    worker_scope: Tuple[str, ...],
    agent_id: str,
    allowed_actions: Tuple[str, ...],
    excluded_actions: Tuple[str, ...],
) -> Dict[str, Any]:
    """Return the only data that is covered by an authorization digest."""

    return {
        "plan_id": plan_id,
        "plan_version": plan_version,
        "node_ids": tuple(sorted(node_ids)),
        "file_scope": tuple(sorted(file_scope)),
        "worker_scope": tuple(sorted(worker_scope)),
        "agent_id": agent_id,
        "allowed_actions": tuple(allowed_actions),
        "excluded_actions": tuple(excluded_actions),
    }


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
    digest: str

    def to_dict(self) -> Dict[str, Any]:
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
    digest: str
    agent_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_authorization_card(
    plan: Plan, nodes: List[DAGNode], capabilities: AgentCapabilities
) -> AuthorizationCard:
    node_ids = tuple(sorted(node.id for node in nodes))
    file_scope = tuple(
        sorted(
            {
                str(path)
                for node in nodes
                for path in node.contract.get("files", [])
            }
        )
    )
    worker_scope = tuple(
        sorted(
            {
                str(node.contract["worker"])
                for node in nodes
                if node.contract.get("worker")
            }
        )
    )
    canonical = {
        "plan_id": plan.plan_id,
        "plan_version": plan.version,
        "node_ids": node_ids,
        "file_scope": file_scope,
        "worker_scope": worker_scope,
        "agent_id": capabilities.agent_id,
        "allowed_actions": _ALLOWED_ACTIONS,
        "excluded_actions": _EXCLUDED_ACTIONS,
    }
    return AuthorizationCard(
        digest=_canonical_digest(
            _authorization_payload(
                canonical["plan_id"],
                canonical["plan_version"],
                canonical["node_ids"],
                canonical["file_scope"],
                canonical["worker_scope"],
                canonical["agent_id"],
                canonical["allowed_actions"],
                canonical["excluded_actions"],
            )
        ),
        **canonical,
    )


def authorize(card: AuthorizationCard, confirmation: str) -> AuthorizationRecord:
    if confirmation != "AUTHORIZE":
        raise ValueError("authorization requires exact AUTHORIZE confirmation")
    if "deploy" not in card.excluded_actions or "deploy" in card.allowed_actions:
        raise ValueError("deploy must remain outside this authorization")
    expected = _canonical_digest(
        _authorization_payload(
            card.plan_id,
            card.plan_version,
            card.node_ids,
            card.file_scope,
            card.worker_scope,
            card.agent_id,
            card.allowed_actions,
            card.excluded_actions,
        )
    )
    if not secrets.compare_digest(card.digest, expected):
        raise ValueError("authorization card digest is invalid")
    return AuthorizationRecord(
        plan_id=card.plan_id,
        plan_version=card.plan_version,
        node_ids=card.node_ids,
        file_scope=card.file_scope,
        worker_scope=card.worker_scope,
        allowed_actions=card.allowed_actions,
        excluded_actions=card.excluded_actions,
        digest=card.digest,
        agent_id=card.agent_id,
    )


def is_authorization_valid(record: AuthorizationRecord, plan: Plan) -> bool:
    if record.plan_id != plan.plan_id or record.plan_version != plan.version:
        return False
    if record.node_ids != tuple(sorted(plan.node_ids)):
        return False
    if record.allowed_actions != _ALLOWED_ACTIONS:
        return False
    if record.excluded_actions != _EXCLUDED_ACTIONS:
        return False
    expected = _canonical_digest(
        _authorization_payload(
            record.plan_id,
            record.plan_version,
            record.node_ids,
            record.file_scope,
            record.worker_scope,
            record.agent_id,
            record.allowed_actions,
            record.excluded_actions,
        )
    )
    return secrets.compare_digest(record.digest, expected)
