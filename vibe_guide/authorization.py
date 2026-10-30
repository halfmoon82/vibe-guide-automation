"""Plan-bound authorization with a digest of every executable node contract."""

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from pathlib import PurePosixPath
import secrets
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    INTEGRATION_REVIEW_NODE_ID,
    AgentCapabilities,
    DAGNode,
    Plan,
)


AUTHORIZATION_SCHEMA_VERSION = 2
# Baseline scope of every card.  ``commit`` is not here: the confirmed V4.5
# design makes commit/push/PR/MR/merge one group behind ``remote_git_actions``;
# ``allow`` adds the whole group (``_REMOTE_GIT_ACTIONS_SCOPE``), ``deny`` none.
_ALLOWED_ACTIONS = ("accept", "develop", "review", "rework", "test")
_LOCAL_MERGE_ACTION = "merge_local"
_EXCLUDED_ACTIONS = ("create_mr", "deploy", "merge", "push")
_HARD_EXCLUDED_ACTIONS = frozenset(("create_change_request", "deploy", "merge", "push"))
_REMOTE_GIT_ACTIONS = frozenset(("commit", "push", "pr", "mr", "create_pr", "create_mr", "merge"))
_REMOTE_GIT_ACTIONS_SCOPE = ("commit", "push", "create_pr", "create_mr", "merge")
# Legacy spellings that must still be refused under ``deny``; the producer
# never emits them, so they are not part of what ``allow`` must include.
_REMOTE_GIT_ACTION_ALIASES = ("pr", "mr")
_ACTION_KEYS = {"action", "actions", "allowed_actions", "requested_actions"}
# PR/MR and merge actions are valid only when explicitly present on a
# confirmed card.  The generic ``create_change_request``/``merge`` forms stay
# excluded to prevent an ambiguous action from widening the allowlist.
# Every action a card or executable contract may name: the baseline, the
# remote Git group (one definition, shared with the validator), and the two
# merge routes.
_RUNTIME_ACTIONS = frozenset(
    _ALLOWED_ACTIONS + _REMOTE_GIT_ACTIONS_SCOPE + (_LOCAL_MERGE_ACTION, "merge_remote")
)
_SENSITIVE_NAMES = (
    "api_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)

# ISSUE-209: two operational choices the product manager makes on the card
# instead of accepting a hard-coded constant.  Both are ordinary authorization
# fields: they are bound into the card digest, so changing either one requires a
# new publication and a new authorization.  The option tuples are the schema's
# own vocabulary (what a card may carry); ``supervisor`` decides what to do with
# the value at runtime and is the only reader of them.
#: The only place the beat cadence tiers are defined.  10 minutes while the
#: platform's push wake-up is unverified; 60 once it is verified (the beat is
#: then only a fallback behind the worker's push signal).
HEARTBEAT_INTERVAL_CHOICES = (10, 60)
#: Card defaults for the daemon options; the signed payload carries a key only when it differs.
DAEMON_DEFAULT = True
LAUNCHD_KEEPALIVE_DEFAULT = False
DEFAULT_HEARTBEAT_INTERVAL_MINUTES = 10
HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES = 60
ROTATE_HARD_CAP_CHOICES = (150000, 200000, 300000, 500000, 800000)
DEFAULT_ROTATE_HARD_CAP_TOKENS = 200000

# V4.6 ISSUE-02: structured per-node worker declarations on authorization
# cards.  Every node records its dispatch topology and the identity source of
# its worker session; the vocabulary mirrors vibe_guide.task_registry so a
# card and a task binding can be cross-checked mechanically.
WORKER_TOPOLOGY_VISIBLE_SDD = "visible-sdd"
WORKER_TOPOLOGY_DUAL_VISIBLE = "dual-visible"
WORKER_TOPOLOGY_BACKGROUND = "background"
_WORKER_TOPOLOGIES = (
    WORKER_TOPOLOGY_VISIBLE_SDD,
    WORKER_TOPOLOGY_DUAL_VISIBLE,
    WORKER_TOPOLOGY_BACKGROUND,
)
DEFAULT_WORKER_TOPOLOGY = WORKER_TOPOLOGY_DUAL_VISIBLE
#: Capability-probe ruling value as the adapter layer spells it
#: (``DISPATCH_TOPOLOGY_MATRIX`` / the manifest probe field name).  The
#: supervisor translates it into the ``visible-sdd`` topology enum value; the
#: adapter-layer name itself is never a topology value.
RULING_IN_SESSION_SDD = "in_session_sdd"
_WORKER_MODES = ("visible", "background")
_WORKER_ROLES = ("developer", "reviewer")
_WORKER_ENTRY_KEYS = frozenset(
    ("node_id", "topology", "mode", "role", "session_source", "limitations")
)
# Machine-checked downgrade disclosure for ``mode=background`` nodes: each
# category must be covered by at least one limitation entry, otherwise the
# card would silently promise full visible automation it cannot deliver.
BACKGROUND_LIMITATION_REQUIREMENTS = {
    "not_visible": ("不可见", "not visible", "non-visible", "invisible"),
    "no_direct_entry": (
        "不可直接进入",
        "cannot enter directly",
        "cannot be entered directly",
        "no direct entry",
    ),
    "rework_continuation_limited": (
        "返工续接受限",
        "rework continuation limited",
        "rework and continuation are limited",
        "rework/continuation limited",
        "limited rework",
    ),
}
# Canonical disclosure text producers may reuse verbatim; the validator only
# requires the category keywords above, so translations stay acceptable.
BACKGROUND_MODE_DISCLOSURES = (
    "不可见：background 任务不在桌面 App 中可见",
    "不可直接进入：用户不能直接进入该任务会话",
    "返工续接受限：返工与复审无法保证回到原任务会话",
)


def remote_git_actions_allowed(authorization, action):
    """Apply the V4.1 product-facing remote Git switch; deploy stays separate."""
    switch = getattr(authorization, "remote_git_actions", None)
    if switch is None and isinstance(authorization, dict):
        switch = authorization.get("remote_git_actions")
    if not isinstance(action, str) or action.casefold() == "deploy":
        return False
    normalized = action.casefold()
    if normalized not in _REMOTE_GIT_ACTIONS:
        return False
    return switch == "allow"


def validate_remote_git_permissions(remote_git_actions, allowed_actions):
    """Fail closed when the remote Git switch and action scope disagree.

    The vocabulary is the producer's ``_REMOTE_GIT_ACTIONS_SCOPE`` so a card
    built with ``allow`` is complete by construction; legacy aliases are only
    ever grounds for refusal under ``deny``.
    """
    if remote_git_actions not in {"allow", "deny"}:
        raise ValueError("remote_git_actions must be allow or deny")
    actions = set(allowed_actions or ())
    remote_required = set(_REMOTE_GIT_ACTIONS_SCOPE)
    remote_any = remote_required | set(_REMOTE_GIT_ACTION_ALIASES)
    if remote_git_actions == "allow" and not remote_required.issubset(actions):
        raise ValueError("remote_git_actions=allow requires all remote Git permissions")
    if remote_git_actions == "deny" and actions & remote_any:
        raise ValueError("remote_git_actions=deny conflicts with remote Git permissions")
    if actions & {"deploy", "production_write", "credentials", "external_communication", "release"}:
        raise ValueError("sensitive actions are always excluded")

def _is_main_session_identity(value: Any) -> bool:
    """Whether an identity string names the supervising main session.

    Case, separator (``-``/``_``/whitespace) and language variants are all
    normalized so "Main Session", "main_session", "codex main session" and
    "主会话" are recognized as the same forbidden identity.
    """
    if not isinstance(value, str):
        return False
    # Fold every non-alphanumeric/non-CJK character into a separator so
    # "main.session", "main/session" and "main:session" tokenize the same as
    # "main session"; compact forms catch camelCase ("MainSession") and
    # fused spellings ("mainthread").
    normalized = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", " ", value).strip().casefold()
    if not normalized:
        return False
    compact = normalized.replace(" ", "")
    if "主会话" in compact:
        return True
    tokens = normalized.split(" ")
    if "main" in tokens and ("session" in tokens or "thread" in tokens):
        return True
    return compact in {"main", "mainsession", "mainthread"}


def _background_limitation_gaps(limitations: Tuple[str, ...]) -> List[str]:
    covered = tuple(item.casefold() for item in limitations)
    missing = []
    for category, keywords in BACKGROUND_LIMITATION_REQUIREMENTS.items():
        if not any(
            keyword in limitation
            for limitation in covered
            for keyword in keywords
        ):
            missing.append(category)
    return missing


def _normalize_worker_entry(
    node_id: str, entry: Any, default_session_source: str = ""
) -> Dict[str, Any]:
    """Validate and complete one per-node worker declaration (fail closed)."""
    if entry is None:
        entry = {}
    if not isinstance(entry, dict):
        raise ValueError("worker entry for node " + node_id + " must be an object")
    unknown = set(entry) - _WORKER_ENTRY_KEYS
    if unknown:
        raise ValueError(
            "worker entry for node " + node_id + " has unknown keys: "
            + ", ".join(sorted(str(key) for key in unknown))
        )
    declared_node_id = entry.get("node_id")
    if declared_node_id is not None and declared_node_id != node_id:
        raise ValueError("worker entry node id does not match its declaration")
    mode = entry.get("mode")
    topology = entry.get("topology")
    if topology is None:
        topology = "background" if mode == "background" else DEFAULT_WORKER_TOPOLOGY
    if not isinstance(topology, str) or topology not in _WORKER_TOPOLOGIES:
        raise ValueError("worker topology for node " + node_id + " is invalid")
    if mode is None:
        mode = "background" if topology == "background" else "visible"
    if not isinstance(mode, str) or mode not in _WORKER_MODES:
        raise ValueError("worker mode for node " + node_id + " is invalid")
    if topology == "background" and mode != "background":
        raise ValueError("background topology requires background mode")
    if mode == "background" and topology != "background":
        raise ValueError("background mode requires background topology")
    role = entry.get("role", "developer")
    if not isinstance(role, str) or role not in _WORKER_ROLES:
        raise ValueError("worker role for node " + node_id + " is invalid")
    session_source = entry.get("session_source", default_session_source)
    if session_source is None:
        session_source = ""
    if not isinstance(session_source, str):
        raise ValueError("worker session source for node " + node_id + " is invalid")
    limitations = entry.get("limitations") or ()
    if not isinstance(limitations, (list, tuple)) or not all(
        isinstance(item, str) for item in limitations
    ):
        raise ValueError("worker limitations for node " + node_id + " are invalid")
    limitations = tuple(limitations)
    # The supervisor's own session must never be signed as a developer: that
    # would break the independent visible-task contract this schema exists
    # to enforce, so any main-session spelling fails closed.
    if role == "developer" and _is_main_session_identity(session_source):
        raise ValueError(
            "the main session cannot be authorized as a developer worker"
        )
    if mode == "background":
        missing = _background_limitation_gaps(limitations)
        if missing:
            raise ValueError(
                "background worker for node "
                + node_id
                + " lacks downgrade disclosure: "
                + ", ".join(missing)
            )
    return {
        "node_id": node_id,
        "topology": topology,
        "mode": mode,
        "role": role,
        "session_source": session_source,
        "limitations": limitations,
    }


def _normalize_workers_schema(
    workers: Any,
    node_ids: Tuple[str, ...],
    default_identities: Optional[Dict[str, str]] = None,
) -> Tuple[Dict[str, Any], ...]:
    """Validate worker declarations against the DAG and complete defaults.

    Accepts either a mapping ``{node_id: entry}`` or a sequence of entries
    carrying their own ``node_id``; the canonical form is a tuple of entries
    ordered by ``node_ids``.  The canonical form is deliberately a sequence
    keyed by an explicit ``node_id`` field rather than a node-id-keyed
    mapping: durable persistence redacts sensitive-looking mapping keys, and
    a legal node id may look exactly like one (``token-refresh``).
    """
    if workers is None:
        workers = {}
    declared: Dict[str, Any] = {}
    if isinstance(workers, dict):
        for key, entry in workers.items():
            if not isinstance(key, str):
                raise ValueError("authorization workers schema is invalid")
            declared[key] = entry
    elif isinstance(workers, (list, tuple)):
        for entry in workers:
            if not isinstance(entry, dict) or not isinstance(
                entry.get("node_id"), str
            ):
                raise ValueError("authorization workers schema is invalid")
            if entry["node_id"] in declared:
                raise ValueError(
                    "duplicate worker entry for node " + entry["node_id"]
                )
            declared[entry["node_id"]] = entry
    else:
        raise ValueError("authorization workers schema is invalid")
    unknown_nodes = sorted(key for key in declared if key not in node_ids)
    if unknown_nodes:
        raise ValueError(
            "worker entry outside the authorized DAG: " + ", ".join(unknown_nodes)
        )
    identities = default_identities or {}
    return tuple(
        _normalize_worker_entry(
            node_id, declared.get(node_id), identities.get(node_id, "")
        )
        for node_id in node_ids
    )


def _topology_summary(workers: Any) -> Dict[str, Any]:
    entries = workers.values() if isinstance(workers, dict) else workers
    entries = tuple(entries or ())
    by_topology = {topology: 0 for topology in _WORKER_TOPOLOGIES}
    background = 0
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or entry.get("topology") not in _WORKER_TOPOLOGIES
            or entry.get("mode") not in _WORKER_MODES
        ):
            raise ValueError("worker entry is invalid")
        by_topology[entry["topology"]] += 1
        if entry["mode"] == "background":
            background += 1
    return {
        "total": len(entries),
        "visible": len(entries) - background,
        "background": background,
        "by_topology": by_topology,
    }


