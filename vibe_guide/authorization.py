"""Plan-bound, action-level authorization primitives for V2-4.

The card is a closed allowlist.  In particular, a normal ``commit`` or
``develop`` authorization never implies ``push``, ``create_mr`` or ``merge``.
Those actions may only be present when the active DAG card names them.  A
merge action additionally carries an exact Change Request scope.
"""

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any, Dict, Iterable, Mapping, Optional

DEFAULT_ACTIONS = ("accept", "commit", "develop", "review", "rework", "test")
# Kept as an explicit audit field for compatibility: these actions are never
# inferred.  ``push``, ``create_mr`` and ``merge`` can be opted in explicitly;
# ``deploy`` is always excluded from an ordinary DAG card.
EXCLUDED_ACTIONS = ("create_mr", "deploy", "merge", "push")
_ACTION_ALIASES = {"local_merge": "merge_local", "remote_merge": "merge"}
_VALID_ACTIONS = frozenset(DEFAULT_ACTIONS + ("push", "create_mr", "merge", "merge_local"))
_MERGE_ACTIONS = frozenset(("merge", "merge_local"))
_SCOPE_ALIASES = {
    "issue": "issue_id",
    "issue_id": "issue_id",
    "source": "source_sha",
    "source_sha": "source_sha",
    "target": "target_branch",
    "target_ref": "target_branch",
    "target_branch": "target_branch",
    "change_request": "change_request_id",
    "change_request_id": "change_request_id",
    "request_id": "change_request_id",
    "mr_id": "change_request_id",
    "pr_id": "change_request_id",
    "name": "change_request_id",
}


@dataclass(frozen=True)
class MergeBinding:
    """Exact scope a card may authorize for a merge action."""

    issue_id: str
    source_sha: str
    target_branch: str
    change_request_id: str

    def to_dict(self) -> Dict[str, str]:
        return asdict(self)


def _digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError("{} must be a 40-character SHA".format(field))
    value = value.strip().lower()
    if len(value) != 40 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("{} must be a 40-character SHA".format(field))
    return value


