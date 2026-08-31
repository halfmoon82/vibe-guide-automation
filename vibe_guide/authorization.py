"""Plan-bound authorization with a digest of every executable node contract."""

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import PurePosixPath
import secrets
from typing import Any, Dict, List, Optional, Tuple

from .models import AgentCapabilities, DAGNode, Plan


AUTHORIZATION_SCHEMA_VERSION = 2
_ALLOWED_ACTIONS = ("accept", "commit", "develop", "review", "rework", "test")
_LOCAL_MERGE_ACTION = "merge_local"
_EXCLUDED_ACTIONS = ("create_mr", "deploy", "merge", "push")
_ACTION_KEYS = {"action", "actions", "allowed_actions", "requested_actions"}
_RUNTIME_ACTIONS = frozenset(_ALLOWED_ACTIONS + (_LOCAL_MERGE_ACTION,))
_SENSITIVE_NAMES = (
    "api_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)


def validate_git_action_target(action: Dict[str, Any]) -> None:
    """Require an explicit non-main merge target and explicit deploy exclusion."""
    if not isinstance(action, dict):
        raise ValueError("git action must be an object")
    if "merge" in action:
        raise ValueError("ambiguous merge action is not allowed")
    merge_requested = bool(action.get("merge_to_target_branch", False))
    target = action.get("merge_target_branch")
    if merge_requested and (not isinstance(target, str) or not target.strip()):
        raise ValueError("merge_target_branch is required")
    normalized_target = target.strip().casefold() if isinstance(target, str) else ""
    if normalized_target in {"main", "origin/main"} or action.get("merge_to_main") is True:
        raise ValueError("merge to main is excluded")
    if isinstance(target, str):
        action["merge_target_branch"] = target.strip()
    if action.get("deploy") is True:
        raise ValueError("deploy requires a separate authorization")
    for key in ("commit", "push", "create_change_request", "merge_to_target_branch", "merge_to_main", "deploy"):
        if key in action and type(action[key]) is not bool:
            raise ValueError("git action flag must be boolean: " + key)


def canonical_git_action(action: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(action, dict):
        raise ValueError("git action must be an object")
    result = {key: action.get(key, False) for key in
              ("commit", "push", "create_change_request", "merge_to_target_branch",
               "merge_target_branch", "merge_to_main", "deploy")}
    result.update({key: value for key, value in action.items() if key not in result})
    validate_git_action_target(result)
    if result["merge_to_target_branch"] and not result["merge_target_branch"]:
        raise ValueError("merge_target_branch is required")
    return result


def build_v38_authorization_card(
    plan_id: str,
    plan_revision: int,
    run_id: str,
    execution_epoch: int,
    scope: List[str],
    ready_nodes: List[str],
    file_scope: List[str],
    merge_target_branch: str,
    preflight_ref: str,
    confirmation_evidence: str,
    preflight_report: Any = None,
) -> Dict[str, Any]:
    """Build the V3.8 action-bound card after a ready preflight."""
    if preflight_report is not None:
        from .preflight import assert_authorizable
        assert_authorizable(preflight_report)
    if not merge_target_branch or merge_target_branch == "main":
        raise ValueError("V3.8 requires an explicit non-main merge target")
    if not scope or not ready_nodes:
        raise ValueError("V3.8 authorization scope and ready nodes are required")
    payload = {
        "schema_version": 1,
        "status": "authorized",
        "plan_id": plan_id,
        "plan_revision": plan_revision,
        "run_id": run_id,
        "execution_epoch": execution_epoch,
        "preflight_required": True,
        "preflight_status": "ready_to_authorize",
        "authorization_status": "authorized",
        "scope": list(scope),
        "ready_nodes": list(ready_nodes),
        "file_scope": sorted(set(file_scope)),
        "allowed_actions": ["accept", "commit", "develop", "test", "review", "rework",
                            "push", "create_change_request", "merge_to_target_branch"],
        "excluded_actions": ["deploy", "merge_to_main", "external_install", "system_permission_change"],
        "merge_target_branch": merge_target_branch,
        "merge_to_main": False,
        "deploy": False,
        "preflight_ref": preflight_ref,
        "confirmation_evidence": confirmation_evidence,
    }
    payload["digest"] = _canonical_digest(payload)
    return payload


def _valid_action_scope(actions: Any) -> bool:
    return (
        isinstance(actions, tuple)
        and bool(actions)
        and len(actions) == len(set(actions))
        and all(isinstance(action, str) and action in _RUNTIME_ACTIONS for action in actions)
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
        if normalized not in _RUNTIME_ACTIONS:
            raise ValueError("executable contract requests an unlisted action")
        return normalized
    if isinstance(value, (list, tuple)):
        if not value or len(value) > 64 or any(
            not isinstance(item, str) for item in value
        ):
            raise ValueError(
                "executable actions must be a bounded flat string list at " + path
            )
        return [
            _normalize_action_value(item, "{}[{}]".format(path, index))
            for index, item in enumerate(value)
        ]
    raise ValueError("executable action must be a string or list at " + path)


def _normalize_files(value: Any, path: str) -> List[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise ValueError("files must be a bounded list at " + path)
    result: List[str] = []
    for index, item in enumerate(value):
        if (
            not isinstance(item, str)
            or not item.strip()
            or "\\" in item
            or "\x00" in item
        ):
            raise ValueError("file scope contains an invalid path")
        candidate = PurePosixPath(item.strip())
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("file scope must remain inside the project")
        normalized = candidate.as_posix()
        if normalized in {"", "."}:
            raise ValueError("file scope contains an invalid path")
        result.append(normalized)
    if len(result) != len(set(result)):
        raise ValueError("file scope contains duplicate normalized paths")
    return result


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
            normalized_key = key.casefold().replace("-", "_")
            if normalized_key in _ACTION_KEYS:
                item = _normalize_action_value(value[key], item_path)
            elif normalized_key == "files":
                item = _normalize_files(value[key], item_path)
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


def affected_node_closure(
    nodes: List[DAGNode], changed_nodes: List[str]
) -> List[str]:
    """Return changed nodes plus hard/integration descendants."""

    node_ids = {node.id for node in nodes}
    if any(node_id not in node_ids for node_id in changed_nodes):
        raise ValueError("changed node is outside the authorized DAG")
    reverse_edges = {node_id: set() for node_id in node_ids}
    for child in nodes:
        for parent_id in set(child.depends_on + child.integration_after):
            if parent_id in reverse_edges:
                reverse_edges[parent_id].add(child.id)
    affected = set(changed_nodes)
    pending = list(changed_nodes)
    while pending:
        parent_id = pending.pop()
        for child_id in sorted(reverse_edges[parent_id]):
            if child_id not in affected:
                affected.add(child_id)
                pending.append(child_id)
    return sorted(affected)


def _scoped_values(value: Any, key_name: str) -> List[str]:
    result: List[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = key.casefold().replace("-", "_")
            if normalized_key == key_name:
                if isinstance(item, str):
                    result.append(item)
                else:
                    result.extend(item)
            else:
                result.extend(_scoped_values(item, key_name))
    elif isinstance(value, list):
        for item in value:
            result.extend(_scoped_values(item, key_name))
    return result


def validate_runtime_contract(
    contract: Dict[str, Any],
    authorized_actions: Optional[Tuple[str, ...]] = None,
    authorized_files: Optional[Tuple[str, ...]] = None,
) -> Dict[str, Any]:
    """Return the normalized runtime contract after exact scope checks."""

    normalized = _normalize_contract(contract, "runtime_contract")
    if not isinstance(normalized, dict):
        raise ValueError("runtime contract must be an object")
    if authorized_actions is not None:
        allowed = set(authorized_actions)
        for key in _ACTION_KEYS:
            if any(item not in allowed for item in _scoped_values(normalized, key)):
                raise ValueError("runtime action is outside the authorized allowlist")
    if authorized_files is not None:
        allowed_files = set(authorized_files)
        if any(
            item not in allowed_files
            for item in _scoped_values(normalized, "files")
        ):
            raise ValueError("runtime file is outside the authorized scope")
    return normalized


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
    decision_digest: str,
    active_pair_limit: int,
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
        "decision_digest": decision_digest,
        "active_pair_limit": active_pair_limit,
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
    decision_digest: str
    active_pair_limit: int
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
    decision_digest: str
    active_pair_limit: int
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
            "decision_digest",
            "active_pair_limit",
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
        if (
            isinstance(converted["active_pair_limit"], bool)
            or not isinstance(converted["active_pair_limit"], int)
            or converted["active_pair_limit"] < 1
        ):
            raise ValueError("authorization active pair limit is invalid")
        return cls(**converted)


@dataclass(frozen=True)
class DeployAuthorizationRecord:
    """Separate authorization contract for a Deploy manifest.

    This intentionally is not an ``AuthorizationRecord``: ordinary plan
    authorization must continue to exclude Deploy and cannot be upgraded by
    changing an action string.
    """

    manifest_digest: str
    target: str
    allowed_actions: Tuple[str, ...]
    digest: str
    schema_version: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DeployAuthorizationRecord":
        if not isinstance(data, dict):
            raise ValueError("deploy authorization record schema is invalid")
        required = {"manifest_digest", "target", "allowed_actions", "digest", "schema_version"}
        if set(data) != required:
            raise ValueError("deploy authorization record schema is invalid")
        actions = data["allowed_actions"]
        if not isinstance(actions, (list, tuple)) or tuple(actions) != ("deploy",):
            raise ValueError("deploy authorization action scope is invalid")
        if data["schema_version"] != 1:
            raise ValueError("unsupported deploy authorization record schema")
        return cls(
            manifest_digest=str(data["manifest_digest"]),
            target=str(data["target"]),
            allowed_actions=("deploy",),
            digest=str(data["digest"]),
            schema_version=1,
        )


def build_deploy_authorization(manifest_digest: str, target: str) -> DeployAuthorizationRecord:
    """Build an unconfirmed Deploy authorization for one exact manifest."""

    if not isinstance(manifest_digest, str) or not manifest_digest.strip():
        raise ValueError("manifest digest must be non-empty")
    if not isinstance(target, str) or not target.strip():
        raise ValueError("deploy target must be non-empty")
    canonical = {
        "schema_version": 1,
        "manifest_digest": manifest_digest,
        "target": target,
        "allowed_actions": ("deploy",),
    }
    return DeployAuthorizationRecord(
        manifest_digest=manifest_digest,
        target=target,
        allowed_actions=("deploy",),
        digest=_canonical_digest(canonical),
    )


def is_deploy_authorization_valid(record: DeployAuthorizationRecord, manifest_digest: str, target: str) -> bool:
    if not isinstance(record, DeployAuthorizationRecord):
        return False
    if record.schema_version != 1 or record.allowed_actions != ("deploy",):
        return False
    if record.manifest_digest != manifest_digest or record.target != target:
        return False
    canonical = {
        "schema_version": 1,
        "manifest_digest": record.manifest_digest,
        "target": record.target,
        "allowed_actions": record.allowed_actions,
    }
    return secrets.compare_digest(record.digest, _canonical_digest(canonical))


def build_authorization_card(
    plan: Plan,
    nodes: List[DAGNode],
    capabilities: AgentCapabilities,
    active_pair_limit: Optional[int] = None,
    allowed_actions: Optional[Tuple[str, ...]] = None,
) -> AuthorizationCard:
    node_ids = tuple(sorted(node.id for node in nodes))
    if node_ids != tuple(sorted(plan.node_ids)):
        raise ValueError("authorization nodes must exactly match the plan")
    contract_digest = executable_contract_digest(nodes)
    normalized_contracts = {
        node.id: _normalize_contract(node.contract, "contract." + node.id)
        for node in nodes
    }
    file_scope = tuple(
        sorted(
            {
                path
                for contract in normalized_contracts.values()
                for path in _scoped_values(contract, "files")
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
    if active_pair_limit is None:
        active_pair_limit = max(1, len(nodes))
    if (
        isinstance(active_pair_limit, bool)
        or not isinstance(active_pair_limit, int)
        or not 1 <= active_pair_limit <= 64
    ):
        raise ValueError("active pair limit must be an integer from 1 to 64")
    if allowed_actions is None:
        allowed_actions = _ALLOWED_ACTIONS
    else:
        if not isinstance(allowed_actions, (tuple, list)) or not all(
            isinstance(action, str) for action in allowed_actions
        ):
            raise ValueError("authorization actions must be strings")
        allowed_actions = tuple(action.strip().casefold() for action in allowed_actions)
    if not _valid_action_scope(allowed_actions):
        raise ValueError("authorization action scope is invalid")
    decision_digest = _canonical_digest(
        {
            "decisions": plan.decisions,
            "evidence_priority": plan.evidence_priority,
        }
    )
    canonical = _authorization_payload(
        plan.plan_id,
        plan.version,
        node_ids,
        file_scope,
        worker_scope,
        capabilities.agent_id,
        allowed_actions,
        _EXCLUDED_ACTIONS,
        contract_digest,
        decision_digest,
        active_pair_limit,
    )
    return AuthorizationCard(digest=_canonical_digest(canonical), **canonical)


def refresh_authorization_card(
    plan: Plan,
    nodes: List[DAGNode],
    previous: AuthorizationCard,
) -> AuthorizationCard:
    """Rebuild a same-plan card while retaining its approved agent/capacity scope."""

    authorize(previous, "AUTHORIZE")
    if (
        previous.plan_id != plan.plan_id
        or previous.plan_version != plan.version
        or previous.node_ids != tuple(sorted(plan.node_ids))
    ):
        raise ValueError("reauthorization must remain on the same plan revision")
    capabilities = AgentCapabilities(
        previous.agent_id,
        False,
        False,
        False,
        False,
        False,
        "guide",
    )
    return build_authorization_card(
        plan,
        nodes,
        capabilities,
        active_pair_limit=previous.active_pair_limit,
        allowed_actions=previous.allowed_actions,
    )


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
        card.decision_digest,
        card.active_pair_limit,
    )
    if card.schema_version != AUTHORIZATION_SCHEMA_VERSION:
        raise ValueError("unsupported authorization card schema")
    if not _valid_action_scope(card.allowed_actions) or card.excluded_actions != _EXCLUDED_ACTIONS:
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
        decision_digest=card.decision_digest,
        active_pair_limit=card.active_pair_limit,
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
    decision_digest = _canonical_digest(
        {
            "decisions": plan.decisions,
            "evidence_priority": plan.evidence_priority,
        }
    )
    if not secrets.compare_digest(record.decision_digest, decision_digest):
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
    if not _valid_action_scope(record.allowed_actions) or record.excluded_actions != _EXCLUDED_ACTIONS:
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
        record.decision_digest,
        record.active_pair_limit,
    )
    if not secrets.compare_digest(record.digest, _canonical_digest(canonical)):
        return False
    return True