def node_adapter_id(node: DAGNode) -> str:
    """Return the platform adapter id a node is ruled under, or "".

    A contract may name its platform either explicitly through ``adapter_id``
    or through the ``worker_profile`` block.  Both spellings are read here so
    the dispatch ruling has exactly one resolution order shared by the
    authorization card and the live dispatcher.
    """
    contract = node.contract if isinstance(node.contract, dict) else {}
    adapter_id = contract.get("adapter_id")
    if isinstance(adapter_id, str) and adapter_id.strip():
        return adapter_id.strip()
    profile = contract.get("worker_profile")
    if isinstance(profile, dict):
        worker = profile.get("worker")
        if isinstance(worker, str) and worker.strip():
            return worker.strip()
    return ""


def dispatch_topology_for_node(
    node: DAGNode,
    topology_rulings: Optional[Dict[str, str]] = None,
) -> str:
    """Single source of truth for one node's dispatch topology.

    The authorization card and the live dispatcher both rule a node through
    this function, so a card can never advertise a topology the supervisor
    would not actually dispatch.  Resolution order, all fail-closed:

    1. an authorization-bound ``dispatch_topology`` persisted in the node
       contract (stamped when the plan was materialized);
    2. ``integration-review`` is a reviewer-role closeout node by
       construction, so the single-session developer topology can never
       apply to it;
    3. the live platform ruling (``in_session_sdd`` is the manifest
       capability-probe field name / ``DISPATCH_TOPOLOGY_MATRIX`` ruling
       value at the adapter layer; this function translates it into the
       ``visible-sdd`` topology enum value at the dispatch layer);
    4. the conservative ``dual-visible`` default.  UNKNOWN evidence never
       yields ``visible-sdd``.

    Integration closeout dispatches only a reviewer.  Keep it off the
    developer-only visible-sdd path: integration acceptance must come from
    a reviewer claim, never from a synthetic developer delivery.  The
    existing topology vocabulary is retained for old authorization records;
    the worker entry's reviewer role describes its single closeout session.
    """
    contract = node.contract if isinstance(node.contract, dict) else {}
    persisted = contract.get("dispatch_topology")
    if isinstance(persisted, str) and persisted in _WORKER_TOPOLOGIES:
        if (
            getattr(node, "id", "") == INTEGRATION_REVIEW_NODE_ID
            and persisted == WORKER_TOPOLOGY_VISIBLE_SDD
        ):
            raise ValueError(
                "integration review requires reviewer dispatch, not visible-sdd"
            )
        return persisted
    if getattr(node, "id", "") == INTEGRATION_REVIEW_NODE_ID:
        return WORKER_TOPOLOGY_DUAL_VISIBLE
    ruling = (topology_rulings or {}).get(node_adapter_id(node))
    if ruling == RULING_IN_SESSION_SDD:
        return WORKER_TOPOLOGY_VISIBLE_SDD
    if ruling == WORKER_TOPOLOGY_BACKGROUND:
        return WORKER_TOPOLOGY_BACKGROUND
    return DEFAULT_WORKER_TOPOLOGY


def _rerule_workers(
    previous_workers: Any,
    nodes: List[DAGNode],
    topology_rulings: Optional[Dict[str, str]],
) -> Tuple[Dict[str, Any], ...]:
    """Re-derive every worker entry's topology from the live ruling.

    A same-run reauthorization used to carry ``previous.workers`` through
    verbatim, so the refreshed card kept advertising whatever topology the
    first card happened to record even after the platform ruling changed —
    the card then described a dispatch the supervisor would never perform.

    ``role`` and ``mode`` are re-derived together with the topology so an
    entry stays internally consistent: a ``visible-sdd`` node is one visible
    session that owns both the developer role and its in-session review, and
    ``background`` is the only topology that pairs with background mode.
    """
    by_id = {node.id: node for node in nodes}
    if isinstance(previous_workers, dict):
        entries: Any = tuple(previous_workers.values())
    else:
        entries = tuple(previous_workers or ())
    reruled: List[Dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("node_id"), str):
            raise ValueError("authorization workers schema is invalid")
        node = by_id.get(entry["node_id"])
        if node is None:
            raise ValueError(
                "worker entry outside the authorized DAG: " + entry["node_id"]
            )
        if (
            entry.get("mode") == "background"
            or entry.get("topology") == WORKER_TOPOLOGY_BACKGROUND
        ):
            # A disclosed background downgrade is part of what the user
            # approved, so it is sticky: a refresh must never silently upgrade
            # it to a visible topology the platform may not actually support.
            # The visible topologies below only *describe* dispatch, which is
            # why they are the ones re-derived from the live ruling.
            reruled.append(dict(entry))
            continue
        topology = dispatch_topology_for_node(node, topology_rulings)
        updated = dict(entry)
        updated["topology"] = topology
        updated["mode"] = (
            "background" if topology == WORKER_TOPOLOGY_BACKGROUND else "visible"
        )
        if node.id == INTEGRATION_REVIEW_NODE_ID:
            updated["role"] = "reviewer"
        elif topology == WORKER_TOPOLOGY_VISIBLE_SDD:
            updated["role"] = "developer"
        reruled.append(updated)
    return tuple(reruled)