def _action(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("authorization action scope is invalid")
    normalized = value.strip().lower()
    return _ACTION_ALIASES.get(normalized, normalized)


def _normalize_scope(scope: Any) -> Optional[Dict[str, str]]:
    if scope is None:
        return None
    if isinstance(scope, MergeBinding):
        scope = scope.to_dict()
    elif hasattr(scope, "to_dict") and callable(scope.to_dict):
        scope = scope.to_dict()
    if isinstance(scope, (list, tuple)):
        if len(scope) != 1:
            raise ValueError("merge scope must identify one Change Request")
        scope = scope[0]
    if not isinstance(scope, Mapping):
        raise ValueError("merge scope must be a mapping")
    normalized: Dict[str, str] = {}
    for key, value in scope.items():
        canonical = _SCOPE_ALIASES.get(str(key).strip().lower())
        if canonical is None:
            raise ValueError("merge scope contains an unknown field")
        if canonical in normalized and normalized[canonical] != value:
            raise ValueError("merge scope contains conflicting aliases")
        if not isinstance(value, str) or not value.strip():
            raise ValueError("merge scope fields must be non-empty strings")
        normalized[canonical] = value.strip()
    required = ("issue_id", "source_sha", "target_branch", "change_request_id")
    if any(field not in normalized for field in required):
        raise ValueError("merge scope requires issue, source SHA, target branch and Change Request")
    normalized["source_sha"] = _sha(normalized["source_sha"], "source_sha")
    return {field: normalized[field] for field in required}


def _scope_from(authorization: Any) -> Optional[Dict[str, str]]:
    if isinstance(authorization, Mapping):
        scope = authorization.get("merge_scope")
    else:
        scope = getattr(authorization, "merge_scope", None)
    if scope is None:
        return None
    return _normalize_scope(scope)


def _authorized_actions(authorization: Any) -> set:
    if isinstance(authorization, Mapping):
        values = authorization.get("allowed_actions", ())
    else:
        values = getattr(authorization, "allowed_actions", ())
    if not isinstance(values, (tuple, list, set, frozenset)):
        return set()
    try:
        return {_action(value) for value in values}
    except ValueError:
        return set()


def _validate_actions(actions: Iterable[Any]) -> tuple:
    normalized = tuple(_action(action) for action in actions)
    if (
        not normalized
        or len(normalized) != len(set(normalized))
        or not set(normalized) <= _VALID_ACTIONS
        or "deploy" in normalized
    ):
        raise ValueError("authorization action scope is invalid")
    return normalized


def build_authorization_card(
    plan_id: str,
    plan_version: int,
    node_ids: Iterable[str],
    file_scope: Iterable[str],
    worker_scope: Iterable[str],
    allowed_actions: Iterable[str] = DEFAULT_ACTIONS,
    merge_scope: Optional[Mapping[str, Any]] = None,
    merge_binding: Optional[Mapping[str, Any]] = None,
    merge_bindings: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Build a digest-bound card whose executable actions are explicit.

    ``merge_scope`` is the preferred name.  The singular/plural binding
    aliases are accepted for callers using the terminology from an Issue
    contract; all are canonicalized to one scope in the card.
    """
    if merge_scope is not None and merge_binding is not None and merge_scope != merge_binding:
        raise ValueError("merge scope aliases disagree")
    if merge_scope is None:
        merge_scope = merge_binding
    if merge_scope is not None and merge_bindings is not None and merge_scope != merge_bindings:
        raise ValueError("merge scope aliases disagree")
    if merge_scope is None:
        merge_scope = merge_bindings
    actions = _validate_actions(allowed_actions)
    normalized_scope = _normalize_scope(merge_scope)
    if actions and set(actions) & _MERGE_ACTIONS and normalized_scope is None:
        raise ValueError("explicit merge authorization requires merge scope")
    payload = {
        "plan_id": plan_id,
        "plan_version": plan_version,
        "node_ids": tuple(sorted(node_ids)),
        "file_scope": tuple(sorted(file_scope)),
        "worker_scope": tuple(sorted(worker_scope)),
        "allowed_actions": actions,
        "excluded_actions": EXCLUDED_ACTIONS,
        "merge_scope": normalized_scope,
    }
    return {**payload, "digest": _digest(payload)}


def authorize(card: Mapping[str, Any], confirmation: str) -> Dict[str, Any]:
    if confirmation != "AUTHORIZE":
        raise ValueError("authorization requires exact AUTHORIZE confirmation")
    if not isinstance(card, Mapping):
        raise TypeError("authorization card must be a mapping")
    payload = {key: card[key] for key in card if key != "digest"}
    expected = dict(payload)
    expected["allowed_actions"] = _validate_actions(expected.get("allowed_actions", ()))
    expected["excluded_actions"] = tuple(expected.get("excluded_actions", ()))
    expected["merge_scope"] = _normalize_scope(expected.get("merge_scope"))
    if tuple(expected.get("allowed_actions", ())) != tuple(payload.get("allowed_actions", ())):
        raise ValueError("authorization card action scope is not canonical")
    if expected["excluded_actions"] != EXCLUDED_ACTIONS:
        raise ValueError("authorization card action scope is invalid")
    if expected["merge_scope"] != payload.get("merge_scope"):
        raise ValueError("authorization card merge scope is not canonical")
    if card.get("digest") != _digest(expected):
        raise ValueError("authorization card digest is invalid")
    if _authorized_actions(card) & _MERGE_ACTIONS and expected["merge_scope"] is None:
        raise ValueError("explicit merge authorization requires merge scope")
    return dict(card)


def is_action_authorized(
    authorization: Any,
    action: str,
    merge_scope: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return whether one exact action is present in the active card.

    No action is inferred from another action.  For merge, an optional runtime
    scope must match the scope digest-bound into the card.
    """
    try:
        normalized = _action(action)
    except ValueError:
        return False
    if normalized == "deploy" or normalized not in _authorized_actions(authorization):
        return False
    if normalized in _MERGE_ACTIONS:
        card_scope = _scope_from(authorization)
        if card_scope is None:
            return False
        if merge_scope is not None:
            try:
                return card_scope == _normalize_scope(merge_scope)
            except ValueError:
                return False
    return True


def require_action_authorized(
    authorization: Any,
    action: str,
    merge_scope: Optional[Mapping[str, Any]] = None,
) -> None:
    if not is_action_authorized(authorization, action, merge_scope):
        raise PermissionError("action is not explicitly authorized: {}".format(action))


# Descriptive aliases keep the boundary easy to discover without introducing
# a second authorization path.
action_is_authorized = is_action_authorized
require_action = require_action_authorized
is_authorized_action = is_action_authorized
require_authorized_action = require_action_authorized
