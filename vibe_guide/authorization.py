"""Digest-bound, action-level authorization for V3 execution contracts."""

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath
import secrets
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .models import AgentCapabilities, DAGNode, Plan


AUTHORIZATION_SCHEMA_VERSION = 3
DEFAULT_ACTIONS = (
    "accept", "commit", "create_mr", "develop", "merge", "push",
    "review", "rework", "test",
)
ALLOWED_ACTIONS = DEFAULT_ACTIONS
EXCLUDED_ACTIONS = ("deploy",)
V2_EXCLUDED_ACTIONS = ("create_mr", "deploy", "merge", "push")
_VALID_ACTIONS = frozenset(DEFAULT_ACTIONS + ("merge_local",))
_MERGE_ACTIONS = frozenset(("merge", "merge_local"))
_ACTION_ALIASES = {"local_merge": "merge_local", "remote_merge": "merge"}
_ACTION_KEYS = {"action", "actions", "allowed_actions", "requested_actions"}
_SENSITIVE_NAMES = ("api_key", "credential", "password", "private_key", "secret", "token")
_SCOPE_ALIASES = {
    "issue": "issue_id", "issue_id": "issue_id", "source": "source_sha",
    "source_sha": "source_sha", "target": "target_branch", "target_ref": "target_branch",
    "target_branch": "target_branch", "change_request": "change_request_id",
    "change_request_id": "change_request_id", "request_id": "change_request_id",
    "mr_id": "change_request_id", "pr_id": "change_request_id", "name": "change_request_id",
}
ACTION_SCOPES = {
    "commit": "current_issue_writer_worktree_branch_allowlist",
    "push": "verified_remote_target_branch_and_paths",
    "create_mr": "bound_source_target_sha_and_change_request",
    "merge": "bound_local_or_remote_target_sha_merge_base_and_capability_evidence",
    "deploy": "requires_separate_manifest_and_AUTHORIZE_DEPLOY",
}
GOVERNANCE_PRECONDITIONS = (
    "dag_audit_reviewed", "plan_confirmation_bound", "node_adapter_id_equals_agent_id",
    "guidance_contract_version_and_hash_verified", "provider_capabilities_fresh_with_evidence_ref",
    "independent_supervisor_context_verified", "unique_writer_worktree_branch_allowlist_verified",
    "monitor_runtime_supports_authorization_schema_v3_and_action_scope",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _action(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("authorization action scope is invalid")
    return _ACTION_ALIASES.get(value.strip().casefold(), value.strip().casefold())


def _validate_actions(values: Iterable[Any]) -> Tuple[str, ...]:
    normalized = tuple(_action(value) for value in values)
    if (not normalized or len(normalized) != len(set(normalized))
            or not set(normalized) <= _VALID_ACTIONS or "deploy" in normalized):
        raise ValueError("authorization action scope is invalid")
    return normalized


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str) or len(value.strip()) != 40:
        raise ValueError("{} must be a 40-character SHA".format(field))
    value = value.strip().lower()
    if any(char not in "0123456789abcdef" for char in value):
        raise ValueError("{} must be a hexadecimal SHA".format(field))
    return value


def _normalize_scope(scope: Any) -> Optional[Dict[str, str]]:
    if scope is None:
        return None
    if hasattr(scope, "to_dict"):
        scope = scope.to_dict()
    if not isinstance(scope, Mapping):
        raise ValueError("merge scope must be a mapping")
    result: Dict[str, str] = {}
    for key, value in scope.items():
        canonical = _SCOPE_ALIASES.get(str(key).strip().casefold())
        if canonical is None or not isinstance(value, str) or not value.strip():
            raise ValueError("merge scope is invalid")
        value = value.strip()
        if canonical in result and result[canonical] != value:
            raise ValueError("merge scope contains conflicting aliases")
        result[canonical] = value
    required = ("issue_id", "source_sha", "target_branch", "change_request_id")
    if any(item not in result for item in required):
        raise ValueError("merge scope requires issue, source SHA, target branch and Change Request")
    result["source_sha"] = _sha(result["source_sha"], "source_sha")
    return {item: result[item] for item in required}


def _scope_from(value: Any) -> Optional[Dict[str, str]]:
    scope = value.get("merge_scope") if isinstance(value, Mapping) else getattr(value, "merge_scope", None)
    return _normalize_scope(scope)


def _authorized_actions(value: Any) -> set:
    values = value.get("allowed_actions", ()) if isinstance(value, Mapping) else getattr(value, "allowed_actions", ())
    if not isinstance(values, (list, tuple, set, frozenset)):
        return set()
    try:
        return {_action(item) for item in values}
    except ValueError:
        return set()


def is_action_authorized(authorization: Any, action: str, merge_scope: Optional[Mapping[str, Any]] = None) -> bool:
    if isinstance(authorization, AuthorizationCard) or (
        isinstance(authorization, Mapping) and authorization.get("schema_version") == AUTHORIZATION_SCHEMA_VERSION
    ):
        try:
            if isinstance(authorization, AuthorizationCard):
                if authorization.confirmation_status != "confirmed" or authorization.digest != _digest(_payload(authorization)):
                    return False
            else:
                candidate = AuthorizationRecord.from_dict(dict(authorization))
                if candidate.confirmation_status != "confirmed" or not is_authorization_integrity_valid(candidate):
                    return False
        except (TypeError, ValueError, AttributeError):
            return False
    try:
        normalized = _action(action)
    except ValueError:
        return False
    if normalized not in _VALID_ACTIONS or normalized not in _authorized_actions(authorization):
        return False
    if normalized in _MERGE_ACTIONS:
        card_scope = _scope_from(authorization)
        if card_scope is None:
            return merge_scope is None
        if merge_scope is not None:
            try:
                return card_scope == _normalize_scope(merge_scope)
            except ValueError:
                return False
    return True


def require_action_authorized(authorization: Any, action: str, merge_scope: Optional[Mapping[str, Any]] = None) -> None:
    if not is_action_authorized(authorization, action, merge_scope):
        raise PermissionError("action is not explicitly authorized: {}".format(action))


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return not (normalized.endswith("_digest") or normalized.endswith("_ref")) and any(name in normalized for name in _SENSITIVE_NAMES)


def _normalize_files(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)) or len(value) > 256:
        raise ValueError("files must be a bounded list")
    result = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or "\\" in item or "\x00" in item:
            raise ValueError("file scope contains an invalid path")
        path = PurePosixPath(item.strip())
        if path.is_absolute() or ".." in path.parts or path.as_posix() in {"", "."}:
            raise ValueError("file scope must remain inside the project")
        result.append(path.as_posix())
    if len(result) != len(set(result)):
        raise ValueError("file scope contains duplicate normalized paths")
    return result