def validate_authorization_card_consistency(card):
    data = card.to_dict() if hasattr(card, "to_dict") else dict(card)
    switch = data.get("remote_git_actions", "deny")
    validate_remote_git_permissions(switch, data.get("allowed_actions", ()))
    allowed = set(data.get("allowed_actions", ()))
    excluded = set(data.get("excluded_actions", ()))
    # ``excluded_actions`` is the worker-side envelope (workers never push or
    # merge themselves); ``allowed_actions`` is what the user authorized.  The
    # two overlap by design for the remote Git group under ``allow``, so only
    # an overlap outside that group is a contradiction.
    if (excluded & allowed) - set(_REMOTE_GIT_ACTIONS_SCOPE):
        raise ValueError("authorization card has overlapping allowed and excluded actions")
    workers = data.get("workers") or ()
    if workers:
        normalized_workers = _normalize_workers_schema(
            workers, tuple(sorted(data.get("node_ids", ()))), {}
        )
        summary = data.get("topology_summary") or {}
        if summary and summary != _topology_summary(normalized_workers):
            raise ValueError(
                "authorization topology summary does not match workers"
            )
    return True


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
        if normalized in _HARD_EXCLUDED_ACTIONS:
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


_SCOPE_CONTRACT_KEYS = frozenset(("files", "allowlist"))


def _normalize_contract(
    value: Any, path: str = "contract", *, include_scope: bool = True
) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [
            _normalize_contract(
                item, "{}[{}]".format(path, index), include_scope=include_scope
            )
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
            if not include_scope and normalized_key in _SCOPE_CONTRACT_KEYS:
                continue
            if normalized_key in _ACTION_KEYS:
                item = _normalize_action_value(value[key], item_path)
            elif normalized_key == "files":
                item = _normalize_files(value[key], item_path)
            else:
                item = _normalize_contract(
                    value[key], item_path, include_scope=include_scope
                )
            result[key] = item
        return result
    raise ValueError("executable contract must be JSON-safe at " + path)


def canonical_node_contracts(
    nodes: List[DAGNode], *, include_scope: bool = True
) -> Tuple[Dict[str, Any], ...]:
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
                "contract": _normalize_contract(
                    node.contract, include_scope=include_scope
                ),
            }
        )
    return tuple(canonical)


def executable_contract_digest(nodes: List[DAGNode]) -> str:
    # File scope is authorized separately through ``AuthorizationRecord.file_scope``.
    # A compliant scope expansion must refresh the write allowlist without
    # invalidating an unchanged acceptance contract.
    return _canonical_digest(
        {"nodes": canonical_node_contracts(nodes, include_scope=False)}
    )


def legacy_executable_contract_digest(nodes: List[DAGNode]) -> str:
    # Despite the name this is the per-node *scope* commitment (file scope
    # included), written for every new authorization into ``node_scope_digests``.
    # Only the aggregate acceptance of 5.0.7 digests was retired: runs
    # authorized before the scope-free digest must be closed on the old version.
    return _canonical_digest({"nodes": canonical_node_contracts(nodes)})


_CONTRACT_DIGEST = re.compile(r"[0-9a-f]{64}")


def executable_contract_digest_matches(nodes: List[DAGNode], digest: Any) -> bool:
    """True when ``digest`` is the current digest of ``nodes``.

    Anything but 64 lowercase hex characters is a mismatch.  The check comes
    first because ``secrets.compare_digest`` raises ``TypeError`` on
    non-ASCII text, and a corrupt persisted digest must be refused, not crash.
    """
    if not isinstance(digest, str) or not _CONTRACT_DIGEST.fullmatch(digest):
        return False
    return secrets.compare_digest(digest, executable_contract_digest(nodes))


def verified_authorized_nodes(
    encoded_nodes: Any, digests: Any, record: "AuthorizationRecord", scope_digests: Any = None
) -> Dict[str, DAGNode]:
    """Decode an epoch's DAG only when aggregate and per-node commitments bind it."""
    if not isinstance(encoded_nodes, dict) or not isinstance(digests, dict):
        return {}
    if set(encoded_nodes) != set(record.node_ids) or set(digests) != set(record.node_ids):
        return {}
    try:
        nodes = {key: DAGNode.from_dict(json.loads(value)) for key, value in encoded_nodes.items()}
        if any(node.id != key for key, node in nodes.items()):
            return {}
        if scope_digests != record.node_scope_digests:
            return {}
        if not isinstance(scope_digests, dict) or set(scope_digests) != set(nodes) or any(
            legacy_executable_contract_digest([node]) != scope_digests[key]
            for key, node in nodes.items()
        ):
            return {}
        if executable_contract_digest(list(nodes.values())) == record.node_contract_digest and all(
            executable_contract_digest([node]) == digests[key] for key, node in nodes.items()
        ):
            return nodes
    except (TypeError, ValueError, KeyError):
        pass
    return {}


def acceptance_contract_projection(node: DAGNode) -> Dict[str, Any]:
    """Remove only known routing leaves; retain scope and all unknown semantics.

    This is NOT an execution authorization digest.  It can only retain an
    acceptance after a separately verified old epoch and a new authorization.
    """
    projected = canonical_node_contracts([node])[0]
    contract = dict(projected["contract"])
    for key in ("adapter_id", "worker", "reviewer_worker", "model", "reasoning", "route_digest"):
        contract.pop(key, None)
    profile = contract.get("worker_profile")
    if isinstance(profile, dict):
        profile = dict(profile)
        for key in ("worker", "model", "reasoning", "route_digest"):
            profile.pop(key, None)
        # selection_basis/fallbacks may carry permissions or business facts;
        # unknown fields are never classified as harmless routing changes.
        contract["worker_profile"] = profile
    return dict(projected, contract=contract)


def route_only_acceptance_binds(
    node: DAGNode, previous_node: Optional[DAGNode], digest: Any
) -> bool:
    """The caller must obtain previous_node from the verified preceding epoch."""
    return (
        previous_node is not None
        and executable_contract_digest_matches([previous_node], digest)
        and acceptance_contract_projection(previous_node) == acceptance_contract_projection(node)
    )


def rebind_dispatch_contract(contract: Dict[str, Any], to_adapter: str) -> Dict[str, Any]:
    """The contract with only its platform-naming dispatch fields moved.

    ``acceptance_contract_projection`` is the one definition of which leaves
    are routing: this function may only write those, so whatever it returns
    projects identically to its input.  ``adapter_id`` always moves; a worker
    label moves only when it was literally the old platform's id (an adapter
    name used as a worker name).  Model and reasoning are never touched: what
    the new platform calls them is not derivable, and guessing would be
    inventing a route nobody verified.
    """
    old = contract.get("adapter_id")
    rebound = dict(contract)
    rebound["adapter_id"] = to_adapter
    if isinstance(old, str) and old.strip():
        for key in ("worker", "reviewer_worker"):
            if rebound.get(key) == old:
                rebound[key] = to_adapter
        profile = rebound.get("worker_profile")
        if isinstance(profile, dict) and profile.get("worker") == old:
            rebound["worker_profile"] = dict(profile, worker=to_adapter)
    return rebound


def integration_contract_projection(plan: Plan, nodes: List[DAGNode]) -> Dict[str, Any]:
    """The integration contract the card freezes, from its two sources.

    Monitor re-reads the live plan long after authorization to derive the
    acceptance references and the permanent exclusions, so it has to project
    the contract exactly the way the card did or the digest comparison would
    reject plans nobody touched.  One definition, two callers.
    """
    integration = next((node for node in nodes if node.id == "integration-review"), None)
    contract = getattr(plan, "integration_contract", {}) or (integration.contract if integration else {})
    return contract if isinstance(contract, dict) else {}


def digest_integration_contract(contract: Dict[str, Any]) -> str:
    return _canonical_digest(contract) if contract else ""


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


def _authorization_file_scope(nodes: List[DAGNode]) -> Tuple[str, ...]:
    """Return the effective write scope frozen into an authorization card."""

    normalized_contracts = {
        node.id: _normalize_contract(node.contract, "contract." + node.id)
        for node in nodes
    }
    return tuple(
        sorted(
            {
                path
                for contract in normalized_contracts.values()
                for key_name in ("files", "allowlist")
                for path in _scoped_values(contract, key_name)
            }
        )
    )


def _scope_allows_path(authorized_files: Tuple[str, ...], path: str) -> bool:
    """Return whether a file is covered by an exact or directory scope."""

    if not authorized_files and path == ".":
        return True
    return any(
        scope == "."
        or path == scope
        or path.startswith(scope.rstrip("/") + "/")
        for scope in authorized_files
    )


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
        scoped_paths = tuple(
            _scoped_values(normalized, key_name)
            for key_name in ("files", "allowlist")
        )
        paths = [item for values in scoped_paths for item in values]
        if any(
            not isinstance(item, str)
            or not _scope_allows_path(authorized_files, item)
            for item in paths
        ):
            raise ValueError("runtime file is outside the authorized scope")
    return normalized


def contract_invariant_entries(contract: Any) -> List[Tuple[int, Dict[str, Any]]]:
    """Return ``(index, invariant)`` pairs with a usable ``test_command``.

    Shared by authorization-card building (which lists the commands a run
    will execute) and the monitor's contract-test executor, so both always
    agree on what counts as an executable invariant: the contract's
    ``invariants`` must be a list, each entry a mapping whose
    ``test_command`` is a non-empty string.  Anything else is ignored,
    which keeps contracts without invariants completely behavior-neutral.
    """
    if not isinstance(contract, dict):
        return []
    invariants = contract.get("invariants")
    if not isinstance(invariants, list):
        return []
    entries = []
    for index, item in enumerate(invariants):
        if not isinstance(item, dict):
            continue
        command = item.get("test_command")
        if not isinstance(command, str) or not command.strip():
            continue
        entries.append((index, item))
    return entries


