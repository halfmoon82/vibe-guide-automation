"""Plan-bound authorization with a digest of every executable node contract."""

from dataclasses import asdict, dataclass
import hashlib
import json
import secrets
from typing import Any, Dict, List, Optional, Tuple

from .models import AgentCapabilities, DAGNode, Plan


AUTHORIZATION_SCHEMA_VERSION = 1
_ALLOWED_ACTIONS = ("accept", "commit", "develop", "review", "rework", "test")
_EXCLUDED_ACTIONS = ("create_mr", "deploy", "merge", "push")
_ACTION_KEYS = {"action", "actions", "allowed_actions", "requested_actions"}
_SENSITIVE_NAMES = (
    "api_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


def _canonical_digest(data: Dict[str, Any]) -> str:
    encoded = json.dumps(
        data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized.endswith("_digest") or normalized.endswith("_ref"):
        return False
    return any(name in normalized for name in _SENSITIVE_NAMES)


def _normalize_action_value(value: Any, path: str) -> Any:
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if not normalized:
            raise ValueError("executable action must not be empty at " + path)
        if normalized in _EXCLUDED_ACTIONS:
            raise ValueError("executable contract requests an excluded action")
        return normalized
    if isinstance(value, (list, tuple)):
        return [
            _normalize_action_value(item, "{}[{}]".format(path, index))
            for index, item in enumerate(value)
        ]
    raise ValueError("executable action must be a string or list at " + path)


def _normalize_contract(value: Any, path: str = "contract") -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [
            _normalize_contract(item, "{}[{}]".format(path, index))
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("executable contract keys must be strings")
            if _is_sensitive_key(key):
                raise ValueError("raw secret fields are forbidden in executable contracts")
            item_path = path + "." + key
            if key.casefold().replace("-", "_") in _ACTION_KEYS:
                item = _normalize_action_value(value[key], item_path)
            else:
                item = _normalize_contract(value[key], item_path)
            result[key] = item
        return result
    raise ValueError("executable contract must be JSON-safe at " + path)


def canonical_node_contracts(nodes: List[DAGNode]) -> Tuple[Dict[str, Any], ...]:
    if len({node.id for node in nodes}) != len(nodes):
        raise ValueError("duplicate executable node ids are not allowed")
    canonical = []
    for node in sorted(nodes, key=lambda item: item.id):
        canonical.append(
            {
                "id": node.id,
                "title": node.title,
                "depends_on": sorted(node.depends_on),
                "integration_after": sorted(node.integration_after),
                "parallel_group": node.parallel_group,
                "status": node.status,
                "contract": _normalize_contract(node.contract),
            }
        )
    return tuple(canonical)


def executable_contract_digest(nodes: List[DAGNode]) -> str:
    return _canonical_digest({"nodes": canonical_node_contracts(nodes)})


def validate_runtime_contract(contract: Dict[str, Any]) -> None:
    """Reject excluded actions, secrets, and non-JSON runtime values."""

    _normalize_contract(contract, "runtime_contract")


def _authorization_payload(
    plan_id: str,
    plan_version: int,
    node_ids: Tuple[str, ...],
    file_scope: Tuple[str, ...],
    worker_scope: Tuple[str, ...],
    agent_id: str,
    allowed_actions: Tuple[str, ...],
    excluded_actions: Tuple[str, ...],
    node_contract_digest: str,
) -> Dict[str, Any]:
    return {
        "schema_version": AUTHORIZATION_SCHEMA_VERSION,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "node_ids": tuple(sorted(node_ids)),
        "file_scope": tuple(sorted(file_scope)),
        "worker_scope": tuple(sorted(worker_scope)),
        "agent_id": agent_id,
        "allowed_actions": tuple(allowed_actions),
        "excluded_actions": tuple(excluded_actions),
        "node_contract_digest": node_contract_digest,
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
    node_contract_digest: str
    digest: str
    schema_version: int = AUTHORIZATION_SCHEMA_VERSION

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
    node_contract_digest: str
    digest: str
    agent_id: str = ""
    schema_version: int = AUTHORIZATION_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AuthorizationRecord":
        required = {
            "schema_version",
            "plan_id",
            "plan_version",
            "node_ids",
            "file_scope",
            "worker_scope",
            "allowed_actions",
            "excluded_actions",
            "node_contract_digest",
            "digest",
            "agent_id",
        }
        if not isinstance(data, dict) or set(data) != required:
            raise ValueError("authorization record schema is invalid")
        if data["schema_version"] != AUTHORIZATION_SCHEMA_VERSION:
            raise ValueError("unsupported authorization record schema")
        converted = dict(data)
        for key in (
            "node_ids",
            "file_scope",
            "worker_scope",
            "allowed_actions",
            "excluded_actions",
        ):
            if not isinstance(converted[key], (list, tuple)) or not all(
                isinstance(item, str) for item in converted[key]
            ):
                raise ValueError("authorization record sequence is invalid")
            converted[key] = tuple(converted[key])
        return cls(**converted)


def build_authorization_card(
    plan: Plan, nodes: List[DAGNode], capabilities: AgentCapabilities
) -> AuthorizationCard:
    node_ids = tuple(sorted(node.id for node in nodes))
    if node_ids != tuple(sorted(plan.node_ids)):
        raise ValueError("authorization nodes must exactly match the plan")
    contract_digest = executable_contract_digest(nodes)
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
    canonical = _authorization_payload(
        plan.plan_id,
        plan.version,
        node_ids,
        file_scope,
        worker_scope,
        capabilities.agent_id,
        _ALLOWED_ACTIONS,
        _EXCLUDED_ACTIONS,
        contract_digest,
    )
    return AuthorizationCard(digest=_canonical_digest(canonical), **canonical)


def authorize(card: AuthorizationCard, confirmation: str) -> AuthorizationRecord:
    if confirmation != "AUTHORIZE":
        raise ValueError("authorization requires exact AUTHORIZE confirmation")
    canonical = _authorization_payload(
        card.plan_id,
        card.plan_version,
        card.node_ids,
        card.file_scope,
        card.worker_scope,
        card.agent_id,
        card.allowed_actions,
        card.excluded_actions,
        card.node_contract_digest,
    )
    if card.schema_version != AUTHORIZATION_SCHEMA_VERSION:
        raise ValueError("unsupported authorization card schema")
    if card.allowed_actions != _ALLOWED_ACTIONS or card.excluded_actions != _EXCLUDED_ACTIONS:
        raise ValueError("authorization action scope is invalid")
    if not secrets.compare_digest(card.digest, _canonical_digest(canonical)):
        raise ValueError("authorization card digest is invalid")
    return AuthorizationRecord(
        plan_id=card.plan_id,
        plan_version=card.plan_version,
        node_ids=card.node_ids,
        file_scope=card.file_scope,
        worker_scope=card.worker_scope,
        allowed_actions=card.allowed_actions,
        excluded_actions=card.excluded_actions,
        node_contract_digest=card.node_contract_digest,
        digest=card.digest,
        agent_id=card.agent_id,
    )


def is_authorization_valid(
    record: AuthorizationRecord,
    plan: Plan,
    nodes: Optional[List[DAGNode]] = None,
) -> bool:
    if not is_authorization_integrity_valid(record):
        return False
    if record.plan_id != plan.plan_id or record.plan_version != plan.version:
        return False
    if record.node_ids != tuple(sorted(plan.node_ids)):
        return False
    if nodes is not None:
        try:
            live_digest = executable_contract_digest(nodes)
        except (TypeError, ValueError):
            return False
        if not secrets.compare_digest(record.node_contract_digest, live_digest):
            return False
    return True


def is_authorization_integrity_valid(record: AuthorizationRecord) -> bool:
    if record.schema_version != AUTHORIZATION_SCHEMA_VERSION:
        return False
    if record.allowed_actions != _ALLOWED_ACTIONS or record.excluded_actions != _EXCLUDED_ACTIONS:
        return False
    canonical = _authorization_payload(
        record.plan_id,
        record.plan_version,
        record.node_ids,
        record.file_scope,
        record.worker_scope,
        record.agent_id,
        record.allowed_actions,
        record.excluded_actions,
        record.node_contract_digest,
    )
    if not secrets.compare_digest(record.digest, _canonical_digest(canonical)):
        return False
    return True