def _normalize_contract(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_normalize_contract(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key in sorted(value):
            if not isinstance(key, str):
                raise ValueError("executable contract keys must be strings")
            if _is_sensitive_key(key):
                raise ValueError("raw secret fields are forbidden in executable contracts")
            normalized_key = key.casefold().replace("-", "_")
            item = value[key]
            if normalized_key in _ACTION_KEYS:
                item = _validate_actions(item if isinstance(item, (list, tuple)) else (item,))
                item = item[0] if isinstance(value[key], str) else list(item)
            elif normalized_key == "files":
                item = _normalize_files(item)
            else:
                item = _normalize_contract(item)
            result[key] = item
        return result
    raise ValueError("executable contract must be JSON-safe")


def _scoped_values(value: Any, key_name: str) -> List[str]:
    result: List[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if str(key).casefold().replace("-", "_") == key_name:
                result.extend([item] if isinstance(item, str) else item)
            else:
                result.extend(_scoped_values(item, key_name))
    elif isinstance(value, list):
        for item in value:
            result.extend(_scoped_values(item, key_name))
    return result


def canonical_node_contracts(nodes: List[DAGNode]) -> Tuple[Dict[str, Any], ...]:
    if len({node.id for node in nodes}) != len(nodes):
        raise ValueError("duplicate executable node ids are not allowed")
    return tuple({
        "id": node.id, "title": node.title, "depends_on": sorted(node.depends_on),
        "integration_after": sorted(node.integration_after), "parallel_group": node.parallel_group,
        "status": node.status, "contract": _normalize_contract(node.contract),
    } for node in sorted(nodes, key=lambda item: item.id))


def executable_contract_digest(nodes: List[DAGNode]) -> str:
    return _digest({"nodes": canonical_node_contracts(nodes)})


def affected_node_closure(nodes: List[DAGNode], changed_nodes: List[str]) -> List[str]:
    ids = {node.id for node in nodes}
    if any(item not in ids for item in changed_nodes):
        raise ValueError("changed node is outside the authorized DAG")
    reverse = {item: set() for item in ids}
    for node in nodes:
        for parent in set(node.depends_on + node.integration_after):
            if parent in reverse:
                reverse[parent].add(node.id)
    affected, pending = set(changed_nodes), list(changed_nodes)
    while pending:
        for child in sorted(reverse[pending.pop()]):
            if child not in affected:
                affected.add(child); pending.append(child)
    return sorted(affected)


def validate_runtime_contract(contract: Dict[str, Any], authorized_actions: Optional[Tuple[str, ...]] = None, authorized_files: Optional[Tuple[str, ...]] = None) -> Dict[str, Any]:
    normalized = _normalize_contract(contract)
    if not isinstance(normalized, dict):
        raise ValueError("runtime contract must be an object")
    if authorized_actions is not None and any(item not in set(authorized_actions) for key in _ACTION_KEYS for item in _scoped_values(normalized, key)):
        raise ValueError("runtime action is outside the authorized allowlist")
    if authorized_files is not None and any(item not in set(authorized_files) for item in _scoped_values(normalized, "files")):
        raise ValueError("runtime file is outside the authorized scope")
    return normalized


def _payload(card: Any) -> Dict[str, Any]:
    payload = {
        "schema_version": card.schema_version, "plan_id": card.plan_id, "plan_version": card.plan_version,
        "node_ids": tuple(card.node_ids), "file_scope": tuple(card.file_scope), "worker_scope": tuple(card.worker_scope),
        "agent_id": card.agent_id, "allowed_actions": tuple(card.allowed_actions), "excluded_actions": tuple(card.excluded_actions),
        "node_contract_digest": card.node_contract_digest, "decision_digest": card.decision_digest,
        "active_pair_limit": card.active_pair_limit,
        "plan_binding_digest": card.plan_binding_digest, "dag_audit_digest": card.dag_audit_digest,
        "action_permissions": dict(card.action_permissions or {}), "action_scopes": dict(card.action_scopes or {}),
        "governance_preconditions": tuple(card.governance_preconditions),
        "confirmation_status": card.confirmation_status,
    }
    if card.merge_scope is not None:
        payload["merge_scope"] = card.merge_scope
    return payload


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
    merge_scope: Optional[Dict[str, str]] = None
    plan_binding_digest: str = ""
    dag_audit_digest: str = ""
    action_permissions: Dict[str, str] = None
    action_scopes: Dict[str, str] = None
    governance_preconditions: Tuple[str, ...] = GOVERNANCE_PRECONDITIONS
    confirmation_status: str = "pending_user_authorization"
    schema_version: int = AUTHORIZATION_SCHEMA_VERSION

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, Mapping):
            raise TypeError("authorization card must be a mapping")
        values = dict(data)
        values.setdefault("merge_scope", None)
        for key in ("node_ids", "file_scope", "worker_scope", "allowed_actions", "excluded_actions", "governance_preconditions"):
            if key in values:
                values[key] = tuple(values[key])
        return cls(**values)

    def render(self) -> str:
        lines = ["授权卡 {}@{}".format(self.plan_id, self.plan_version)]
        shown = ("commit", "push", "create_mr", "merge", "deploy")
        lines.append("动作：" + ", ".join("{}={}".format(item, "excluded" if item in self.excluded_actions else "allowed" if item in self.allowed_actions else "denied") for item in shown))
        lines.append("摘要：" + self.digest)
        return "\n".join(lines)


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
    merge_scope: Optional[Dict[str, str]] = None
    plan_binding_digest: str = ""
    dag_audit_digest: str = ""
    action_permissions: Dict[str, str] = None
    action_scopes: Dict[str, str] = None
    governance_preconditions: Tuple[str, ...] = GOVERNANCE_PRECONDITIONS
    confirmation_status: str = "confirmed"
    schema_version: int = AUTHORIZATION_SCHEMA_VERSION

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError("authorization record schema is invalid")
        required = {"schema_version", "plan_id", "plan_version", "node_ids", "file_scope", "worker_scope", "allowed_actions", "excluded_actions", "node_contract_digest", "decision_digest", "active_pair_limit", "digest", "agent_id", "merge_scope", "plan_binding_digest", "dag_audit_digest", "action_permissions", "action_scopes", "governance_preconditions", "confirmation_status"}
        if set(data) != required:
            raise ValueError("authorization record schema is invalid")
        values = dict(data)
        for key in ("node_ids", "file_scope", "worker_scope", "allowed_actions", "excluded_actions"):
            if not isinstance(values[key], (list, tuple)) or not all(isinstance(item, str) for item in values[key]):
                raise ValueError("authorization record sequence is invalid")
            values[key] = tuple(values[key])
        values["merge_scope"] = _normalize_scope(values["merge_scope"])
        values["governance_preconditions"] = tuple(values["governance_preconditions"])
        return cls(**values)


def build_authorization_card(plan: Plan = None, nodes: List[DAGNode] = None, capabilities: AgentCapabilities = None, active_pair_limit: Optional[int] = None, allowed_actions: Optional[Iterable[str]] = None, merge_scope: Optional[Mapping[str, Any]] = None, **kwargs) -> AuthorizationCard:
    # Keep the compact plan-object API used by the monitor, while accepting
    # the explicit field API used by standalone authorization clients.
    if not isinstance(plan, Plan):
        legacy = {
            "plan_id": kwargs.get("plan_id", plan),
            "plan_version": kwargs.get("plan_version"),
            "node_ids": kwargs.get("node_ids", ()),
            "file_scope": kwargs.get("file_scope", ()),
            "worker_scope": kwargs.get("worker_scope", ()),
        }
        if legacy["plan_version"] is None:
            raise TypeError("plan_version is required")
        actions = _validate_actions(DEFAULT_ACTIONS if allowed_actions is None else allowed_actions)
        scope = _normalize_scope(merge_scope if merge_scope is not None else kwargs.get("merge_binding", kwargs.get("merge_bindings")))
        if allowed_actions is not None and tuple(actions) != DEFAULT_ACTIONS and set(actions) & _MERGE_ACTIONS and scope is None:
            raise ValueError("explicit merge authorization requires merge scope")
        payload = {**legacy, "allowed_actions": actions, "excluded_actions": V2_EXCLUDED_ACTIONS, "merge_scope": scope}
        return {**payload, "digest": _digest(payload)}
    if tuple(sorted(node.id for node in nodes)) != tuple(sorted(plan.node_ids)):
        raise ValueError("authorization nodes must exactly match the plan")
    if active_pair_limit is None:
        active_pair_limit = max(1, len(nodes))
    if isinstance(active_pair_limit, bool) or not isinstance(active_pair_limit, int) or not 1 <= active_pair_limit <= 64:
        raise ValueError("active pair limit must be an integer from 1 to 64")
    actions = _validate_actions(DEFAULT_ACTIONS if allowed_actions is None else allowed_actions)
    normalized_nodes = [_normalize_contract(node.contract) for node in nodes]
    file_scope = tuple(sorted({path for contract in normalized_nodes for path in _scoped_values(contract, "files")}))
    worker_scope = tuple(sorted({str(node.contract["worker"]) for node in nodes if node.contract.get("worker")}))
    scope = _normalize_scope(merge_scope)
    if allowed_actions is not None and tuple(actions) != DEFAULT_ACTIONS and set(actions) & _MERGE_ACTIONS and scope is None:
        raise ValueError("explicit merge authorization requires merge scope")
    plan_binding_digest = _digest({"plan_id": plan.plan_id, "plan_version": plan.version, "node_ids": tuple(sorted(plan.node_ids))})
    action_permissions = {item: ("allowed" if item in actions else "denied") for item in ("commit", "push", "create_mr", "merge")}
    action_permissions["deploy"] = "excluded"
    card = AuthorizationCard(
        plan.plan_id, plan.version, tuple(sorted(plan.node_ids)), file_scope, worker_scope,
        capabilities.agent_id, actions, EXCLUDED_ACTIONS, executable_contract_digest(nodes),
        _digest({"decisions": plan.decisions, "evidence_priority": plan.evidence_priority}),
        active_pair_limit, "", scope, plan_binding_digest,
        str(kwargs.get("dag_audit_digest", "")), action_permissions,
        dict(ACTION_SCOPES), GOVERNANCE_PRECONDITIONS, "pending_user_authorization",
    )
    return AuthorizationCard(**{**card.to_dict(), "digest": _digest(_payload(card))})


def authorize(card: AuthorizationCard, confirmation: str) -> AuthorizationRecord:
    if confirmation != "AUTHORIZE":
        raise ValueError("authorization requires exact AUTHORIZE confirmation")
    if isinstance(card, Mapping):
        values = dict(card)
        values.setdefault("merge_scope", None)
        if "schema_version" not in values:
            payload = {key: values[key] for key in values if key != "digest"}
            if values.get("digest") != _digest(payload):
                raise ValueError("authorization card digest is invalid")
            return dict(values)
        for key in ("node_ids", "file_scope", "worker_scope", "allowed_actions", "excluded_actions"):
            if key in values:
                values[key] = tuple(values[key])
        card = AuthorizationCard(**values)
    if card.schema_version != AUTHORIZATION_SCHEMA_VERSION or tuple(card.allowed_actions) != _validate_actions(card.allowed_actions) or tuple(card.excluded_actions) != EXCLUDED_ACTIONS:
        raise ValueError("authorization action scope is invalid")
    if card.digest != _digest(_payload(card)):
        raise ValueError("authorization card digest is invalid")
    record = AuthorizationRecord(
        card.plan_id, card.plan_version, card.node_ids, card.file_scope, card.worker_scope,
        card.allowed_actions, card.excluded_actions, card.node_contract_digest, card.decision_digest,
        card.active_pair_limit, "", card.agent_id, card.merge_scope,
        card.plan_binding_digest, card.dag_audit_digest, dict(card.action_permissions or {}),
        dict(card.action_scopes or {}), tuple(card.governance_preconditions), "confirmed",
    )
    return AuthorizationRecord(**{**record.to_dict(), "digest": _digest(_payload(record))})


def is_authorization_integrity_valid(record: AuthorizationRecord) -> bool:
    try:
        return record.schema_version == AUTHORIZATION_SCHEMA_VERSION and tuple(record.excluded_actions) == EXCLUDED_ACTIONS and record.digest == _digest(_payload(record))
    except (TypeError, ValueError, AttributeError):
        return False


def is_authorization_valid(record: AuthorizationRecord, plan: Plan, nodes: Optional[List[DAGNode]] = None) -> bool:
    if not is_authorization_integrity_valid(record) or record.plan_id != plan.plan_id or record.plan_version != plan.version or record.node_ids != tuple(sorted(plan.node_ids)):
        return False
    if record.decision_digest != _digest({"decisions": plan.decisions, "evidence_priority": plan.evidence_priority}):
        return False
    if nodes is not None:
        try:
            return secrets.compare_digest(record.node_contract_digest, executable_contract_digest(nodes))
        except (TypeError, ValueError):
            return False
    return True


def refresh_authorization_card(plan: Plan, nodes: List[DAGNode], previous: AuthorizationCard) -> AuthorizationCard:
    authorize(previous, "AUTHORIZE")
    if previous.plan_id != plan.plan_id or previous.plan_version != plan.version or previous.node_ids != tuple(sorted(plan.node_ids)):
        raise ValueError("reauthorization must remain on the same plan revision")
    capabilities = AgentCapabilities(previous.agent_id, False, False, False, False, False, "guide")
    return build_authorization_card(plan, nodes, capabilities, previous.active_pair_limit, previous.allowed_actions, previous.merge_scope)


# Compatibility aliases used by the monitor and older integrations.
action_is_authorized = is_action_authorized
require_action = require_action_authorized
is_authorized_action = is_action_authorized
require_authorized_action = require_action_authorized