def _normalize_contract_test_commands(value: Any) -> Optional[Dict[str, Tuple[str, ...]]]:
    """Validate the ``contract_test_commands`` card/record field shape.

    ``None`` (absent legacy records) stays ``None``; anything present must
    be a dict of non-empty node id strings to non-empty string command
    sequences.  Invalid shapes are a schema error, never silently dropped.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or not all(
        isinstance(node_id, str) and node_id for node_id in value
    ):
        raise ValueError("authorization contract test commands are invalid")
    normalized: Dict[str, Tuple[str, ...]] = {}
    for node_id, commands in value.items():
        if (
            not isinstance(commands, (list, tuple))
            or not commands
            or not all(isinstance(command, str) and command.strip() for command in commands)
        ):
            raise ValueError("authorization contract test commands are invalid")
        normalized[node_id] = tuple(commands)
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
    remote_git_actions: str = "deny",
    required_workflow: Tuple[str, ...] = (),
    skipped_nodes: Tuple[str, ...] = (),
    integration_contract_digest: str = "",
    integration_node_id: str = "",
    integration_review_scope: Tuple[str, ...] = (),
    execution_engine: str = "",
    engine_mode: str = "",
    engine_evidence_ref: str = "",
    dag_revision: int = 0,
    engine_authorization_digest: str = "",
    explicit_execution_mode_override: Optional[Dict[str, Any]] = None,
    workers: Optional[Any] = None,
    topology_summary: Optional[Dict[str, Any]] = None,
    contract_test_commands: Optional[Dict[str, Any]] = None,
    heartbeat_interval_minutes: Optional[int] = None,
    rotate_hard_cap: Optional[int] = None,
    heartbeat_interval_forced: Optional[bool] = None,
    heartbeat_interval_forced_cost: Optional[str] = None,
    node_scope_digests: Optional[Dict[str, str]] = None,
    daemon: bool = True,
    launchd_keepalive: bool = False,
) -> Dict[str, Any]:
    payload = {
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
        "remote_git_actions": remote_git_actions,
        "required_workflow": tuple(required_workflow),
        "skipped_nodes": tuple(skipped_nodes),
        "integration_contract_digest": integration_contract_digest,
        "integration_node_id": integration_node_id,
        "integration_review_scope": tuple(integration_review_scope),
        "execution_engine": execution_engine,
        "engine_mode": engine_mode,
        "engine_evidence_ref": engine_evidence_ref,
        "dag_revision": dag_revision,
        # The card digest itself is the immutable authorization binding.  The
        # mirrored engine field is metadata and is intentionally excluded from
        # its own hash to avoid a circular digest.
        "engine_authorization_digest": "",
        "explicit_execution_mode_override": explicit_execution_mode_override or {},
    }
    # Workers semantics are bound into the digest whenever present.  Records
    # pre-dating ISSUE-02 carry no workers at all; keeping the keys absent for
    # empty declarations preserves their digests bit-for-bit.
    if node_scope_digests:
        payload["node_scope_digests"] = dict(node_scope_digests)
    if workers:
        payload["workers"] = workers
        payload["topology_summary"] = (
            topology_summary
            if topology_summary is not None
            else _topology_summary(workers)
        )
    # Same digest-preservation rule as workers: the commands a run will
    # execute are bound into the digest when present, and the key stays
    # absent for invariant-free plans so pre-existing digests are unchanged.
    if contract_test_commands:
        payload["contract_test_commands"] = {
            str(node_id): tuple(sorted(commands))
            for node_id, commands in sorted(contract_test_commands.items())
        }
    # ISSUE-209, same digest-preservation rule: a recorded choice is bound into
    # the digest, and the keys stay absent when no choice was recorded so every
    # authorization issued before the option existed keeps its digest bit for
    # bit.  Absent is therefore meaningful, not "the default": the runtime reads
    # it as "this run predates the option" and keeps the pre-209 behaviour.
    if heartbeat_interval_minutes is not None:
        payload["heartbeat_interval_minutes"] = heartbeat_interval_minutes
    if rotate_hard_cap is not None:
        payload["rotate_hard_cap"] = rotate_hard_cap
    # A forced 30-minute choice is part of what was approved, so it is signed;
    # absent otherwise, which keeps every earlier record digest unchanged.
    if heartbeat_interval_forced:
        payload["heartbeat_interval_forced"] = True
        payload["heartbeat_interval_forced_cost"] = heartbeat_interval_forced_cost or ""
    # Daemon / launchd keep-alive choices: signed only when they differ from the
    # defaults (daemon on, keep-alive off).  Absent therefore means "the
    # default", so every record issued before the options existed keeps its
    # digest bit for bit and reads back as daemon=on, launchd_keepalive=off.
    if daemon != DAEMON_DEFAULT:
        payload["daemon"] = daemon
    if launchd_keepalive != LAUNCHD_KEEPALIVE_DEFAULT:
        payload["launchd_keepalive"] = launchd_keepalive
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
    schema_version: int = AUTHORIZATION_SCHEMA_VERSION
    remote_git_actions: str = "deny"
    required_workflow: Tuple[str, ...] = ()
    skipped_nodes: Tuple[str, ...] = ()
    integration_contract_digest: str = ""
    integration_node_id: str = ""
    integration_review_scope: Tuple[str, ...] = ()
    execution_engine: str = ""
    engine_mode: str = ""
    engine_evidence_ref: str = ""
    dag_revision: int = 0
    engine_authorization_digest: str = ""
    explicit_execution_mode_override: Dict[str, Any] = None
    workers: Tuple[Dict[str, Any], ...] = None
    topology_summary: Dict[str, Any] = None
    contract_test_commands: Dict[str, Tuple[str, ...]] = None
    # ISSUE-209 choices.  ``None`` means the authorization predates the option
    # and the runtime keeps its pre-209 behaviour; the two ``*_options`` fields
    # are the availability the publication observed.  They are unsigned: they
    # describe this publication's environment, not what the product manager
    # approved, so they never enter the digest.
    heartbeat_interval_minutes: Optional[int] = None
    rotate_hard_cap: Optional[int] = None
    heartbeat_interval_forced: Optional[bool] = None
    heartbeat_interval_forced_cost: Optional[str] = None
    node_scope_digests: Optional[Dict[str, str]] = None
    heartbeat_interval_options: Tuple[Dict[str, Any], ...] = ()
    rotate_hard_cap_options: Tuple[Dict[str, Any], ...] = ()
    # Daemon / launchd keep-alive choices (signed when non-default, see the payload).
    daemon: bool = True
    launchd_keepalive: bool = False
    # Per-node capability tier and what the current platform resolves it to.
    # Unsigned display: the tier is derived from the signed node contract, and
    # a concrete model name must never enter the digest.
    model_tiers: Tuple[Dict[str, Any], ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        # Absent-when-empty, mirroring the payload rule: invariant-free cards
        # serialize exactly like pre-ISSUE-140 cards so digests and stored
        # records stay byte-identical.
        if result.get("contract_test_commands") is None:
            result.pop("contract_test_commands", None)
        # Keep the signed contract unchanged while making the user-facing
        # remote-action choice explicit in plan artifacts and authorization UI.
        result.update({
            "remote_git_actions_options": ["allow", "deny"],
            "remote_git_actions_scope": list(_REMOTE_GIT_ACTIONS_SCOPE),
            "deploy_authorization": "separate",
            "heartbeat_interval_options": [
                dict(item) for item in self.heartbeat_interval_options or ()
            ],
            "rotate_hard_cap_options": [
                dict(item) for item in self.rotate_hard_cap_options or ()
            ],
        })
        return _drop_unset_card_fields(result)


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
    remote_git_actions: str = "deny"
    required_workflow: Tuple[str, ...] = ()
    skipped_nodes: Tuple[str, ...] = ()
    integration_contract_digest: str = ""
    integration_node_id: str = ""
    integration_review_scope: Tuple[str, ...] = ()
    execution_engine: str = ""
    engine_mode: str = ""
    engine_evidence_ref: str = ""
    dag_revision: int = 0
    engine_authorization_digest: str = ""
    explicit_execution_mode_override: Dict[str, Any] = None
    workers: Tuple[Dict[str, Any], ...] = None
    topology_summary: Dict[str, Any] = None
    contract_test_commands: Dict[str, Tuple[str, ...]] = None
    # ISSUE-209 choices.  ``None`` means the authorization predates the option
    # and the runtime keeps its pre-209 behaviour.  Only the choice is part of
    # the record: which tiers were *available* when the card was published is
    # card metadata, not something the product manager approved, so the record
    # keeps carrying exactly the keys it carried before.
    heartbeat_interval_minutes: Optional[int] = None
    rotate_hard_cap: Optional[int] = None
    heartbeat_interval_forced: Optional[bool] = None
    heartbeat_interval_forced_cost: Optional[str] = None
    node_scope_digests: Optional[Dict[str, str]] = None
    daemon: bool = True
    launchd_keepalive: bool = False

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if result.get("contract_test_commands") is None:
            result.pop("contract_test_commands", None)
        result.update({
            "remote_git_actions_options": ["allow", "deny"],
            "remote_git_actions_scope": list(_REMOTE_GIT_ACTIONS_SCOPE),
            "deploy_authorization": "separate",
        })
        # Default choices stay out of the stored record, so a record whose
        # options were never changed is byte-identical to one issued before
        # the options existed.
        if result.get("daemon") == DAEMON_DEFAULT:
            result.pop("daemon", None)
        if result.get("launchd_keepalive") == LAUNCHD_KEEPALIVE_DEFAULT:
            result.pop("launchd_keepalive", None)
        return _drop_unset_card_fields(result)

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
        allowed = required | {"node_scope_digests", "remote_git_actions", "required_workflow", "skipped_nodes", "integration_contract_digest", "integration_node_id", "integration_review_scope", "execution_engine", "engine_mode", "engine_evidence_ref", "dag_revision", "engine_authorization_digest", "explicit_execution_mode_override", "workers", "topology_summary", "contract_test_commands", "remote_git_actions_options", "remote_git_actions_scope", "deploy_authorization", "heartbeat_interval_minutes", "rotate_hard_cap", "heartbeat_interval_forced", "heartbeat_interval_forced_cost", "heartbeat_interval_options", "rotate_hard_cap_options", "daemon", "launchd_keepalive"}
        if not isinstance(data, dict) or not required.issubset(data) or not set(data).issubset(allowed):
            raise ValueError("authorization record schema is invalid")
        if data["schema_version"] != AUTHORIZATION_SCHEMA_VERSION:
            raise ValueError("unsupported authorization record schema")
        converted = dict(data)
        for key in ("remote_git_actions_options", "remote_git_actions_scope", "deploy_authorization",
                    "heartbeat_interval_options", "rotate_hard_cap_options"):
            converted.pop(key, None)
        converted.setdefault("remote_git_actions", "deny")
        converted.setdefault("heartbeat_interval_minutes", None)
        converted.setdefault("rotate_hard_cap", None)
        converted.setdefault("heartbeat_interval_forced", None)
        converted.setdefault("heartbeat_interval_forced_cost", None)
        converted.setdefault("daemon", DAEMON_DEFAULT)
        converted.setdefault("launchd_keepalive", LAUNCHD_KEEPALIVE_DEFAULT)
        _validate_daemon_choices(converted["daemon"], converted["launchd_keepalive"])
        _validate_stored_card_choice(
            converted["heartbeat_interval_minutes"], "heartbeat interval minutes"
        )
        _validate_stored_card_choice(converted["rotate_hard_cap"], "rotation hard cap")
        if converted["remote_git_actions"] not in {"allow", "deny"}:
            raise ValueError("authorization remote git action switch is invalid")
        for key in ("required_workflow", "skipped_nodes", "integration_review_scope"):
            converted.setdefault(key, ())
            if not isinstance(converted[key], (list, tuple)) or not all(isinstance(item, str) for item in converted[key]):
                raise ValueError("authorization record workflow sequence is invalid")
            converted[key] = tuple(converted[key])
        converted.setdefault("integration_contract_digest", "")
        converted.setdefault("integration_node_id", "")
        converted.setdefault("execution_engine", "")
        converted.setdefault("engine_mode", "")
        converted.setdefault("engine_evidence_ref", "")
        converted.setdefault("dag_revision", 0)
        converted.setdefault("engine_authorization_digest", "")
        converted.setdefault("explicit_execution_mode_override", {})
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
        # V4.6 ISSUE-02 compatibility: records issued before the workers
        # schema simply lack the keys and load with empty defaults; any
        # present declaration is revalidated fail-closed, including the
        # main-session developer refusal and background disclosure check.
        converted["contract_test_commands"] = _normalize_contract_test_commands(
            converted.get("contract_test_commands")
        )
        workers = converted.get("workers") or ()
        if workers:
            converted["workers"] = _normalize_workers_schema(
                workers, converted["node_ids"], {}
            )
            summary = converted.get("topology_summary") or {}
            derived_summary = _topology_summary(converted["workers"])
            if summary and summary != derived_summary:
                raise ValueError(
                    "authorization topology summary does not match workers"
                )
            converted["topology_summary"] = derived_summary
        else:
            if converted.get("topology_summary"):
                raise ValueError(
                    "authorization topology summary requires workers"
                )
            converted["workers"] = ()
            converted["topology_summary"] = {}
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


def _validate_card_choice(value: Any, choices: Tuple[int, ...], label: str) -> Optional[int]:
    """Refuse a value the card is not allowed to carry.

    ``None`` stays legal: it means this authorization predates the option, and
    the runtime keeps its pre-option behaviour for it.  That is why the value is
    never defaulted here -- defaulting would erase the difference between "the
    product manager chose the default" and "this card never had the choice".
    """
    if value is None:
        return None
    if type(value) is not int or value not in choices:
        raise ValueError(
            label + " must be one of " + ", ".join(str(item) for item in choices)
        )
    return value


def _validate_stored_card_choice(value: Any, label: str) -> Optional[int]:
    """Accept any positive integer an earlier release may have signed.

    The offered tiers change between releases (5.1 offered 30 minutes, 5.2
    offers 60); a stored record carries the tier its card offered at the time,
    so checking it against today's tiers would make finished runs unreadable.
    New cards are still checked against the current tiers by
    ``_validate_card_choice``, and the runtime reads only current tiers.
    """
    if value is None:
        return None
    if type(value) is not int or value <= 0:
        raise ValueError(label + " must be a positive integer")
    return value


def _normalized_option_entries(
    options: Any, key: str, choices: Tuple[int, ...], label: str
) -> Tuple[Dict[str, Any], ...]:
    """Validate the availability list a publication observed.

    Every option is listed, available or not, with the reason it is not
    (product decision 2026-10-13: an unusable tier is named, never hidden), so
    the shape is fixed here instead of at each reader.
    """
    if options is None:
        return ()
    if not isinstance(options, (list, tuple)):
        raise ValueError(label + " options must be a sequence")
    entries: List[Dict[str, Any]] = []
    seen = set()
    for item in options:
        if not isinstance(item, dict):
            raise ValueError(label + " option must be an object")
        unknown = set(item) - {key, "available", "reason", "forced", "cost"}
        if unknown:
            raise ValueError(
                label + " option has unknown keys: "
                + ", ".join(sorted(str(name) for name in unknown))
            )
        value = item.get(key)
        if type(value) is not int or value not in choices:
            raise ValueError(label + " option is not an allowed value")
        if value in seen:
            raise ValueError(label + " options contain a duplicate value")
        seen.add(value)
        available = item.get("available")
        if type(available) is not bool:
            raise ValueError(label + " option availability must be a boolean")
        reason = item.get("reason") or ""
        if not isinstance(reason, str):
            raise ValueError(label + " option reason must be a string")
        if not available and not reason.strip():
            raise ValueError(label + " option is unavailable without a reason")
        entry = {key: value, "available": available, "reason": reason}
        # A forced tier is usable only by an explicit product choice and must
        # state its price on the card: a missing cost refuses publication.
        # Absent for ordinary tiers, so existing cards stay byte-identical.
        if "forced" in item or "cost" in item:
            forced = item.get("forced")
            cost = item.get("cost")
            if forced is not True or not available:
                raise ValueError(label + " option forced marker must be true on an available tier")
            if not isinstance(cost, str) or not cost.strip():
                raise ValueError(label + " forced option has no cost statement")
            entry["forced"] = True
            entry["cost"] = cost
        entries.append(entry)
    return tuple(entries)


def _require_offered(
    options: Tuple[Dict[str, Any], ...], key: str, chosen: Optional[int], label: str
) -> Optional[int]:
    """Refuse a choice the publication did not observe as available.

    A choice the product manager made from a card that named the tier
    unavailable must fail at publication rather than be silently downgraded at
    the first beat.  Callers that re-derive no availability (a same-run refresh
    carrying an already approved choice) skip this check: the runtime re-checks
    the condition and reports any fallback in its own reason.
    """
    if chosen is None:
        return None
    entry = next((item for item in options if item.get(key) == chosen), None)
    if entry is None:
        raise ValueError(label + " was not offered by this publication")
    if entry.get("available") is not True:
        raise ValueError(label + " is unavailable: " + str(entry.get("reason")))
    return chosen


def _validate_daemon_choices(daemon: Any, launchd_keepalive: Any) -> None:
    if type(daemon) is not bool or type(launchd_keepalive) is not bool:
        raise ValueError("authorization daemon choices must be booleans")
    if launchd_keepalive and not daemon:
        raise ValueError("launchd keep-alive requires the daemon option")


def _drop_unset_card_fields(result: Dict[str, Any]) -> Dict[str, Any]:
    """Serialize an unrecorded ISSUE-209 choice exactly like a pre-209 card.

    Absent-when-unset, the same rule ``contract_test_commands`` follows: a card
    or record that recorded no choice -- and observed no availability, which is
    what a pre-209 card did -- must be byte-identical to the ones issued before
    the option existed, so their stored digests keep verifying.
    """
    for key in ("node_scope_digests", "heartbeat_interval_minutes", "rotate_hard_cap",
                "heartbeat_interval_forced", "heartbeat_interval_forced_cost"):
        if result.get(key) is None:
            result.pop(key, None)
    for key in ("heartbeat_interval_options", "rotate_hard_cap_options", "model_tiers"):
        if not result.get(key):
            result.pop(key, None)
    return result


def _model_tier_rows(nodes: List[DAGNode], platform: str) -> Tuple[Dict[str, Any], ...]:
    """Each node's tier and its model on ``platform``; a failed probe is shown as unknown."""
    from . import model_catalog

    home = model_catalog.vibe_home_path()
    rows = []
    for node in sorted(nodes, key=lambda item: item.id):
        tier = model_catalog.node_tier(node)
        choice = model_catalog.resolve(platform, tier, vibe_home=home)
        rows.append({
            "node_id": node.id,
            "tier": tier,
            "reviewer_tier": model_catalog.node_tier(node, role="reviewer"),
            "platform": choice.platform,
            "resolved_model": choice.model,
            "effort": choice.effort,
            "resolution": choice.status,
            "note": choice.note,
        })
    return tuple(rows)


def build_authorization_card(
    plan: Plan,
    nodes: List[DAGNode],
    capabilities: AgentCapabilities,
    active_pair_limit: Optional[int] = None,
    allowed_actions: Optional[Tuple[str, ...]] = None,
    remote_git_actions: str = "deny",
    required_workflow: Optional[Tuple[str, ...]] = None,
    skipped_nodes: Optional[Tuple[str, ...]] = None,
    integration_contract: Optional[Dict[str, Any]] = None,
    integration_contract_digest: Optional[str] = None,
    integration_node_id: str = "",
    integration_review_scope: Optional[Tuple[str, ...]] = None,
    workflow: Optional[Dict[str, Any]] = None,
    execution_engine: str = "",
    engine_mode: str = "",
    engine_evidence_ref: str = "",
    engine_attestation: Optional[Dict[str, Any]] = None,
    explicit_execution_mode_override: Optional[Dict[str, Any]] = None,
    workers: Optional[Dict[str, Any]] = None,
    topology_rulings: Optional[Dict[str, str]] = None,
    heartbeat_interval_minutes: Optional[int] = None,
    rotate_hard_cap: Optional[int] = None,
    heartbeat_interval_forced: Optional[bool] = None,
    heartbeat_interval_forced_cost: Optional[str] = None,
    heartbeat_interval_options: Optional[Any] = None,
    rotate_hard_cap_options: Optional[Any] = None,
    enforce_choice_availability: bool = True,
    daemon: bool = DAEMON_DEFAULT,
    launchd_keepalive: bool = LAUNCHD_KEEPALIVE_DEFAULT,
) -> AuthorizationCard:
    node_ids = tuple(sorted(node.id for node in nodes))
    if node_ids != tuple(sorted(plan.node_ids)):
        raise ValueError("authorization nodes must exactly match the plan")
    from .dag import _write_scope_conflict_errors

    conflicts = _write_scope_conflict_errors(nodes)
    if conflicts:
        reasons = [
            "{}: {}".format(node_id, "; ".join(node_reasons))
            for node_id, node_reasons in sorted(conflicts.items())
        ]
        raise ValueError("blocked_dag write scope conflict: " + " | ".join(reasons))
    contract_digest = executable_contract_digest(nodes)
    file_scope = _authorization_file_scope(nodes)
    worker_scope = tuple(
        sorted(
            {
                str(node.contract["worker"])
                for node in nodes
                if node.contract.get("worker")
            }
        )
    )
    worker_identities = {}
    for node in nodes:
        raw_worker = node.contract.get("worker")
        if raw_worker is None:
            continue
        if not isinstance(raw_worker, str):
            # Fail closed: coercing a non-string identity with str() would
            # let a structured value smuggle a main-session spelling past
            # the worker identity refusal.
            raise ValueError("contract worker identity must be a string")
        worker_identities[node.id] = raw_worker
    normalized_workers = _normalize_workers_schema(
        workers, node_ids, worker_identities
    )
    normalized_workers = tuple(
        dict(entry, role="reviewer")
        if entry["node_id"] == INTEGRATION_REVIEW_NODE_ID else entry
        for entry in normalized_workers
    )
    if topology_rulings is not None:
        # The card has to describe the dispatch the supervisor will actually
        # perform from the *first* publication on, not only after a refresh.
        # ``_normalize_workers_schema`` defaults every undeclared entry to the
        # conservative topology, so a fresh card would advertise
        # ``dual-visible`` for a node the live ruling dispatches as
        # ``visible-sdd`` -- the same card/dispatch split, one publication
        # earlier.
        normalized_workers = _rerule_workers(
            normalized_workers, nodes, topology_rulings
        )
    topology_summary = _topology_summary(normalized_workers)
    if active_pair_limit is None:
        active_pair_limit = max(1, len(nodes))
    if (
        isinstance(active_pair_limit, bool)
        or not isinstance(active_pair_limit, int)
        or not 1 <= active_pair_limit <= 64
    ):
        raise ValueError("active pair limit must be an integer from 1 to 64")
    if allowed_actions is None:
        allowed_actions = tuple(dict.fromkeys(_ALLOWED_ACTIONS + ((_REMOTE_GIT_ACTIONS_SCOPE) if remote_git_actions == "allow" else ())))
    else:
        if not isinstance(allowed_actions, (tuple, list)) or not all(
            isinstance(action, str) for action in allowed_actions
        ):
            raise ValueError("authorization actions must be strings")
        allowed_actions = tuple(action.strip().casefold() for action in allowed_actions)
    if not _valid_action_scope(allowed_actions):
        raise ValueError("authorization action scope is invalid")
    if getattr(plan, "complexity_band", "") == "complex":
        if not execution_engine:
            execution_engine = "vibeguide_monitor"
        if not engine_mode:
            engine_mode = "dag"
        if engine_attestation is not None:
            from .engine_attestation import validate_engine_attestation
            validate_engine_attestation(engine_attestation, plan.plan_id, plan.version)
            attested_ref = engine_attestation["evidence_ref"]
            if engine_evidence_ref and engine_evidence_ref != attested_ref:
                raise ValueError("engine evidence reference does not match attestation")
            engine_evidence_ref = attested_ref
        if engine_attestation is None and getattr(plan, "status", "") == "confirmed_pending_authorization":
            raise ValueError("complex authorization requires verified engine attestation")
        if not engine_evidence_ref:
            engine_evidence_ref = "unverified:legacy"
        if (execution_engine != "vibeguide_monitor" or engine_mode != "dag") and not explicit_execution_mode_override:
            raise ValueError("complex plans require vibeguide_monitor DAG execution engine")
        if not isinstance(engine_evidence_ref, str) or not engine_evidence_ref.strip():
            raise ValueError("execution engine evidence reference is required")
    elif execution_engine or engine_mode or engine_evidence_ref:
        raise ValueError("execution engine binding is only valid for complex plans")
    if required_workflow is None:
        from .planner import required_workflow_nodes
        required_workflow = tuple(required_workflow_nodes(getattr(plan, "complexity_band", "")))
    else:
        required_workflow = tuple(required_workflow)
    if skipped_nodes is not None and workflow is None and skipped_nodes:
        raise ValueError("skipped nodes require workflow evidence")
    if workflow is not None and not isinstance(workflow, dict):
        raise ValueError("workflow evidence is invalid")
    if workflow is not None and getattr(plan, "complexity_band", "") == "complex":
        from .workflow_gate import verify_workflow
        verification = verify_workflow(workflow)
        if verification.get("status") != "complete":
            raise ValueError("workflow evidence is incomplete or invalid")
    workflow_records = (workflow or {}).get("node_records", {}) if workflow else {}
    derived_skips = tuple(sorted(node_id for node_id, rec in workflow_records.items() if isinstance(rec, dict) and rec.get("status") == "skipped_by_user"))
    skipped_nodes = tuple(skipped_nodes) if skipped_nodes is not None else derived_skips
    if workflow is not None and tuple(skipped_nodes) != derived_skips:
        raise ValueError("skipped nodes do not match workflow evidence")
    integration = next((node for node in nodes if node.id == "integration-review"), None)
    integration_node_id = integration_node_id or (integration.id if integration else "")
    if integration_contract is None:
        integration_contract = integration_contract_projection(plan, nodes)
    computed_integration_digest = digest_integration_contract(integration_contract)
    if integration_contract_digest is not None and integration_contract_digest != computed_integration_digest:
        raise ValueError("integration contract digest does not match projection")
    integration_contract_digest = computed_integration_digest
    if integration_review_scope is None:
        integration_review_scope = ("整合 Review", "独立 reviewer", "P0–P2 清零", "只读聚合证据") if integration else ()
    integration_review_scope = tuple(integration_review_scope)
    if getattr(plan, "complexity_band", "") == "complex":
        from .planner import REQUIRED_COMPLEX_WORKFLOW
        if required_workflow != tuple(REQUIRED_COMPLEX_WORKFLOW):
            raise ValueError("complex authorization must bind the complete required workflow")
        if any(item not in node_ids for item in skipped_nodes):
            raise ValueError("skipped node is outside the authorized DAG")
        if integration is None or integration_node_id != "integration-review":
            raise ValueError("complex authorization requires the integration review node")
        if not integration_review_scope:
            raise ValueError("integration review scope is required")
        if not integration_contract:
            raise ValueError("integration acceptance contract is required")
        plan_contract = getattr(plan, "integration_contract", {}) or {}
        if integration_contract != plan_contract:
            raise ValueError("integration contract override does not match plan projection")
    decision_digest = _canonical_digest(
        {
            "decisions": plan.decisions,
            "evidence_priority": plan.evidence_priority,
        }
    )
    # The card summary lists every invariant test_command the monitor will
    # execute for each node, so the authorization review sees the exact
    # command set being approved (ISSUE-140).  Invariant-free plans omit the
    # key entirely and keep their legacy digest.
    contract_test_commands: Dict[str, List[str]] = {}
    for node in nodes:
        entries = contract_invariant_entries(node.contract)
        if entries:
            contract_test_commands[node.id] = [
                item["test_command"] for _index, item in entries
            ]
    _validate_daemon_choices(daemon, launchd_keepalive)
    heartbeat_interval_minutes = _validate_card_choice(
        heartbeat_interval_minutes, HEARTBEAT_INTERVAL_CHOICES,
        "heartbeat interval minutes",
    )
    rotate_hard_cap = _validate_card_choice(
        rotate_hard_cap, ROTATE_HARD_CAP_CHOICES, "rotation hard cap"
    )
    heartbeat_options = _normalized_option_entries(
        heartbeat_interval_options, "minutes", HEARTBEAT_INTERVAL_CHOICES,
        "heartbeat interval",
    )
    rotate_options = _normalized_option_entries(
        rotate_hard_cap_options, "tokens", ROTATE_HARD_CAP_CHOICES,
        "rotation hard cap",
    )
    # The card marks a forced tier; choosing it writes that fact and its price
    # into the authorization itself.  Options not observed (a carry-forward)
    # keep whatever the caller passed.
    if heartbeat_interval_options is not None:
        heartbeat_interval_forced, heartbeat_interval_forced_cost = None, None
        for item in heartbeat_options:
            if item.get("forced") and item["minutes"] == heartbeat_interval_minutes:
                heartbeat_interval_forced, heartbeat_interval_forced_cost = True, item["cost"]
    # Only a freshly requested choice is checked against observed availability;
    # a carried-forward one (options not observed) is left to the runtime.
    if enforce_choice_availability and heartbeat_interval_options is not None:
        _require_offered(
            heartbeat_options, "minutes", heartbeat_interval_minutes,
            "heartbeat interval",
        )
    if enforce_choice_availability and rotate_hard_cap_options is not None:
        _require_offered(
            rotate_options, "tokens", rotate_hard_cap, "rotation hard cap"
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
        remote_git_actions,
        required_workflow,
        skipped_nodes,
        integration_contract_digest,
        integration_node_id,
        integration_review_scope,
        execution_engine,
        engine_mode,
        engine_evidence_ref,
        plan.version,
        "",
        explicit_execution_mode_override,
        workers=normalized_workers,
        topology_summary=topology_summary,
        contract_test_commands=contract_test_commands,
        heartbeat_interval_minutes=heartbeat_interval_minutes,
        rotate_hard_cap=rotate_hard_cap,
        heartbeat_interval_forced=heartbeat_interval_forced,
        heartbeat_interval_forced_cost=heartbeat_interval_forced_cost,
        node_scope_digests={node.id: legacy_executable_contract_digest([node]) for node in nodes},
        daemon=daemon,
        launchd_keepalive=launchd_keepalive,
    )
    digest = _canonical_digest(canonical)
    canonical["engine_authorization_digest"] = digest
    # Observed availability is not signed: it describes this publication's
    # environment, not the approval.  Adding it after the digest keeps both the
    # digest and the record schema unchanged for a caller that passes none.
    canonical["heartbeat_interval_options"] = heartbeat_options
    canonical["rotate_hard_cap_options"] = rotate_options
    canonical["model_tiers"] = _model_tier_rows(nodes, capabilities.agent_id)
    canonical.setdefault("daemon", daemon)
    canonical.setdefault("launchd_keepalive", launchd_keepalive)
    card = AuthorizationCard(digest=digest, **canonical)
    # The issuing path checks its own output: a card the validator would
    # refuse must not be signed in the first place (ISSUE-08).
    validate_authorization_card_consistency(card)
    return card


def refresh_authorization_card(
    plan: Plan,
    nodes: List[DAGNode],
    previous: AuthorizationCard,
    workflow: Optional[Dict[str, Any]] = None,
    topology_rulings: Optional[Dict[str, str]] = None,
    engine_attestation: Optional[Dict[str, Any]] = None,
    heartbeat_interval_options: Optional[Any] = None,
    rotate_hard_cap_options: Optional[Any] = None,
    agent_id: Optional[str] = None,
) -> AuthorizationCard:
    """Rebuild a same-plan card while retaining its approved agent/capacity scope.

    ``agent_id`` is for a platform handoff only: it names the platform the
    run moves to.  Left ``None`` the approved agent is carried forward.

    The agent/capacity envelope is retained from ``previous`` because it was
    already approved; the per-node worker topology is *not*.  It is re-derived
    from the live platform ruling through :func:`dispatch_topology_for_node`,
    so the refreshed card describes the dispatch the supervisor will actually
    perform rather than echoing the topology the first card recorded.

    ``engine_attestation`` is the other half of the same argument for the
    engine binding.  ``validate_engine_attestation`` refuses an attestation
    older than 72 hours, so a plan published more than 72 hours ago cannot be
    reauthorized at all while the refreshed card keeps naming the expired
    evidence: ``Monitor._require_record`` reads the file, finds the recorded
    reference intact but the evidence stale, and blocks before any provider
    dispatch.  Passing a freshly observed attestation re-signs the card
    against it, exactly as publication does.  Left ``None`` the previous
    reference is carried forward unchanged, which is the historical behaviour
    and still the only option for a caller that has no live observation.
    """

    authorize(previous, "AUTHORIZE")
    if (
        previous.plan_id != plan.plan_id
        or previous.plan_version != plan.version
        or previous.node_ids != tuple(sorted(plan.node_ids))
    ):
        raise ValueError("reauthorization must remain on the same plan revision")
    capabilities = AgentCapabilities(
        agent_id or previous.agent_id,
        False,
        False,
        False,
        False,
        False,
        "guide",
    )
    if getattr(plan, "complexity_band", "") == "complex" and workflow is None:
        raise ValueError("complex reauthorization requires preserved workflow evidence")
    workers = _rerule_workers(previous.workers, nodes, topology_rulings)
    return build_authorization_card(
        plan,
        nodes,
        capabilities,
        active_pair_limit=previous.active_pair_limit,
        allowed_actions=previous.allowed_actions,
        remote_git_actions=previous.remote_git_actions,
        required_workflow=previous.required_workflow,
        skipped_nodes=previous.skipped_nodes,
        integration_node_id=previous.integration_node_id,
        integration_review_scope=previous.integration_review_scope,
        workflow=workflow,
        execution_engine=previous.execution_engine,
        engine_mode=previous.engine_mode,
        # ``build_authorization_card`` refuses a reference that disagrees with
        # the attestation it was handed, so the carried-forward reference is
        # only passed when there is no fresh attestation to name instead.
        engine_evidence_ref=(
            "" if engine_attestation is not None else previous.engine_evidence_ref
        ),
        engine_attestation=engine_attestation,
        explicit_execution_mode_override=previous.explicit_execution_mode_override,
        workers=workers,
        heartbeat_interval_minutes=previous.heartbeat_interval_minutes,
        rotate_hard_cap=previous.rotate_hard_cap,
        heartbeat_interval_forced=previous.heartbeat_interval_forced,
        heartbeat_interval_forced_cost=previous.heartbeat_interval_forced_cost,
        daemon=previous.daemon,
        launchd_keepalive=previous.launchd_keepalive,
        # A caller that observed nothing keeps the availability the approved
        # card recorded: the alternative -- emitting an empty list -- would
        # erase the reason a tier was unavailable from the refreshed card.
        heartbeat_interval_options=(
            heartbeat_interval_options
            if heartbeat_interval_options is not None
            else previous.heartbeat_interval_options
        ),
        rotate_hard_cap_options=(
            rotate_hard_cap_options
            if rotate_hard_cap_options is not None
            else previous.rotate_hard_cap_options
        ),
        # A refresh re-signs an approved choice, it does not re-approve it.  If
        # the environment stopped supporting that choice, blocking here would
        # make the plan impossible to reauthorize at all; the runtime falls back
        # to the default and names the reason in its preflight, which is the
        # behaviour the PRD patch specifies.  The freshly observed availability
        # is still carried, so the reviewer sees the tier is gone.
        enforce_choice_availability=False,
    )


def is_authorization_confirmation(card: AuthorizationCard, confirmation: str) -> bool:
    """Accept the legacy confirmation or the explicit V3.9 Rev3 token."""
    if confirmation == "AUTHORIZE":
        return True
    return (
        card.plan_id == "vibe-guide-v3.9-bugfix"
        and card.plan_version == 3
        and confirmation == "AUTHORIZE_V39_REV3_SELF_HEAL_NON_DEPLOY_SCOPE"
    )


def authorize(card: AuthorizationCard, confirmation: str) -> AuthorizationRecord:
    if not is_authorization_confirmation(card, confirmation):
        raise ValueError("authorization requires the exact plan-bound confirmation")
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
        card.remote_git_actions,
        card.required_workflow, card.skipped_nodes, card.integration_contract_digest,
        card.integration_node_id, card.integration_review_scope,
        card.execution_engine,
        card.engine_mode,
        card.engine_evidence_ref,
        card.dag_revision,
        card.engine_authorization_digest,
        card.explicit_execution_mode_override,
        workers=card.workers,
        topology_summary=card.topology_summary,
        contract_test_commands=card.contract_test_commands,
        heartbeat_interval_minutes=card.heartbeat_interval_minutes,
        rotate_hard_cap=card.rotate_hard_cap,
        heartbeat_interval_forced=card.heartbeat_interval_forced,
        heartbeat_interval_forced_cost=card.heartbeat_interval_forced_cost,
        node_scope_digests=card.node_scope_digests,
        daemon=card.daemon,
        launchd_keepalive=card.launchd_keepalive,
    )
    if card.schema_version != AUTHORIZATION_SCHEMA_VERSION:
        raise ValueError("unsupported authorization card schema")
    if not _valid_action_scope(card.allowed_actions) or card.excluded_actions != _EXCLUDED_ACTIONS:
        raise ValueError("authorization action scope is invalid")
    if card.workers:
        _normalize_workers_schema(card.workers, card.node_ids, {})
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
        remote_git_actions=card.remote_git_actions,
        required_workflow=card.required_workflow,
        skipped_nodes=card.skipped_nodes,
        integration_contract_digest=card.integration_contract_digest,
        integration_node_id=card.integration_node_id,
        integration_review_scope=card.integration_review_scope,
        execution_engine=card.execution_engine,
        engine_mode=card.engine_mode,
        engine_evidence_ref=card.engine_evidence_ref,
        dag_revision=card.dag_revision,
        engine_authorization_digest=card.engine_authorization_digest,
        explicit_execution_mode_override=card.explicit_execution_mode_override,
        workers=card.workers,
        topology_summary=card.topology_summary,
        contract_test_commands=card.contract_test_commands,
        heartbeat_interval_minutes=card.heartbeat_interval_minutes,
        rotate_hard_cap=card.rotate_hard_cap,
        heartbeat_interval_forced=card.heartbeat_interval_forced,
        heartbeat_interval_forced_cost=card.heartbeat_interval_forced_cost,
        node_scope_digests=card.node_scope_digests,
        daemon=card.daemon,
        launchd_keepalive=card.launchd_keepalive,
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
            digest_matches = executable_contract_digest_matches(
                nodes, record.node_contract_digest
            )
            live_file_scope = _authorization_file_scope(nodes)
        except (TypeError, ValueError):
            return False
        if not digest_matches:
            return False
        if record.node_scope_digests is not None and record.node_scope_digests != {
            node.id: legacy_executable_contract_digest([node]) for node in nodes
        }:
            return False
        if record.file_scope != live_file_scope:
            return False
    return True


def is_authorization_integrity_valid(record: AuthorizationRecord) -> bool:
    if record.schema_version != AUTHORIZATION_SCHEMA_VERSION:
        return False
    if record.remote_git_actions not in {"allow", "deny"}:
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
        record.remote_git_actions,
        record.required_workflow, record.skipped_nodes, record.integration_contract_digest,
        record.integration_node_id, record.integration_review_scope,
        record.execution_engine, record.engine_mode, record.engine_evidence_ref,
        record.dag_revision, record.engine_authorization_digest,
        record.explicit_execution_mode_override,
        workers=record.workers,
        topology_summary=record.topology_summary,
        contract_test_commands=record.contract_test_commands,
        heartbeat_interval_minutes=record.heartbeat_interval_minutes,
        rotate_hard_cap=record.rotate_hard_cap,
        heartbeat_interval_forced=record.heartbeat_interval_forced,
        heartbeat_interval_forced_cost=record.heartbeat_interval_forced_cost,
        node_scope_digests=record.node_scope_digests,
        daemon=record.daemon,
        launchd_keepalive=record.launchd_keepalive,
    )
    if not secrets.compare_digest(record.digest, _canonical_digest(canonical)):
        # V3/V4 records pre-dating execution-engine binding remain readable
        # as legacy evidence; they never grant the new complex engine gate.
        legacy = dict(canonical)
        for key in ("execution_engine", "engine_mode", "engine_evidence_ref", "dag_revision", "engine_authorization_digest"):
            legacy.pop(key, None)
        if not secrets.compare_digest(record.digest, _canonical_digest(legacy)):
            return False
    return True


def _authorization_actions(authorization: Any) -> set:
    """Read the explicit action entries from a card/record-like object."""
    actions = getattr(authorization, "allowed_actions", None)
    if actions is None and isinstance(authorization, dict):
        actions = authorization.get("allowed_actions")
    if not isinstance(actions, (tuple, list, set, frozenset)):
        return set()
    return {str(action).strip().casefold() for action in actions if isinstance(action, str)}


def _zero_p0_p2(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    # Accept the two common evidence spellings while remaining strict about
    # all three severities being present and integer zero.
    clearance = value.get("p0_p2", value.get("clearance", value))
    return (
        isinstance(clearance, dict)
        and all(key in clearance for key in ("p0", "p1", "p2"))
        and all(type(clearance[key]) is int and clearance[key] == 0 for key in ("p0", "p1", "p2"))
    )


def _target_is_frozen_and_matching(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False
    if evidence.get("target_match") is False:
        return False
    contract = evidence.get("target_contract")
    if hasattr(contract, "to_dict"):
        contract = contract.to_dict()
    if not isinstance(contract, dict):
        return False
    if contract.get("frozen") is not True or contract.get("status") not in {None, "frozen"}:
        return False
    if contract.get("missing_fields"):
        return False
    fields = ("provider", "target_branch", "issue_type", "source_branch", "merge_method", "file_scope")
    if any(not contract.get(field) for field in fields) or not (contract.get("repository") or contract.get("project")):
        return False
    digest = evidence.get("target_digest")
    if not isinstance(digest, str) or len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest.casefold()):
        return False
    canonical = {key: contract.get(key, [] if key == "file_scope" else "") for key in ("provider", "repository", "project", "target_branch", "issue_type", "source_branch", "file_scope", "merge_method")}
    expected_digest = hashlib.sha256(json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    if not secrets.compare_digest(digest.casefold(), expected_digest):
        return False
    observed = evidence.get("observed_target_contract")
    if observed is not None:
        if not isinstance(observed, dict):
            return False
        all_fields = ("provider", "repository", "project", "target_branch", "issue_type", "source_branch", "file_scope", "merge_method")
        if any(field not in observed for field in all_fields):
            return False
        for field in all_fields:
            if observed.get(field) != contract.get(field):
                return False
    return True


def _final_review_and_bindings_ok(evidence: Any) -> bool:
    if not isinstance(evidence, dict):
        return False
    review = evidence.get("final_review", evidence.get("review"))
    if not isinstance(review, dict) or str(review.get("status", "")).casefold() not in {"accepted", "approved", "pass", "passed"}:
        return False
    reviewer_id = review.get("reviewer_id") or review.get("task_id") or evidence.get("reviewer_identity")
    if not reviewer_id:
        return False
    developer = evidence.get("developer", evidence.get("delivery"))
    if not isinstance(developer, dict) or str(developer.get("status", "")).casefold() not in {"delivered", "accepted", "complete"}:
        return False
    if evidence.get("writer_reviewer_binding") is not True and evidence.get("binding_valid") is not True:
        return False
    writer_id = developer.get("writer_id") or developer.get("task_id") or evidence.get("writer_identity")
    if not writer_id or str(writer_id) == str(reviewer_id):
        return False
    clearance = evidence.get("p0_p2", evidence.get("review", {}))
    if not isinstance(clearance, dict) or not any(key in clearance for key in ("p0", "p1", "p2")):
        clearance = evidence
    return _zero_p0_p2(clearance)


def can_create_change_request(evidence: Any, authorization: Any) -> bool:
    """Whether a PR/MR creation is explicitly authorized and evidence-bound."""
    if hasattr(evidence, "to_dict"):
        evidence = evidence.to_dict()
    if not isinstance(evidence, dict):
        return False
    action = str(evidence.get("action", "")).strip().casefold()
    switch = getattr(authorization, "remote_git_actions", None)
    if switch is None and isinstance(authorization, dict):
        switch = authorization.get("remote_git_actions")
    if switch is not None and not remote_git_actions_allowed(authorization, action):
        return False
    if action not in {"create_pr", "create_mr"} or action not in _authorization_actions(authorization):
        return False
    contract = evidence.get("target_contract")
    if hasattr(contract, "to_dict"):
        contract = contract.to_dict()
    if not isinstance(contract, dict):
        return False
    if action == "create_pr" and str(contract.get("issue_type", "")).casefold() not in {"pr", "pull_request"}:
        return False
    if action == "create_mr" and str(contract.get("issue_type", "")).casefold() not in {"mr", "merge_request"}:
        return False
    return _final_review_and_bindings_ok(evidence) and _target_is_frozen_and_matching(evidence)


def can_auto_merge(evidence: Any, authorization: Any) -> bool:
    """Whether a local or verified-remote merge may be attempted."""
    if hasattr(evidence, "to_dict"):
        evidence = evidence.to_dict()
    if not isinstance(evidence, dict):
        return False
    action = str(evidence.get("action", "")).strip().casefold()
    if action == "merge_remote":
        switch = getattr(authorization, "remote_git_actions", None)
        if switch is None and isinstance(authorization, dict):
            switch = authorization.get("remote_git_actions")
        if switch is not None and not remote_git_actions_allowed(authorization, "merge"):
            return False
    if action not in {"merge_local", "merge_remote"} or action not in _authorization_actions(authorization):
        return False
    if not (_final_review_and_bindings_ok(evidence) and _target_is_frozen_and_matching(evidence)):
        return False
    contract = evidence.get("target_contract")
    if hasattr(contract, "to_dict"):
        contract = contract.to_dict()
    if not isinstance(contract, dict) or str(contract.get("issue_type", "")).casefold() not in {"pr", "pull_request", "mr", "merge_request"}:
        return False
    if action == "merge_remote":
        capability = str(evidence.get("merge_capability", "")).casefold()
        return evidence.get("provider_verified") is True and capability in {"verified_remote", "verified"}
    return True
