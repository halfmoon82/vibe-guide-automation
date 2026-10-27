"""Versioned, recoverable state, event, lock, and writer-lease persistence."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import errno
from ._wincompat import flock_exclusive, flock_release, IS_WINDOWS as _IS_WINDOWS
import hashlib
import copy
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from typing import Any, Dict, Iterator, List, Optional
import uuid

from .authorization import (
    AuthorizationRecord,
    affected_node_closure,
    executable_contract_digest,
    executable_contract_digest_matches,
    is_authorization_integrity_valid,
    legacy_executable_contract_digest,
    verified_authorized_nodes,
    route_only_acceptance_binds,
)
from .contracts import RunEvent
from .models import (
    BindingIntent,
    BindingObservation,
    BindingVerification,
    DAGNode,
    _PROVENANCE_TOKEN,
    SupervisorLeaseObservation,
    WaitThreadsCursorObservation,
)
from .binding_contract import BindingIntent as V44BindingIntent, BindingProof as V44BindingProof
from .paths import ProjectPaths


_SCOPE_KEYS = frozenset(("files", "allowlist"))


def _strip_scope_increment(value: Any, increment: frozenset) -> Any:
    if isinstance(value, dict):
        return {
            key: (
                [item for item in item_value if item not in increment]
                if isinstance(key, str)
                and key.casefold().replace("-", "_") in _SCOPE_KEYS
                and isinstance(item_value, list)
                else _strip_scope_increment(item_value, increment)
            )
            for key, item_value in value.items()
        }
    if isinstance(value, list):
        return [_strip_scope_increment(item, increment) for item in value]
    return value


def scope_only_expansion_binds(
    node: DAGNode,
    previous_digest: Any,
    previous_scope: Any,
    new_scope: Any,
) -> bool:
    """Whether ``previous_digest`` proves ``node`` changed only by file scope.

    5.0.7 digests bound file scope, and the snapshot keeps no copy of the old
    scope, so the old contract cannot be recomputed.  Instead the union
    increment (files authorized now but not before) is removed from the new
    contract's ``files``/``allowlist`` and the result is digested with the old
    algorithm: a match proves nothing else differs.  Known ceiling: when one
    node adds a file and another drops that same file the union is unchanged,
    the increment is empty, nothing can be proven, and the node is redispatched.
    Anything unprovable answers False (redispatch), never a false retain.
    """
    if not isinstance(previous_digest, str) or not _DIGEST.fullmatch(previous_digest):
        return False
    if isinstance(new_scope, str) or isinstance(previous_scope, str):
        return False
    try:
        increment = frozenset(new_scope) - frozenset(previous_scope)
    except TypeError:
        return False
    contract = getattr(node, "contract", None)
    if not increment or not isinstance(contract, dict) or not contract:
        return False
    try:
        stripped = copy.copy(node)
        stripped.contract = _strip_scope_increment(copy.deepcopy(contract), increment)
        return legacy_executable_contract_digest([stripped]) == previous_digest
    except (TypeError, ValueError):
        return False


def _previous_digest_binds(
    node: DAGNode,
    digest: Any,
    previous: AuthorizationRecord,
    replacement: AuthorizationRecord,
    previous_node: Optional[DAGNode] = None,
) -> bool:
    """Unchanged contract, or a 5.0.7 digest whose only delta is file scope.

    Must stay isomorphic with ``Monitor.reauthorize``: replay rejects any
    reauthorization whose recorded disposition this predicate disagrees with.
    """
    return route_only_acceptance_binds(node, previous_node, digest) or executable_contract_digest_matches([node], digest) or (
        scope_only_expansion_binds(
            node, digest, previous.file_scope, replacement.file_scope
        )
    )


STATE_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1
LEASE_SCHEMA_VERSION = 1
_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RUN_STATUSES = {
    "initialized",
    "running",
    "complete",
    "blocked_unknown",
    "blocked_design",
    "failed",
}
_NODE_STATUSES = {
    "planned",
    "retry_pending",
    "start_pending",
    "running",
    "delivered",
    "review",
    "rework",
    "accepted",
    "blocked_unknown",
    "blocked_design",
    "failed",
    "stopped",
    "brief_pending",
    "skipped_by_user",
    "blocked_by_required_node",
}


def verify_required_workflow_state(workflow):
    """Validate a task workflow snapshot using the monitor's hard gate."""
    from .workflow_gate import verify_workflow
    if not isinstance(workflow, dict) or not isinstance(workflow.get("task_id"), str):
        raise ValueError("workflow state is invalid")
    result = verify_workflow(workflow)
    if result.get("status") == "complete":
        return result
    return result

# V3.9 deliberately keeps the durable/internal node statuses precise while
# exposing a small, stable vocabulary to end users.  This mapping is pure so
# callers (CLI, desktop bridges and tests) can derive the same label without
# mutating the snapshot or interpreting provider details themselves.
USER_VISIBLE_STATES = ("准备中", "自动修复中", "已启动", "需要你决定", "需要监工建工作树", "需要监工排查")
#: A node that would read 自动修复中 but whose progress fingerprint has not moved
#: for several beats is not self-healing: the supervisor has to investigate
#: (``supervisor_investigate``) or hand the node to the product manager
#: (``escalate_to_pm``).  The tier is a transient display marker (``stall_tier``)
#: injected by output code from the stall record; it is never persisted.
STALL_INVESTIGATE_STATUS = "需要监工排查"
STALL_TIER_KEY = "stall_tier"
_STALL_TIER_STATUS = {
    "supervisor_investigate": STALL_INVESTIGATE_STATUS,
    "escalate_to_pm": "需要你决定",
}
_USER_DECISION_STATUSES = {"blocked_design", "blocked_deploy"}
_USER_PREPARING_STATUSES = {"initialized", "planned", "brief_pending", "start_pending"}
_USER_ACTIVE_STATUSES = {
    "running", "review", "rework", "delivered", "accepted", "complete",
}
_USER_RECOVERY_STATUSES = {
    "retry_pending", "binding_probe_pending", "binding_repair_pending",
    "binding_repairing", "blocked_unknown", "unknown", "timeout", "failed", "stopped",
}

#: Node marker the supervisor persists when a dispatch was blocked by a node
#: worktree it must not build itself (#235).  ``reason_code`` is a fixed enum
#: -- the same vocabulary ``adapters.claude_bg.WORKTREE_REASON_CODES`` uses --
#: so the label can name the required action without reading provider text.
WORKTREE_PREFLIGHT_KEY = "worktree_preflight"
WORKTREE_REASON_CODES = ("worktree_missing", "worktree_mismatch", "worktree_create_failed")
WORKTREE_ACTION_STATUS = "需要监工建工作树"
#: The marker's own keys, in one place because two writers need them: the CLI
#: records what the launcher left behind (#235), and the monitor's replay of
#: that event rebuilds the same marker after a crash between the event append
#: and the snapshot save.  Two copies of this list is how a replay silently
#: drops a field the live path wrote.
WORKTREE_PREFLIGHT_KEYS = (
    "reason_code",
    "worktree",
    "branch",
    "worktree_base",
    "worktree_command",
)


def worktree_reason_code(record: Any) -> Optional[str]:
    """The persisted worktree-blocking code of a node record, or None."""
    if not isinstance(record, dict):
        return None
    marker = record.get(WORKTREE_PREFLIGHT_KEY)
    code = marker.get("reason_code") if isinstance(marker, dict) else None
    return code if code in WORKTREE_REASON_CODES else None


def worktree_action(record: Any) -> Optional[str]:
    """The concrete command a supervisor has to run for this node, or None."""
    if not isinstance(record, dict):
        return None
    marker = record.get(WORKTREE_PREFLIGHT_KEY)
    if not isinstance(marker, dict):
        return None
    command = marker.get("worktree_command")
    return command if isinstance(command, str) and command.strip() else None


def map_user_status(status: Any, reason: Any = "") -> str:
    """User-facing status, with a stalled auto-repair node relabelled.

    Only a node that would otherwise read ``自动修复中`` is relabelled by its
    ``stall_tier`` marker; every other label keeps its own authority.
    """
    label = _derived_user_status(status, reason)
    if isinstance(status, dict) and label == "自动修复中":
        return _STALL_TIER_STATUS.get(status.get(STALL_TIER_KEY), label)
    return label


def _derived_user_status(status: Any, reason: Any = "") -> str:
    """Map internal V3.9 state to one of the user-facing statuses.

    A mapping/dict is accepted as a convenience for snapshot node records;
    technical fields remain untouched and reason text is not interpreted as a
    decision boundary.  Unknown states fail safe to the recoverable
    ``自动修复中`` label.  ``需要监工建工作树`` is the one label that is not
    derived from a status enum: it names an action only a supervisor can
    take (#235), so it is keyed on the persisted worktree marker.
    """
    if isinstance(status, dict):
        record = status
        # #235: nothing in vibe builds a missing worktree on a retry, so a
        # node blocked on one is not self-healing and must not read as
        # 自动修复中.  Checked before the binding/retry branches (a blocked
        # dispatch carries both markers), and only while no session is bound
        # and the node has not moved on: a delivered or accepted node keeps
        # its real status even if an old marker is still in its record.
        if (
            not record.get("active_task")
            and record.get("status") not in _USER_ACTIVE_STATUSES
            # #235: a decision-state node must read 需要你决定 even if an
            # old worktree marker survives in its record; the supervisor
            # cannot act on a worktree while the user still owes a decision.
            and record.get("status") not in _USER_DECISION_STATUSES
            and worktree_reason_code(record)
        ):
            return WORKTREE_ACTION_STATUS
        if record.get("binding_phase") in _USER_RECOVERY_STATUSES:
            return "自动修复中"
        # A running node with no active handle but a durable retry marker is
        # not business-started yet; it is the internal recovery phase.
        if isinstance(record.get("retryable_action"), dict) and not record.get("active_task"):
            return "自动修复中"
        status = record.get("status")
    value = str(status or "")
    if value in _USER_DECISION_STATUSES:
        return "需要你决定"
    if value in _USER_PREPARING_STATUSES:
        return "准备中"
    if value in _USER_ACTIVE_STATUSES:
        return "已启动"
    if value in _USER_RECOVERY_STATUSES:
        return "自动修复中"
    return "自动修复中"


# Descriptive alias used by UI integrations.
user_visible_status = map_user_status
_PROVENANCE_KEYS = {
    "role",
    "task_id",
    "handle_id",
    "generation",
    "authorization_digest",
    "node_contract_digest",
}
CONSISTENCY_CORRECTION_KEYS = (
    "field",
    "value",
    "source",
    "action",
    "files",
    "consistency_binding",
    "decision",
)
_EVENT_DATA_KEYS = {
    # Fixed-enum cause (and repo node id) of a decision-less blocked_design,
    # so the monitor can register vibe-authored decision text after a replay.
    "blocked_design_cause",
    "blocked_design_target",
    "blocked_design_files",
    # decision_answer_applied: which consumed answers moved the node, and the
    # fixed-enum transition they selected.  Replay needs both to land on the
    # same state after a restart.
    "answer_action",
    "decision_answers",
    # Notification bridge outcome (accepted/unknown/disabled) of a decision the
    # monitor registered itself; kept as evidence for the PM-visibility chain.
    "notification_status",
    *CONSISTENCY_CORRECTION_KEYS,
    "authorization_digest",
    "authorization_epoch",
    "superseded_creates",
    "capability_contract_digest",
    "checkpoint_sha",
    "last_event_seq",
    "limit_tokens",
    "ratio",
    "source",
    "total_tokens",
    "authorized_node_contracts",
    "accepted_nodes",
    "affected_nodes",
    "changed_nodes",
    "change_reason",
    "contract_digest",
    "continuation",
    "evidence",
    "finding",
    "generation",
    "handle_id",
    "in_contract",
    "intent_id",
    "node_contract_digest",
    "node_scope_digests",
    "node_contract_digests",
    "node_id",
    "node_ids",
    "new_authorization",
    "phase",
    "predecessor_task_id",
    "proof",
    "previous_authorization",
    "previous_authorization_digest",
    "previous_capability_contract_digest",
    "previous_node_contract_digest",
    "previous_node_contract_digests",
    # Same-run reauthorization is where an edited PRD/Spec is re-bound, so the
    # old -> new source digests have to survive event sanitization: replaying
    # the transition without them would silently drop the re-pin.
    "previous_prd_digest",
    "previous_spec_digest",
    "prd_digest",
    "spec_digest",
    "reason",
    # node_repaired (#278): the repair class ("stuck_session" /
    # "reviewer_replacement" / failure classification kind), the action it
    # queued, and whether identity survived.  All are program constants or
    # booleans; ``reconciliation`` is the structured git/provider probe block
    # the repair already computes from repo-local facts.
    "kind",
    "repair_action",
    "preserved_identity",
    "reconciliation",
    "same_task",
    # The binding status observed before a stuck_session repair normalizes it
    # to failed; without it the fold would erase why the writer was dead.
    "previous_binding_status",
    "retained_acceptances",
    "invalidated_acceptances",
    "role",
    "run_id",
    "retryable_same_task",
    "recoverable",
    "same_task_required",
    # rejection_code / rejection_count (#254/#255): the machine-readable
    # acceptance-format rejection code (built only from program constants --
    # error class plus claim key names / expected types) and the consecutive
    # rejection count for the same session.  Both live on non-provider-text
    # keys on purpose: the free-text `reason` next to them is redacted on
    # write, and without the code an auditor could not tell a wrong key set
    # from a wrong type from an empty value, nor see the replay loop the
    # count makes visible.
    "rejection_code",
    "rejection_count",
    # worktree preflight (#235): what the background launcher had to do about
    # a node worktree before it could dispatch.  ``reason_code`` is a fixed
    # enum, ``worktree``/``branch``/``worktree_base`` are paths and git
    # identifiers, and ``worktree_command`` is a command vibe itself
    # composed -- none of it is provider text, so all of it survives
    # sanitization on purpose: the whole defect was that the one readable
    # fact ("this directory is not there") was replaced by a placeholder.
    "reason_code",
    "worktree",
    "branch",
    "worktree_base",
    "worktree_command",
    # session-root delivery warning (#240): the one observation that can see
    # which sandbox a worker actually ran in.  It is advisory structure vibe
    # itself composed from ``delivery_evidence.session_cwd``, never provider
    # text, so it survives sanitization on purpose: the whole defect was that
    # the warning reached ``RunEvent.data`` and then vanished at the event
    # boundary, leaving the supervisor with a delivery that looked normal.
    "warnings",
    "attempt",
    "reason_class",
    # #277: the exception class behind a block (e.g. "ConnectionError").
    # Program-authored type name, never provider text; without it the
    # blocked_unknown event loses half of the structured classification the
    # issue requires persistence to keep.
    "error_type",
    "next_retry_at",
    "binding_digest",
    "last_observation_ref",
    "cursor",
    "setup_identity",
    "retryable_action",
    "successor_created",
    "status",
    "successor",
    "task_id",
    "worker",
    "observation_kind",
    "disposition",
    "action",
    "isolated",
    "repaired",
    "binding",
    "legacy_evidence_digest",
    # Execution-topology observation fields.  _validate_execution_topology()
    # compares the persisted `execution_topology_observed` event field by field
    # against the live snapshot, so omitting them here silently dropped them on
    # write and made that comparison fail as "topology evidence drift" for every
    # complex plan.  All six are structural counters/identifiers, never provider
    # text or secrets.
    "plan_revision",
    "started_nodes",
    "active_concurrency",
    "capacity",
    "parallel_groups",
    "monitor_entry_evidence",
    "previous_monitor_entry_evidence",
    # visible-sdd accepted event fields emitted by _accept_visible_sdd_delivery.
    # Absent from this set they are silently dropped by _sanitize_event_data,
    # leaving the persisted event incomplete for audit replay and future
    # VisibleSddAcceptance.from_dict verification.  All three are structural
    # identifiers or integer severity counters; none carries provider text or
    # secrets (protocol_ref and evidence_ref are exempted by _is_sensitive_data_key
    # already; clearance is {"p0": 0, "p1": 0, "p2": 0}).
    "protocol_ref",
    "evidence_ref",
    "clearance",
    # scope_auto_expanded: [{"path", "rule"}] for each file the supervisor
    # pulled into a node's scope on its own ("tests_dir" / "auto_scope_paths").
    # Repository-relative paths and fixed rule names only.
    "scope_rules",
    "trigger",
    "acceptance_examples_unchanged",
    "scope_reason",
    "scope_transition",
    # orphan_dispatch_adopted: the provider action, dispatch baseline and
    # old -> new lease intent that prove which session was re-bound.  Digests,
    # action ids, a git SHA and a host name only; no provider text.
    "action_id",
    "request_digest",
    "request_authorization_digest",
    "intent_digest",
    "previous_intent_digest",
    "dispatch_head",
    "host",
    # decision_raised events lost decision_ids on write, so a replayed decision
    # node looked decision-less and was misrouted as a lifecycle defect.  The
    # rest record routing markers (fixed enums / booleans), never provider text.
    "decision_ids",
    "old_task_reconciled",
    "owner_layer",
    "recovery_action",
    "user_interrupt",
    # self_heal_attempt: one supervisor re-check of a refused provider receipt.
    # ``operation``/``outcome``/``tier`` are fixed enums, ``differences`` are the
    # reader's own constant messages (field names and type names, never receipt
    # values), ``unchanged_beats`` is a counter.
    "operation",
    "outcome",
    "differences",
    "tier",
    "unchanged_beats",
}
_SENSITIVE_DATA_NAMES = (
    "api_key",
    "credential",
    "password",
    "private_key",
    "secret",
    "token",
)
_PROVIDER_TEXT_KEYS = {
    "detail",
    "error",
    "evidence",
    "exception",
    "finding",
    "message",
    "output",
    "reason",
    "scope_reason",
    "stderr",
    "stdout",
    "traceback",
}
_REDACTED = "[REDACTED]"
_REDACTED_PROVIDER_TEXT = "[REDACTED_PROVIDER_TEXT]"
#: #277: structured error-classification fields are vibe/provider-authored
#: fixed enums and type names, never free text, so they are exempted from
#: provider-text redaction even when they sit inside an otherwise-redacted
#: provider payload.  The defect was that a failure dict like
#: ``{"reason": <text>, "reason_class": "binding_unknown"}`` persisted as
#: ``{"reason": "[REDACTED_PROVIDER_TEXT]", "reason_class":
#: "[REDACTED_PROVIDER_TEXT]"}`` -- the one fact a supervisor or auditor can
#: act on ("which error class") was replaced by a placeholder.
STRUCTURED_REASON_KEYS = frozenset({
    "error_type",
    "reason_class",
    "reason_code",
})
#: Node-level structural facts a recovery or rollback gate is allowed to
#: decide on (#256).  Every entry is program-authored structure -- a status
#: enum, a hex digest, an epoch, an identity/generation pair, a marker object
#: -- so persistence keeps it verbatim.  Provider text is deliberately absent:
#: the free-text ``reason`` that sits next to these keys is redacted on write,
#: so a gate that compares it can only ever compare against
#: ``[REDACTED_PROVIDER_TEXT]`` and silently miss (the #249/#256 failure).
#: This set and ``_PROVIDER_TEXT_KEYS`` are disjoint by contract
#: (tests/test_recovery_fidelity_256.py pins it): a key in both would make
#: every recovery comparison read a placeholder.
#:
#: Binding status / identity / generation are not here because bindings live
#: in the task registry, which persists them unredacted; gates still copy them
#: into a read-only snapshot before mutating (``monitor.BindingFacts``).
RECOVERY_SIGNATURE_KEYS = frozenset({
    "acceptance",
    "active_role",
    "active_task",
    "contract_digest",
    "developer_generation",
    "developer_identity",
    "handle_id",
    "pair_archived",
    "quarantine",
    "recovery_action",
    "review_clearance",
    "review_generation",
    "reviewer_identity",
    "retryable_action",
    "start_intent",
    "status",
})
# Keys of these maps are identifiers — node ids, "<node>:<role>" task keys,
# parallel-group names — not field names, so they are never candidate secret
# names.  Node ids come straight from the product spec and only have to match
# models._ID, so `token-refresh` or `secret-rotation` is legal; judging them by
# substring replaced the whole node object with the string "[REDACTED]" and
# _validate_snapshot then rejected every later read, leaving the run
# permanently unloadable from its first save.  The exemption is exactly one
# level deep: inside an identifier's value, field names are candidate secret
# names again, so a real `token` field in a node is still redacted.
_IDENTIFIER_KEYED_FIELDS = frozenset({
    "authorized_node_contracts",
    "continuation",
    "handles",
    "invalidated_acceptances",
    "node_scope_digests",
    "node_contract_digests",
    "nodes",
    "parallel_groups",
    "previous_node_contract_digests",
    "retained_acceptances",
    "tasks",
})


def _normalize_data_key(key: str) -> str:
    return key.strip().casefold().replace("-", "_")


def _is_sensitive_data_key(key: str) -> bool:
    normalized = _normalize_data_key(key)
    if normalized.endswith("_digest") or normalized.endswith("_ref"):
        return False
    return any(name in normalized for name in _SENSITIVE_DATA_NAMES)


def is_redacted_value(value: Any) -> bool:
    """Whether persistence replaced ``value`` with one of its placeholders.

    A recovery gate that reads such a value is deciding on a placeholder, not
    on a fact; the #256 contract is that such a gate fails closed instead of
    comparing (see ``RECOVERY_SIGNATURE_KEYS``).
    """
    return isinstance(value, str) and value in (
        _REDACTED,
        _REDACTED_PROVIDER_TEXT,
    )


def _provider_text_verbose() -> bool:
    """#277 opt-in debug switch: ``VIBE_VERBOSE=1`` keeps provider free text.

    Off by default.  Field-name-based secret masking (``token``/``secret``/
    ``credential``/...) stays on even under verbose -- the flag is for
    debugging a live failure, not for persisting credentials.
    """
    return os.environ.get("VIBE_VERBOSE") == "1"


def redact_provider_text(value: Any) -> Any:
    if _provider_text_verbose():
        # Verbose keeps the original text verbatim; routing through the normal
        # sanitizer still masks dict entries whose *keys* name secrets and
        # keeps nested provider-text keys treated uniformly.
        return _sanitize_durable_value(value)
    if value is None:
        return None
    if isinstance(value, list):
        return [redact_provider_text(item) for item in value]
    if isinstance(value, tuple):
        return [redact_provider_text(item) for item in value]
    if isinstance(value, dict):
        result: Dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if _is_sensitive_data_key(name):
                continue
            if _normalize_data_key(name) in STRUCTURED_REASON_KEYS and isinstance(
                item, (str, int, float, bool)
            ):
                # Fixed-enum/type-name fields (#277).  Scalars only: a dict or
                # list under a structured name is still provider payload.
                result[name] = item
                continue
            result[name] = redact_provider_text(item)
        return result
    return _REDACTED_PROVIDER_TEXT


def _sanitize_durable_value(
    value: Any, key: Optional[str] = None, key_is_identifier: bool = False
) -> Any:
    if key is not None and not key_is_identifier:
        if _is_sensitive_data_key(key):
            return _REDACTED
        if _normalize_data_key(key) in _PROVIDER_TEXT_KEYS:
            return redact_provider_text(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [_sanitize_durable_value(item) for item in value]
    if isinstance(value, dict):
        identifier_keyed = (
            key is not None
            and not key_is_identifier
            and _normalize_data_key(key) in _IDENTIFIER_KEYED_FIELDS
        )
        return {
            str(item_key): _sanitize_durable_value(
                item_value, str(item_key), identifier_keyed
            )
            for item_key, item_value in value.items()
        }
    return _REDACTED


def durable_projection(value: Any) -> Any:
    """What persistence will keep of ``value``.

    Callers that must agree with what a later read validates -- the run-level
    integration review package is the one that must -- can only check that by
    applying the same redaction `save_snapshot` applies.
    """
    return _sanitize_durable_value(value)


def _sanitize_event_data(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError("event data is invalid")
    result: Dict[str, Any] = {}
    for key, value in data.items():
        if not isinstance(key, str):
            continue
        normalized = _normalize_data_key(key)
        if normalized not in _EVENT_DATA_KEYS or _is_sensitive_data_key(key):
            continue
        result[normalized] = _sanitize_durable_value(value, normalized)
    return result


@dataclass
class RunSnapshot:
    run_id: str
    plan_id: str
    plan_version: int
    status: str
    nodes: Dict[str, Dict[str, Any]]
    handles: Dict[str, str]
    tasks: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    authorization: Dict[str, Any] = field(default_factory=dict)
    authorization_digest: str = ""
    node_contract_digest: str = ""
    # A V2 run is bound to the exact capability evidence contract used when
    # its child tasks were dispatched.  Empty keeps pre-contract snapshots
    # loadable for legacy, non-V2 runs; V2 monitor paths always populate it.
    capability_contract_digest: str = ""
    event_sequence: int = 0
    schema_version: int = STATE_SCHEMA_VERSION
    # Optional V3.9 binding evidence.  Empty values preserve legacy snapshots.
    binding_intent: Optional[Dict[str, Any]] = None
    binding_observation: Optional[Dict[str, Any]] = None
    binding_state: str = "blocked_unknown"
    business_write_allowed: bool = False
    # V4.1 aggregate integration-review evidence. Optional for historical runs.
    integration_review_evidence: Dict[str, Any] = field(default_factory=dict)
    workflow: Optional[Dict[str, Any]] = None
    prd_digest: str = ""
    spec_digest: str = ""
    legacy_run: bool = False
    legacy_evidence: Optional[Dict[str, Any]] = None
    legacy_evidence_digest: str = ""
    execution_engine: str = ""
    engine_mode: str = ""
    engine_evidence_ref: str = ""
    dag_revision: int = 0
    ready_set: List[str] = field(default_factory=list)
    topology_digest: str = ""
    started_nodes: List[str] = field(default_factory=list)
    active_concurrency: int = 0
    capacity: int = 0
    monitor_entry_evidence: str = ""
    parallel_groups: Dict[str, List[str]] = field(default_factory=dict)
    # V4.4 two-phase binding is additive; legacy binding_intent semantics are
    # intentionally left unchanged for old snapshots and replay.
    v44_binding_intent: Optional[Dict[str, Any]] = None
    v44_binding_proof: Optional[Dict[str, Any]] = None

    def __post_init__(self) -> None:
        if not isinstance(self.execution_engine, str) or not isinstance(self.engine_mode, str) or not isinstance(self.engine_evidence_ref, str):
            raise ValueError("snapshot execution engine fields are invalid")
        if isinstance(self.dag_revision, bool) or not isinstance(self.dag_revision, int) or self.dag_revision < 0:
            raise ValueError("snapshot DAG revision is invalid")
        if not isinstance(self.ready_set, list) or not all(isinstance(item, str) and item.strip() for item in self.ready_set):
            raise ValueError("snapshot ready-set is invalid")
        if not isinstance(self.topology_digest, str):
            raise ValueError("snapshot topology digest is invalid")
        if self.binding_state not in {"blocked_unknown", "binding_verified"}:
            raise ValueError("snapshot binding state is invalid")
        if type(self.business_write_allowed) is not bool:
            raise ValueError("snapshot business write flag is invalid")
        if self.business_write_allowed and self.binding_state != "binding_verified":
            raise ValueError("snapshot business write requires verified binding")
        if self.binding_state != "binding_verified":
            return
        if not isinstance(self.binding_intent, BindingIntent) or not isinstance(
            self.binding_observation, BindingObservation
        ):
            raise ValueError("verified snapshot requires binding evidence")
        intent = self.binding_intent
        observation = self.binding_observation
        if not isinstance(observation.lease, SupervisorLeaseObservation):
            raise ValueError("verified snapshot lease lacks provenance")
        if not isinstance(observation.cursor_observation, WaitThreadsCursorObservation):
            raise ValueError("verified snapshot cursor lacks provenance")
        if not observation.lease.active or observation.lease.status != "active":
            raise ValueError("verified snapshot lease is not active")
        required_intent = ("node_id", "lease_id", "head_sha", "clean", "cursor")
        if any(getattr(intent, field, None) in (None, "") for field in required_intent):
            raise ValueError("verified snapshot binding intent is incomplete")
        required_observation = (
            "project_id",
            "task_id",
            "node_id",
            "host_id",
            "worktree",
            "managed_root",
            "branch",
            "base_sha",
            "head_sha",
            "lease",
            "cursor",
            "cursor_source",
            "cursor_task_id",
            "cursor_host_id",
            "cursor_lineage",
        )
        if any(getattr(observation, field, None) in (None, "") for field in required_observation):
            raise ValueError("verified snapshot binding observation is incomplete")
        from .task_registry import validate_binding

        if not validate_binding(intent, observation).verified:
            raise ValueError("verified snapshot binding evidence failed validation")

    def to_dict(self) -> Dict[str, Any]:
        result = asdict(self)
        if isinstance(self.binding_intent, BindingIntent):
            result["binding_intent"] = self.binding_intent.to_dict()
        if isinstance(self.binding_observation, BindingObservation):
            result["binding_observation"] = self.binding_observation.to_dict()
        if isinstance(self.v44_binding_intent, V44BindingIntent):
            result["v44_binding_intent"] = self.v44_binding_intent.to_dict()
        if isinstance(self.v44_binding_proof, V44BindingProof):
            result["v44_binding_proof"] = self.v44_binding_proof.to_dict()
        return result

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RunSnapshot":
        expected = {
            "schema_version",
            "run_id",
            "plan_id",
            "plan_version",
            "status",
            "nodes",
            "handles",
            "tasks",
            "authorization",
            "authorization_digest",
            "node_contract_digest",
            "event_sequence",
        }
        with_capability = expected | {"capability_contract_digest"}
        binding_fields = {
            "binding_intent",
            "binding_observation",
            "binding_state",
            "business_write_allowed",
        }
        allowed = with_capability | binding_fields | {"integration_review_evidence", "workflow", "prd_digest", "spec_digest", "legacy_run", "legacy_evidence", "legacy_evidence_digest", "execution_engine", "engine_mode", "engine_evidence_ref", "dag_revision", "ready_set", "topology_digest", "started_nodes", "active_concurrency", "capacity", "monitor_entry_evidence", "parallel_groups", "v44_binding_intent", "v44_binding_proof"}
        if not isinstance(data, dict) or not set(data).issubset(allowed) or not expected.issubset(data):
            raise ValueError("snapshot schema is invalid")
        normalized = dict(data)
        normalized.setdefault("capability_contract_digest", "")
        normalized.setdefault("binding_intent", None)
        normalized.setdefault("binding_observation", None)
        normalized.setdefault("binding_state", "blocked_unknown")
        normalized.setdefault("business_write_allowed", False)
        normalized.setdefault("integration_review_evidence", {})
        normalized.setdefault("prd_digest", "")
        normalized.setdefault("spec_digest", "")
        normalized.setdefault("legacy_run", False)
        normalized.setdefault("legacy_evidence", None)
        normalized.setdefault("legacy_evidence_digest", "")
        normalized.setdefault("execution_engine", "")
        normalized.setdefault("engine_mode", "")
        normalized.setdefault("engine_evidence_ref", "")
        normalized.setdefault("dag_revision", 0)
        normalized.setdefault("ready_set", [])
        normalized.setdefault("topology_digest", "")
        normalized.setdefault("started_nodes", [])
        normalized.setdefault("active_concurrency", 0)
        normalized.setdefault("capacity", 0)
        normalized.setdefault("monitor_entry_evidence", "")
        normalized.setdefault("parallel_groups", {})
        normalized.setdefault("v44_binding_intent", None)
        normalized.setdefault("v44_binding_proof", None)
        if normalized["v44_binding_intent"] is not None:
            if not isinstance(normalized["v44_binding_intent"], dict):
                raise ValueError("snapshot V4.4 binding intent is invalid")
            normalized["v44_binding_intent"] = V44BindingIntent.from_dict(normalized["v44_binding_intent"]).to_dict()
        if normalized["v44_binding_proof"] is not None:
            if not isinstance(normalized["v44_binding_proof"], dict):
                raise ValueError("snapshot V4.4 binding proof is invalid")
            normalized["v44_binding_proof"] = V44BindingProof.from_dict(normalized["v44_binding_proof"]).to_dict()
        if not isinstance(normalized["integration_review_evidence"], dict):
            raise ValueError("snapshot integration review evidence is invalid")
        if normalized.get("workflow") is not None and not isinstance(normalized["workflow"], dict):
            raise ValueError("snapshot workflow is invalid")
        for key in ("prd_digest", "spec_digest"):
            if not isinstance(normalized.get(key, ""), str):
                raise ValueError("snapshot {} is invalid".format(key))
        if type(normalized.get("legacy_run")) is not bool:
            raise ValueError("snapshot legacy_run is invalid")
        if normalized.get("legacy_evidence") is not None and not isinstance(normalized["legacy_evidence"], dict):
            raise ValueError("snapshot legacy evidence is invalid")
        if normalized.get("legacy_run") is True:
            evidence = normalized.get("legacy_evidence")
            if (not isinstance(evidence, dict) or not isinstance(evidence.get("source"), str)
                    or not evidence.get("source", "").strip() or not isinstance(evidence.get("run_id"), str)
                    or not evidence.get("run_id", "").strip()):
                raise ValueError("legacy snapshot evidence is missing")
            validate_run_id(evidence["run_id"])
            if not isinstance(normalized.get("legacy_evidence_digest"), str) or not _DIGEST.fullmatch(normalized["legacy_evidence_digest"]):
                raise ValueError("legacy snapshot evidence digest is invalid")
        if normalized["binding_state"] == "binding_verified":
            normalized["binding_state"] = "blocked_unknown"
            normalized["business_write_allowed"] = False
        elif normalized["binding_state"] != "binding_verified" and normalized[
            "business_write_allowed"
        ]:
            normalized["business_write_allowed"] = False
        if normalized["binding_intent"] is not None and not isinstance(
            normalized["binding_intent"], dict
        ):
            raise ValueError("snapshot binding intent is invalid")
        if normalized["binding_intent"] is not None:
            normalized["binding_intent"] = BindingIntent.from_dict(
                normalized["binding_intent"]
            ).to_dict()
        if normalized["binding_observation"] is not None and not isinstance(
            normalized["binding_observation"], dict
        ):
            raise ValueError("snapshot binding observation is invalid")
        if normalized["binding_observation"] is not None:
            normalized["binding_observation"] = BindingObservation.from_dict(
                normalized["binding_observation"]
            ).to_dict()
        if normalized["binding_state"] not in {"blocked_unknown", "binding_verified"}:
            raise ValueError("snapshot binding state is invalid")
        if type(normalized["business_write_allowed"]) is not bool:
            raise ValueError("snapshot business write flag is invalid")
        if normalized["business_write_allowed"] and normalized["binding_state"] != "binding_verified":
            raise ValueError("snapshot business write requires verified binding")
        normalized["authorization"] = AuthorizationRecord.from_dict(
            normalized["authorization"]
        ).to_dict()
        return cls(**normalized)


def validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError("run id must be a simple identifier")
    return run_id


def _safe_project_path(paths: ProjectPaths, *parts: str) -> Path:
    root = paths.root.resolve()
    candidate = root.joinpath(*parts)
    current = root
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("persistence path may not traverse a symlink")
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as error:
        raise ValueError("persistence path escapes the project root") from error
    return candidate


def runs_archive_dir(paths: ProjectPaths, run_id: str) -> Path:
    """Archive location for a finished run, mirroring ``plans-archive``."""
    return _safe_project_path(paths, ".vibe", "runs-archive", run_id)


def run_dir(paths: ProjectPaths, run_id: str, create: bool = False) -> Path:
    validate_run_id(run_id)
    directory = _safe_project_path(paths, ".vibe", "runs", run_id)
    if not directory.exists():
        # An archived run's evidence stays addressable read-side: any later
        # run-scoped read or write lands inside the archive instead of
        # resurrecting a stub directory under .vibe/runs/.
        archived = runs_archive_dir(paths, run_id)
        if archived.is_dir() and not archived.is_symlink():
            return archived
    if create:
        directory.mkdir(parents=True, exist_ok=True)
        # Revalidate after mkdir to catch a concurrently inserted symlink.
        directory = _safe_project_path(paths, ".vibe", "runs", run_id)
    return directory


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise ValueError("atomic write parent may not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".", dir=str(path.parent)
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary_path), str(path))
        try:
            parent_descriptor = os.open(str(path.parent), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


@contextmanager
def interprocess_lock(lock_path: Path, timeout: float = 10.0) -> Iterator[None]:
    """Exclusive advisory lock whose kernel ownership survives partial metadata.

    This is intentionally a filesystem-local mutex, not a distributed lock.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.parent.is_symlink() or lock_path.is_symlink():
        raise ValueError("lock path may not be a symlink")
    owner = uuid.uuid4().hex
    payload = json.dumps(
        {"pid": os.getpid(), "owner": owner},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    deadline = time.monotonic() + timeout
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(str(lock_path), flags, 0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("lock path may not be a symlink") from error
        raise
    try:
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            while True:
                try:
                    flock_exclusive(descriptor, blocking=False)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError("timed out waiting for persistence lock")
                    time.sleep(0.01)
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            yield
        finally:
            try:
                flock_release(descriptor)
            except OSError:
                pass
    finally:
        os.close(descriptor)


def _event_lock(paths: ProjectPaths, run_id: str) -> Path:
    return run_dir(paths, run_id, create=True) / ".events.lock"


def _normalize_provenance(
    event: RunEvent, provenance: Optional[Dict[str, Any]]
) -> Dict[str, Any]:
    supplied = dict(provenance or {})
    normalized = {
        "role": supplied.get("role", "system"),
        "task_id": supplied.get("task_id"),
        "handle_id": supplied.get("handle_id"),
        "generation": supplied.get("generation", 0),
        "authorization_digest": supplied.get(
            "authorization_digest", event.data.get("authorization_digest", "")
        ),
        "node_contract_digest": supplied.get(
            "node_contract_digest", event.data.get("node_contract_digest", "")
        ),
    }
    if set(normalized) != _PROVENANCE_KEYS:
        raise ValueError("event provenance schema is invalid")
    if normalized["role"] not in {"system", "developer", "reviewer"}:
        raise ValueError("event provenance role is invalid")
    if not isinstance(normalized["generation"], int) or normalized["generation"] < 0:
        raise ValueError("event provenance generation is invalid")
    for key in ("task_id", "handle_id"):
        if normalized[key] is not None and not isinstance(normalized[key], str):
            raise ValueError("event provenance task/handle identity is invalid")
    for key in ("authorization_digest", "node_contract_digest"):
        if not isinstance(normalized[key], str):
            raise ValueError("event provenance digest is invalid")
    return normalized


def _decode_event_records(raw: bytes) -> List[Dict[str, Any]]:
    raw_lines = [line for line in raw.splitlines() if line.strip()]
    records: List[Dict[str, Any]] = []
    expected_keys = {
        "schema_version",
        "sequence",
        "event_id",
        "run_id",
        "event",
        "provenance",
        "data",
        "previous_event_digest",
        "event_digest",
    }
    previous_digest: Optional[str] = None
    for raw_line in raw_lines:
        try:
            record = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("event log is not valid JSONL") from error
        if not isinstance(record, dict) or set(record) != expected_keys:
            raise ValueError("event record schema is invalid")
        if record["schema_version"] != EVENT_SCHEMA_VERSION:
            raise ValueError("unsupported event record schema")
        if not isinstance(record["sequence"], int) or record["sequence"] < 1:
            raise ValueError("event sequence is invalid")
        if not isinstance(record["event_id"], str) or not record["event_id"]:
            raise ValueError("event id is invalid")
        if not isinstance(record["run_id"], str) or not isinstance(record["event"], str):
            raise ValueError("event identity is invalid")
        if not isinstance(record["data"], dict):
            raise ValueError("event data is invalid")
        provenance = record["provenance"]
        if not isinstance(provenance, dict) or set(provenance) != _PROVENANCE_KEYS:
            raise ValueError("event provenance schema is invalid")
        if provenance["role"] not in {"system", "developer", "reviewer"}:
            raise ValueError("event provenance role is invalid")
        if not isinstance(provenance["generation"], int) or provenance["generation"] < 0:
            raise ValueError("event provenance generation is invalid")
        if record["previous_event_digest"] != previous_digest:
            raise ValueError("event hash chain is inconsistent")
        if not isinstance(record["event_digest"], str) or not _DIGEST.fullmatch(
            record["event_digest"]
        ):
            raise ValueError("event digest is invalid")
        digest_payload = dict(record)
        digest_payload.pop("event_digest")
        expected_digest = hashlib.sha256(
            json.dumps(
                digest_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if record["event_digest"] != expected_digest:
            raise ValueError("event record digest is inconsistent")
        records.append(record)
        previous_digest = record["event_digest"]
    if [item["sequence"] for item in records] != list(range(1, len(records) + 1)):
        raise ValueError("event log sequence is not ordered")
    if len({item["event_id"] for item in records}) != len(records):
        raise ValueError("event ids must be unique")
    return records


def _read_event_records(path: Path) -> List[Dict[str, Any]]:
    try:
        if path.is_symlink():
            raise ValueError("event log may not be a symlink")
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    return _decode_event_records(raw)


def load_events(paths: ProjectPaths, run_id: str) -> List[Dict[str, Any]]:
    directory = run_dir(paths, run_id, create=False)
    records = _read_event_records(directory / "events.jsonl")
    if any(record["run_id"] != run_id for record in records):
        raise ValueError("event run lineage is inconsistent")
    return records


def append_event(
    paths: ProjectPaths,
    event: RunEvent,
    provenance: Optional[Dict[str, Any]] = None,
) -> int:
    sanitized_data = _sanitize_event_data(event.data)
    run_id = sanitized_data.get("run_id")
    validate_run_id(run_id)
    directory = run_dir(paths, run_id, create=True)
    event_path = directory / "events.jsonl"
    normalized_provenance = _normalize_provenance(event, provenance)
    with interprocess_lock(_event_lock(paths, run_id)):
        descriptor = None
        directory_descriptor: Optional[int] = None
        try:
            flags = os.O_CREAT | os.O_RDWR | os.O_APPEND | getattr(os, "O_BINARY", 0)
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            if os.open in os.supports_dir_fd:
                directory_flags = os.O_RDONLY
                if hasattr(os, "O_DIRECTORY"):
                    directory_flags |= os.O_DIRECTORY
                if hasattr(os, "O_NOFOLLOW"):
                    directory_flags |= os.O_NOFOLLOW
                directory_descriptor = os.open(str(directory), directory_flags)
                try:
                    descriptor = os.open(
                        event_path.name, flags, 0o600, dir_fd=directory_descriptor
                    )
                except OSError as error:
                    if error.errno in {errno.ELOOP, getattr(errno, "EFTYPE", -1)}:
                        raise ValueError("event log may not be a symlink") from error
                    raise
            else:
                # Windows: directories cannot be opened with os.open and
                # dir_fd is unsupported, so fall back to a full-path open
                # with an explicit symlink check (advisory; creating
                # symlinks on Windows requires elevated privileges).
                if directory.is_symlink() or event_path.is_symlink():
                    raise ValueError("event log may not be a symlink")
                descriptor = os.open(str(event_path), flags, 0o600)
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("event log must be a regular file")
            os.lseek(descriptor, 0, os.SEEK_SET)
            chunks = []
            while True:
                chunk = os.read(descriptor, 64 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            records = _decode_event_records(b"".join(chunks))
            sequence = len(records) + 1
            record = {
                "schema_version": EVENT_SCHEMA_VERSION,
                "sequence": sequence,
                "event_id": uuid.uuid4().hex,
                "run_id": run_id,
                "event": event.event,
                "provenance": normalized_provenance,
                "data": sanitized_data,
                "previous_event_digest": (
                    records[-1]["event_digest"] if records else None
                ),
            }
            record["event_digest"] = hashlib.sha256(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            encoded = (
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8")
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fsync(descriptor)
            return sequence
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if directory_descriptor is not None:
                os.close(directory_descriptor)


def _validate_snapshot(snapshot: RunSnapshot, records: List[Dict[str, Any]]) -> None:
    if snapshot.schema_version != STATE_SCHEMA_VERSION:
        raise ValueError("unsupported snapshot schema")
    validate_run_id(snapshot.run_id)
    if not isinstance(snapshot.plan_id, str) or not snapshot.plan_id:
        raise ValueError("snapshot plan id is invalid")
    if not isinstance(snapshot.plan_version, int) or snapshot.plan_version < 1:
        raise ValueError("snapshot plan version is invalid")
    if snapshot.status not in _RUN_STATUSES:
        raise ValueError("snapshot run status is invalid")
    if not isinstance(snapshot.nodes, dict) or not snapshot.nodes:
        raise ValueError("snapshot nodes must be a non-empty object")
    if not isinstance(snapshot.handles, dict) or not isinstance(snapshot.tasks, dict):
        raise ValueError("snapshot handle/task registries are invalid")
    if not isinstance(snapshot.event_sequence, int) or snapshot.event_sequence < 1:
        raise ValueError("snapshot event sequence is invalid")
    if snapshot.event_sequence > len(records):
        raise ValueError("snapshot references missing events")
    if not _DIGEST.fullmatch(snapshot.authorization_digest):
        raise ValueError("snapshot authorization digest is invalid")
    if not _DIGEST.fullmatch(snapshot.node_contract_digest):
        raise ValueError("snapshot node contract digest is invalid")
    if snapshot.capability_contract_digest and not _DIGEST.fullmatch(
        snapshot.capability_contract_digest
    ):
        raise ValueError("snapshot capability contract digest is invalid")

    authorization = AuthorizationRecord.from_dict(snapshot.authorization)
    if not is_authorization_integrity_valid(authorization):
        raise ValueError("snapshot authorization record digest is invalid")
    if authorization.digest != snapshot.authorization_digest:
        raise ValueError("snapshot authorization lineage is inconsistent")
    if authorization.node_contract_digest != snapshot.node_contract_digest:
        raise ValueError("snapshot contract lineage is inconsistent")
    if authorization.plan_id != snapshot.plan_id or authorization.plan_version != snapshot.plan_version:
        raise ValueError("snapshot plan lineage is inconsistent")
    if set(snapshot.nodes) != set(authorization.node_ids):
        raise ValueError("snapshot node set is inconsistent")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in snapshot.handles.items()):
        raise ValueError("snapshot handles are invalid")
    if not set(snapshot.handles).issubset(snapshot.nodes):
        raise ValueError("snapshot handle node is unknown")
    if len(set(snapshot.handles.values())) != len(snapshot.handles):
        raise ValueError("snapshot active handles must be unique")
    for node_id, node in snapshot.nodes.items():
        if not isinstance(node, dict) or node.get("status") not in _NODE_STATUSES:
            raise ValueError("snapshot node state is invalid for " + node_id)
    if snapshot.status == "complete" and any(
        node.get("workspace_reconciliation_required") is True for node in snapshot.nodes.values()
    ):
        raise ValueError("complete run has unresolved workspace quarantine")
    if snapshot.status == "complete":
        if not all(node.get("status") == "accepted" for node in snapshot.nodes.values()):
            raise ValueError("complete run contains unaccepted nodes")
        if "integration-review" in snapshot.nodes:
            evidence = snapshot.integration_review_evidence
            clearance = evidence.get("clearance") if isinstance(evidence, dict) else None
            if not isinstance(evidence, dict) or not evidence or not isinstance(clearance, dict) or any(clearance.get(key) != 0 for key in ("p0", "p1", "p2")):
                raise ValueError("complete run lacks accepted integration review evidence")
            try:
                from .evidence import validate_integration_review_evidence
                current_evidence = {key: value for key, value in evidence.items() if key != "history"}
                validate_integration_review_evidence(snapshot, current_evidence)
            except (TypeError, ValueError) as error:
                raise ValueError("complete run integration evidence is invalid") from error

    first = records[0] if records else None
    if first is None or first["event"] != "run_started":
        raise ValueError("snapshot has no run-start event lineage")
    current_authorization_digest = first["data"].get("authorization_digest")
    current_node_contract_digest = first["data"].get("node_contract_digest")
    first_capability_contract_digest = first["data"].get(
        "capability_contract_digest", ""
    )
    if not isinstance(current_authorization_digest, str) or not _DIGEST.fullmatch(
        current_authorization_digest
    ):
        raise ValueError("run-start authorization lineage is inconsistent")
    if not isinstance(current_node_contract_digest, str) or not _DIGEST.fullmatch(
        current_node_contract_digest
    ):
        raise ValueError("run-start contract lineage is inconsistent")
    if first_capability_contract_digest and not _DIGEST.fullmatch(
        first_capability_contract_digest
    ):
        raise ValueError("run-start capability contract lineage is inconsistent")
    current_prd_digest = first["data"].get("prd_digest")
    current_spec_digest = first["data"].get("spec_digest")
    current_capability_contract_digest = first_capability_contract_digest
    if sorted(first["data"].get("node_ids", [])) != sorted(snapshot.nodes):
        raise ValueError("run-start node lineage is inconsistent")
    if snapshot.legacy_run and first["data"].get("legacy_evidence_digest") != snapshot.legacy_evidence_digest:
        raise ValueError("legacy evidence lineage is inconsistent")
    retained_acceptance_proofs = []
    latest_node_contract_digests = None
    epoch_contracts = first["data"].get("authorized_node_contracts")
    epoch_digests = first["data"].get("node_contract_digests")
    epoch_scope_digests = first["data"].get("node_scope_digests")
    for record in records[: snapshot.event_sequence]:
        provenance = record["provenance"]
        if provenance["authorization_digest"] != current_authorization_digest:
            raise ValueError("event authorization provenance is inconsistent")
        if provenance["node_contract_digest"] != current_node_contract_digest:
            raise ValueError("event contract provenance is inconsistent")
        if record["event"] != "authorization_reauthorized":
            continue
        data = record["data"]
        if (
            data.get("previous_authorization_digest")
            != current_authorization_digest
            or data.get("previous_node_contract_digest")
            != current_node_contract_digest
        ):
            raise ValueError("reauthorization previous lineage is inconsistent")
        previous_capability_contract_digest = data.get(
            "previous_capability_contract_digest", current_capability_contract_digest
        )
        replacement_capability_contract_digest = data.get(
            "capability_contract_digest", current_capability_contract_digest
        )
        if (
            previous_capability_contract_digest != current_capability_contract_digest
            or not isinstance(replacement_capability_contract_digest, str)
            or (
                replacement_capability_contract_digest
                and not _DIGEST.fullmatch(replacement_capability_contract_digest)
            )
        ):
            raise ValueError("reauthorization capability contract lineage is inconsistent")
        source_fields = {"previous_prd_digest", "previous_spec_digest", "prd_digest", "spec_digest"}
        if source_fields & set(data) and not source_fields.issubset(data):
            raise ValueError("reauthorization source lineage is incomplete")
        for source, pinned in (("prd", current_prd_digest), ("spec", current_spec_digest)):
            if pinned is not None and data.get("previous_" + source + "_digest", pinned) != pinned:
                raise ValueError("reauthorization source lineage is inconsistent")
        current_prd_digest = data.get("prd_digest", current_prd_digest)
        current_spec_digest = data.get("spec_digest", current_spec_digest)
        previous = AuthorizationRecord.from_dict(data.get("previous_authorization"))
        replacement = AuthorizationRecord.from_dict(data.get("new_authorization"))
        if (
            not is_authorization_integrity_valid(previous)
            or previous.digest != current_authorization_digest
            or previous.node_contract_digest != current_node_contract_digest
        ):
            raise ValueError("reauthorization previous record is invalid")
        if (
            not is_authorization_integrity_valid(replacement)
            or replacement.plan_id != snapshot.plan_id
            or replacement.plan_version != snapshot.plan_version
            or set(replacement.node_ids) != set(snapshot.nodes)
            or data.get("authorization_digest") != replacement.digest
            or data.get("node_contract_digest")
            != replacement.node_contract_digest
        ):
            raise ValueError("reauthorization replacement record is invalid")
        previous_node_contract_digests = data.get("previous_node_contract_digests")
        node_contract_digests = data.get("node_contract_digests")
        retained_acceptances = data.get("retained_acceptances")
        invalidated_acceptances = data.get("invalidated_acceptances")
        changed_nodes = data.get("changed_nodes")
        affected_nodes = data.get("affected_nodes")
        accepted_nodes = data.get("accepted_nodes")
        authorized_node_contracts = data.get("authorized_node_contracts")
        if (
            not isinstance(previous_node_contract_digests, dict)
            or not isinstance(node_contract_digests, dict)
            or set(previous_node_contract_digests) != set(snapshot.nodes)
            or set(node_contract_digests) != set(snapshot.nodes)
            or not isinstance(retained_acceptances, dict)
            or not isinstance(invalidated_acceptances, dict)
            or set(retained_acceptances) & set(invalidated_acceptances)
            or not isinstance(authorized_node_contracts, dict)
            or set(authorized_node_contracts) != set(snapshot.nodes)
            or any(
                not isinstance(value, str)
                for value in authorized_node_contracts.values()
            )
            or any(
                not isinstance(items, list)
                or any(not isinstance(node_id, str) for node_id in items)
                or len(items) != len(set(items))
                or items != sorted(items)
                or any(node_id not in snapshot.nodes for node_id in items)
                for items in (changed_nodes, affected_nodes, accepted_nodes)
            )
        ):
            raise ValueError("reauthorization node acceptance lineage is invalid")
        for mapping in (previous_node_contract_digests, node_contract_digests):
            if any(
                not isinstance(value, str) or not _DIGEST.fullmatch(value)
                for value in mapping.values()
            ):
                raise ValueError("reauthorization node contract digest is invalid")
        try:
            authorized_nodes = []
            for node_id, encoded_node in authorized_node_contracts.items():
                decoded_node = json.loads(encoded_node)
                authorized_node = DAGNode.from_dict(decoded_node)
                if authorized_node.id != node_id:
                    raise ValueError("authorized DAG node identity is inconsistent")
                authorized_nodes.append(authorized_node)
        except (json.JSONDecodeError, TypeError, ValueError) as error:
            raise ValueError("reauthorization authorized DAG proof is invalid") from error
        # 5.0.7 wrote these digests with file scope included.  The aggregate
        # selects the algorithm and every per-node digest must use the same
        # one, so a proof mixing the two is rejected.
        digest_algorithm = next(
            (
                algorithm
                for algorithm in (
                    executable_contract_digest,
                    legacy_executable_contract_digest,
                )
                if algorithm(authorized_nodes) == replacement.node_contract_digest
            ),
            None,
        )
        if digest_algorithm is None or any(
            digest_algorithm([node]) != node_contract_digests[node.id]
            for node in authorized_nodes
        ):
            raise ValueError("reauthorization authorized DAG proof is invalid")
        if replacement.node_scope_digests is not None and data.get("node_scope_digests") != replacement.node_scope_digests:
            raise ValueError("reauthorization signed scope proof is inconsistent")
        if "node_scope_digests" in data and data["node_scope_digests"] != {
            node.id: legacy_executable_contract_digest([node]) for node in authorized_nodes
        }:
            raise ValueError("reauthorization per-node scope proof is invalid")
        previous_by_id = verified_authorized_nodes(
            epoch_contracts, epoch_digests, previous, epoch_scope_digests
        )
        authorized_by_id = {node.id: node for node in authorized_nodes}
        # The previous digest may be the 5.0.7 form; a node is unchanged when
        # either form of it still binds the authorized contract.
        expected_changed_nodes = sorted(
            node_id
            for node_id in snapshot.nodes
            if (
                node_id == "integration-review" and (
                    previous.integration_contract_digest != replacement.integration_contract_digest
                    or data.get("prd_digest", snapshot.prd_digest) != data.get("previous_prd_digest", snapshot.prd_digest)
                    or data.get("spec_digest", snapshot.spec_digest) != data.get("previous_spec_digest", snapshot.spec_digest)
                )
            ) or not _previous_digest_binds(
                authorized_by_id[node_id],
                previous_node_contract_digests[node_id],
                previous,
                replacement,
                previous_by_id.get(node_id),
            )
        )
        retained_ids = set(retained_acceptances)
        invalidated_ids = set(invalidated_acceptances)
        if (
            changed_nodes != expected_changed_nodes
            or affected_nodes
            != affected_node_closure(authorized_nodes, changed_nodes)
            or accepted_nodes != sorted(retained_ids | invalidated_ids)
            or set(changed_nodes) - set(affected_nodes)
            or retained_ids & set(affected_nodes)
            or not invalidated_ids.issubset(set(affected_nodes))
            or retained_ids | invalidated_ids != set(accepted_nodes)
        ):
            raise ValueError("reauthorization affected suffix disposition is invalid")
        if data.get("change_reason") == "platform_handoff" and (
            changed_nodes
            or invalidated_ids
            # scope-only expansion reads as "unchanged" above, so the write
            # scope needs its own check (the write side compares it too).
            or previous.file_scope != replacement.file_scope
        ):
            raise ValueError("platform handoff transition changed the acceptance contract")
        for node_id, evidence in retained_acceptances.items():
            if (
                node_id not in snapshot.nodes
                or not isinstance(evidence, dict)
                or set(evidence) != {"contract_digest", "authorization_epoch"}
                or evidence["contract_digest"] != previous_node_contract_digests[node_id]
                or not _previous_digest_binds(
                    authorized_by_id[node_id],
                    evidence["contract_digest"],
                    previous,
                    replacement,
                    previous_by_id.get(node_id),
                )
                or evidence["authorization_epoch"] != previous.digest
            ):
                raise ValueError("retained acceptance proof is invalid")
            retained_acceptance_proofs.append(
                (node_id, node_contract_digests[node_id], replacement.digest)
            )
        for node_id, evidence in invalidated_acceptances.items():
            if (
                node_id not in snapshot.nodes
                or not isinstance(evidence, dict)
                or set(evidence) != {"contract_digest", "authorization_epoch"}
                or evidence["contract_digest"] != previous_node_contract_digests[node_id]
                or evidence["authorization_epoch"] != previous.digest
            ):
                raise ValueError("invalidated acceptance proof is invalid")
        epoch_contracts = authorized_node_contracts
        epoch_digests = node_contract_digests
        epoch_scope_digests = data.get("node_scope_digests")
        latest_node_contract_digests = node_contract_digests
        current_authorization_digest = replacement.digest
        current_node_contract_digest = replacement.node_contract_digest
        current_capability_contract_digest = replacement_capability_contract_digest
    if (current_prd_digest is not None and current_prd_digest != snapshot.prd_digest) or (
        current_spec_digest is not None and current_spec_digest != snapshot.spec_digest
    ):
        raise ValueError("snapshot final source lineage is inconsistent")
    if current_authorization_digest != snapshot.authorization_digest:
        raise ValueError("snapshot final authorization lineage is inconsistent")
    if current_node_contract_digest != snapshot.node_contract_digest:
        raise ValueError("snapshot final contract lineage is inconsistent")
    if current_capability_contract_digest != snapshot.capability_contract_digest:
        raise ValueError("snapshot final capability contract lineage is inconsistent")
    if latest_node_contract_digests is not None and any(
        snapshot.nodes[node_id].get("contract_digest") != digest
        for node_id, digest in latest_node_contract_digests.items()
    ):
        raise ValueError("snapshot final node contract lineage is inconsistent")

    for node_id, node in snapshot.nodes.items():
        if node.get("status") != "accepted":
            continue
        contract_digest = node.get("contract_digest")
        acceptance = node.get("acceptance")
        if (
            not isinstance(contract_digest, str)
            or not _DIGEST.fullmatch(contract_digest)
            or not isinstance(acceptance, dict)
            or set(acceptance) != {"contract_digest", "authorization_epoch"}
            or acceptance["contract_digest"] != contract_digest
            or acceptance["authorization_epoch"] != snapshot.authorization_digest
        ):
            raise ValueError("accepted node acceptance contract epoch is invalid")
        matching = [
            record
            for record in records[: snapshot.event_sequence]
            if record["event"] == "accepted"
            and record["data"].get("node_id") == node_id
        ]
        if not matching:
            raise ValueError("accepted node lacks an acceptance event")
        current_acceptance_event = matching[-1]
        if not (
            current_acceptance_event["data"].get("contract_digest")
            == acceptance["contract_digest"]
            and current_acceptance_event["data"].get("authorization_epoch")
            == acceptance["authorization_epoch"]
        ) and not any(
            proof
            == (node_id, acceptance["contract_digest"], acceptance["authorization_epoch"])
            for proof in retained_acceptance_proofs
        ):
            raise ValueError("accepted node acceptance evidence is stale")
        provenance = matching[-1]["provenance"]
        if provenance["role"] != "reviewer":
            raise ValueError("accepted node lacks reviewer provenance")
        if provenance["task_id"] != node.get("reviewer_identity"):
            raise ValueError("accepted node reviewer identity is inconsistent")
        if provenance["generation"] != node.get("review_generation"):
            raise ValueError("accepted node review generation is inconsistent")


def _decode_snapshot(data: bytes, records: List[Dict[str, Any]]) -> RunSnapshot:
    try:
        raw = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("snapshot is not valid JSON") from error
    snapshot = RunSnapshot.from_dict(raw)
    _validate_snapshot(snapshot, records)
    return snapshot


def save_snapshot(paths: ProjectPaths, snapshot: RunSnapshot) -> None:
    directory = run_dir(paths, snapshot.run_id, create=True)
    records = load_events(paths, snapshot.run_id)
    _validate_snapshot(snapshot, records)
    state_path = directory / "state.json"
    previous_path = directory / "state.previous.json"
    lock_path = directory / ".state.lock"
    sanitized_snapshot = _sanitize_durable_value(snapshot.to_dict())
    serialized = json.dumps(
        sanitized_snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    with interprocess_lock(lock_path):
        if state_path.exists():
            current = state_path.read_bytes()
            try:
                previous_snapshot = _decode_snapshot(current, records)
            except (TypeError, ValueError):
                pass
            else:
                _atomic_bytes(
                    previous_path,
                    json.dumps(
                        _sanitize_durable_value(previous_snapshot.to_dict()),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8"),
                )
        _atomic_bytes(state_path, serialized)


def verify_run_evidence(paths: ProjectPaths, run_id: str) -> RunSnapshot:
    """Strict read of a run's evidence: event hash chain plus the current state.json.

    Unlike ``load_snapshot`` this never falls back to ``state.previous.json``
    and never rewrites ``state.json``; a hand-edited or corrupt file raises
    ``ValueError`` instead of being silently replaced by an older snapshot.
    """
    try:
        directory = run_dir(paths, run_id, create=False)
        records = load_events(paths, run_id)
        snapshot = _decode_snapshot((directory / "state.json").read_bytes(), records)
    except (OSError, TypeError) as error:
        raise ValueError("run evidence is unreadable: " + type(error).__name__) from error
    if snapshot.run_id != run_id:
        raise ValueError("snapshot run id does not match its path")
    return snapshot


def load_snapshot(paths: ProjectPaths, run_id: str) -> RunSnapshot:
    directory = run_dir(paths, run_id, create=False)
    records = load_events(paths, run_id)
    failures: List[Exception] = []
    state_path = directory / "state.json"
    previous_path = directory / "state.previous.json"
    try:
        current_raw = state_path.read_bytes()
        current_snapshot = _decode_snapshot(current_raw, records)
    except (FileNotFoundError, OSError, TypeError, ValueError) as error:
        failures.append(error)
    else:
        if current_snapshot.run_id == run_id:
            return current_snapshot
        failures.append(ValueError("snapshot run id does not match its path"))

    # The current file was not usable.  Re-read both candidates while holding
    # the same lock used by save_snapshot so a newer writer cannot land between
    # selecting the recovery bytes and restoring state.json.
    if not state_path.exists() and not previous_path.exists():
        if failures:
            raise ValueError("no valid snapshot for run " + run_id) from failures[-1]
        raise ValueError("no valid snapshot for run " + run_id)

    try:
        with interprocess_lock(directory / ".state.lock"):
            try:
                current_raw = state_path.read_bytes()
                current_snapshot = _decode_snapshot(current_raw, records)
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                failures.append(error)
            else:
                if current_snapshot.run_id == run_id:
                    return current_snapshot
                failures.append(ValueError("snapshot run id does not match its path"))

            try:
                previous_raw = previous_path.read_bytes()
                previous_snapshot = _decode_snapshot(previous_raw, records)
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                failures.append(error)
            else:
                if previous_snapshot.run_id != run_id:
                    failures.append(ValueError("snapshot run id does not match its path"))
                else:
                    try:
                        _atomic_bytes(state_path, previous_raw)
                    except (OSError, ValueError) as error:
                        raise ValueError("snapshot recovery could not restore state") from error
                    return previous_snapshot
    except ValueError:
        raise
    if failures:
        raise ValueError("no valid snapshot for run " + run_id) from failures[-1]
    raise ValueError("no valid snapshot for run " + run_id)


def _lease_path(paths: ProjectPaths, node_id: str, worktree: str, intent_digest: Optional[str] = None, role: Optional[str] = None, generation: Optional[int] = None) -> Path:
    # V4.4 leases are keyed by the immutable intent when available.  Keep the
    # legacy node/worktree key for old snapshots and callers.
    material = (node_id + "\0" + worktree + (("\0" + role if role else "") + ("\0" + str(generation) if generation is not None else "") + "\0" + intent_digest if intent_digest else ""))
    key = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return _safe_project_path(paths, ".vibe", "leases", key + ".json")


def _lease_lock(paths: ProjectPaths) -> Path:
    return _safe_project_path(paths, ".vibe", ".leases.lock")


def _lease_id(node_id: str, worktree: str, run_id: str, role: Optional[str] = None, generation: Optional[int] = None, intent_digest: Optional[str] = None) -> str:
    material = node_id + "\0" + worktree + "\0" + run_id
    if role is not None or generation is not None or intent_digest is not None:
        material += "\0" + str(role or "") + "\0" + str(generation if generation is not None else "") + "\0" + str(intent_digest or "")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


supervisor_lease_id = _lease_id


def acquire_writer_lease(
    paths: ProjectPaths, node_id: str, worktree: str, run_id: str,
    intent_digest: Optional[str] = None, role: Optional[str] = None, generation: Optional[int] = None,
) -> bool:
    validate_run_id(run_id)
    lease_path = _lease_path(paths, node_id, worktree, intent_digest, role, generation)
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": LEASE_SCHEMA_VERSION,
        "node_id": node_id,
        "worktree": worktree,
        "run_id": run_id,
        "lease_id": _lease_id(node_id, worktree, run_id, role, generation, intent_digest),
        "role": role, "generation": generation,
        "intent_digest": intent_digest,
        "status": "active",
    }
    with interprocess_lock(_lease_lock(paths)):
        # No second writer may claim the same node/worktree, even with a
        # different intent or run.  Exact same lease remains idempotent below.
        lease_dir = _safe_project_path(paths, ".vibe", "leases")
        for candidate in lease_dir.glob("*.json"):
            if candidate == lease_path:
                continue
            try:
                probe = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if probe.get("node_id") == node_id and probe.get("worktree") == worktree and probe.get("status") in {"active", "quarantined"}:
                if probe.get("run_id") != run_id or (role is not None and generation is not None and probe.get("role") == role and probe.get("generation") == generation and probe.get("intent_digest") != intent_digest):
                    return False
        # A legacy caller must still observe any active intent-bound lease.
        if intent_digest is None:
            lease_dir = _safe_project_path(paths, ".vibe", "leases")
            for candidate in lease_dir.glob("*.json"):
                if candidate == lease_path:
                    continue
                try:
                    probe = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if probe.get("node_id") == node_id and probe.get("worktree") == worktree and probe.get("status") in {"active", "quarantined"}:
                    return False
        if lease_path.exists():
            try:
                existing = json.loads(lease_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            return (
                existing.get("schema_version") == LEASE_SCHEMA_VERSION
                and existing.get("run_id") == run_id
                and existing.get("node_id") == node_id
                and existing.get("worktree") == worktree
                and existing.get("intent_digest") == intent_digest
                and (role is None or existing.get("role") == role) and (generation is None or existing.get("generation") == generation)
            )
        _atomic_bytes(
            lease_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        return True


def read_writer_lease(
    paths: ProjectPaths, node_id: str, worktree: str,
    intent_digest: Optional[str] = None, role: Optional[str] = None, generation: Optional[int] = None,
) -> Optional[SupervisorLeaseObservation]:
    """Read the supervisor-owned lease without requiring provider metadata.

    The returned ``active`` flag is derived only from the local lease record.
    Missing or malformed records return ``None`` so a caller can fail closed
    without fabricating ownership or provenance.
    """

    lease_path = _lease_path(paths, node_id, worktree, intent_digest, role, generation)
    try:
        payload = json.loads(lease_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        # Intent-bound leases use a distinct filename.  A legacy read without
        # the intent must still be able to observe the single active lease for
        # this node/worktree; it never grants ownership and remains subject to
        # the same schema and lease-id checks below.
        payload = None
        if intent_digest is None and role is None and generation is None:
            lease_dir = _safe_project_path(paths, ".vibe", "leases")
            try:
                for candidate in lease_dir.glob("*.json"):
                    try:
                        probe = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if (probe.get("node_id") == node_id
                            and probe.get("worktree") == worktree
                            and probe.get("status") == "active"):
                        lease_path = candidate
                        payload = probe
                        break
            except OSError:
                return None
        if payload is None:
            return None
    if not isinstance(payload, dict):
        return None
    observed = dict(payload)
    intent_matches = (
        observed.get("intent_digest") == intent_digest
        if intent_digest is not None
        else True
    )
    expected_lease_id = (
        _lease_id(
            node_id, worktree, observed.get("run_id"),
            observed.get("role"), observed.get("generation"),
            observed.get("intent_digest"),
        )
        if (observed.get("role") is not None
                or observed.get("generation") is not None
                or observed.get("intent_digest") is not None)
        else _lease_id(node_id, worktree, observed.get("run_id"))
    )
    observed["active"] = (
        observed.get("schema_version") == LEASE_SCHEMA_VERSION
        and observed.get("node_id") == node_id
        and observed.get("worktree") == worktree
        and intent_matches
        and (role is None or observed.get("role") == role) and (generation is None or observed.get("generation") == generation)
        and isinstance(observed.get("run_id"), str)
        and observed.get("lease_id") == expected_lease_id
        and observed.get("status") == "active"
    )
    if not observed["active"]:
        return None
    try:
        # ``SupervisorLeaseObservation`` exposes the stable legacy ownership
        # proof.  Intent-bound filenames and IDs are still validated above,
        # but are internal lease storage details and are not part of that
        # public observation contract.
        observed["lease_id"] = _lease_id(node_id, worktree, observed.get("run_id"))
        return SupervisorLeaseObservation._from_read(observed, _token=_PROVENANCE_TOKEN)
    except (KeyError, TypeError, ValueError):
        return None


load_writer_lease = read_writer_lease


def quarantine_writer_lease(
    paths: ProjectPaths,
    node_id: str,
    worktree: str,
    run_id: str,
    reason: str,
) -> bool:
    lease_path = _lease_path(paths, node_id, worktree)
    with interprocess_lock(_lease_lock(paths)):
        if not lease_path.exists():
            for candidate in (_safe_project_path(paths, ".vibe", "leases")).glob("*.json"):
                try:
                    probe = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
                if probe.get("node_id") == node_id and probe.get("worktree") == worktree and probe.get("run_id") == run_id:
                    lease_path = candidate; break
        try:
            payload = json.loads(lease_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return False
        if payload.get("run_id") != run_id:
            return False
        payload["status"] = "quarantined"
        payload["reason"] = redact_provider_text(reason)
        _atomic_bytes(
            lease_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        return True


def release_writer_lease(
    paths: ProjectPaths, node_id: str, worktree: str, run_id: str
) -> bool:
    lease_path = _lease_path(paths, node_id, worktree)
    with interprocess_lock(_lease_lock(paths)):
        candidates = [lease_path]
        if not lease_path.exists():
            try:
                candidates.extend(
                    candidate
                    for candidate in _safe_project_path(paths, ".vibe", "leases").glob("*.json")
                    if candidate != lease_path
                )
            except OSError:
                return False
        released = False
        for candidate in candidates:
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                continue
            if (payload.get("node_id") == node_id
                    and payload.get("worktree") == worktree
                    and payload.get("run_id") == run_id):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    continue
                released = True
        return released


def rebind_writer_lease(
    paths: ProjectPaths, node_id: str, worktree: str, run_id: str,
    role: str, generation: int, previous_intent_digest: Optional[str], intent_digest: str,
    *, previous_status: str = "quarantined",
) -> bool:
    """Move one same-run, same-generation lease (quarantined by default) to a new intent.

    ``previous_intent_digest=None`` accepts the one lease that differs from
    ``intent_digest`` (platform_handoff: the old digest is not recomputable).

    Only the orphan-adoption recovery uses this: the writer is unchanged, the
    authorization that names it was re-issued.  Any other active/quarantined
    lease on the node or writer root refuses the move.  Re-running after the
    new lease was written (crash before the old one was removed) completes it.
    """
    validate_run_id(run_id)
    new_path = _lease_path(paths, node_id, worktree, intent_digest, role, generation)
    payload = {
        "schema_version": LEASE_SCHEMA_VERSION,
        "node_id": node_id,
        "worktree": worktree,
        "run_id": run_id,
        "lease_id": _lease_id(node_id, worktree, run_id, role, generation, intent_digest),
        "role": role, "generation": generation,
        "intent_digest": intent_digest,
        "status": "active",
    }
    with interprocess_lock(_lease_lock(paths)):
        lease_dir = _safe_project_path(paths, ".vibe", "leases")
        previous = []
        for candidate in lease_dir.glob("*.json"):
            try:
                probe = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError):
                return False
            if not isinstance(probe, dict):
                return False
            if probe.get("status") not in {"active", "quarantined"}:
                continue
            if probe.get("node_id") != node_id and probe.get("worktree") != worktree:
                continue
            if candidate == new_path and probe == payload:
                continue
            if (probe.get("node_id") == node_id and probe.get("worktree") == worktree
                    and probe.get("run_id") == run_id and probe.get("role") == role
                    and probe.get("generation") == generation
                    and (probe.get("intent_digest") == previous_intent_digest
                         or (previous_intent_digest is None
                             and probe.get("intent_digest") != intent_digest))
                    and probe.get("status") == previous_status):
                previous.append(candidate)
                continue
            return False
        if len(previous) > 1 or (not previous and not new_path.exists()):
            return False
        _atomic_bytes(
            new_path,
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
        for candidate in previous:
            if candidate != new_path:
                candidate.unlink()
        return True


def _archive_merge_directory(source: Path, destination: Path, archive_root: Path,
                             moved: List[str], conflicts: List[str]) -> None:
    """Merge ``source`` into ``destination`` without overwriting evidence.

    Identical files are deduplicated; a conflicting file is kept beside the
    original under a ``.conflict-<digest>`` suffix so neither side is lost.
    """
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        target = destination / child.name
        if child.is_symlink():
            conflicts.append(str(child.relative_to(archive_root.parent.parent)))
            continue
        if child.is_dir():
            if target.exists() and not target.is_dir():
                conflicts.append(str(child.relative_to(archive_root.parent.parent)))
                continue
            target.mkdir(exist_ok=True)
            _archive_merge_directory(child, target, archive_root, moved, conflicts)
            try:
                child.rmdir()
            except OSError:
                pass
            continue
        relative = target.relative_to(archive_root).as_posix()
        if target.exists() and not target.is_symlink() and target.is_file():
            if target.read_bytes() == child.read_bytes():
                child.unlink()
                continue
            digest = hashlib.sha256(child.read_bytes()).hexdigest()[:8]
            conflict_target = destination / (child.name + ".conflict-" + digest)
            if not conflict_target.exists():
                os.replace(str(child), str(conflict_target))
                moved.append(conflict_target.relative_to(archive_root).as_posix())
            else:
                child.unlink()
            continue
        if target.exists() or target.is_symlink():
            conflicts.append(str(child.relative_to(archive_root.parent.parent)))
            continue
        os.replace(str(child), str(target))
        moved.append(relative)


def archive_run_leases(paths: ProjectPaths, run_id: str,
                       target_root: Path) -> List[str]:
    """Move writer leases carrying ``run_id`` into ``target_root/leases/``.

    Removing the lease file is what frees a node/worktree pair for a later
    dispatch; unreadable or foreign leases are left in place.
    """
    validate_run_id(run_id)
    lease_dir = _safe_project_path(paths, ".vibe", "leases")
    if lease_dir.is_symlink():
        raise ValueError("lease directory may not be a symlink")
    moved: List[str] = []
    if not lease_dir.is_dir():
        return moved
    for candidate in sorted(lease_dir.glob("*.json")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            continue
        destination = target_root / "leases" / candidate.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.is_file() and destination.read_bytes() == candidate.read_bytes():
                candidate.unlink()
                continue
            raise ValueError("archive lease destination already exists: " + candidate.name)
        os.replace(str(candidate), str(destination))
        moved.append("leases/" + candidate.name)
    return moved


def archive_run(paths: ProjectPaths, run_id: str) -> Dict[str, Any]:
    """Move one run's directory, provider artifacts and leases to the archive.

    Idempotent and crash-resumable: every partial state (run dir moved but
    manifest missing, artifacts half-swept, residual stub recreated by a late
    writer) converges on the next call.  Existing archive content is never
    overwritten; conflicting residual files are kept as ``.conflict-*``.
    """
    validate_run_id(run_id)
    primary = _safe_project_path(paths, ".vibe", "runs", run_id)
    destination = runs_archive_dir(paths, run_id)
    if destination.is_symlink() or (primary.is_symlink()):
        raise ValueError("run archive paths may not be symlinks")
    if primary.exists() and not primary.is_dir():
        raise ValueError("run archive source is not a directory")
    lock = _safe_project_path(paths, ".vibe", "runs-archive", ".archive.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    moved: List[str] = []
    conflicts: List[str] = []
    with interprocess_lock(lock):
        destination.mkdir(parents=True, exist_ok=True)
        manifest_path = destination / "archive-manifest.json"
        if manifest_path.exists() and not manifest_path.is_symlink():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeDecodeError):
                manifest = None
            if not isinstance(manifest, dict) or manifest.get("run_id") != run_id:
                manifest = None
        else:
            manifest = None
        if manifest is None:
            manifest = {
                "schema_version": 1,
                "run_id": run_id,
                "moved": [],
                "conflicts": [],
            }
        if primary.is_dir():
            try:
                # Single rename when the archive slot is still empty; a
                # pre-existing archive (rerun after a crash, or a stub
                # recreated by a late writer) falls through to a merge that
                # never overwrites.
                os.rename(str(primary), str(destination))
                moved.append(".")
            except OSError:
                _archive_merge_directory(
                    primary, destination, destination, moved, conflicts
                )
                try:
                    primary.rmdir()
                except OSError:
                    # Residual conflict files keep the source legible.
                    conflicts.append(str(primary))
        from .runners.provider_action import archive_run_provider_actions

        moved.extend(archive_run_provider_actions(paths, run_id, destination))
        moved.extend(archive_run_leases(paths, run_id, destination))
        seen = set(manifest["moved"])
        for item in moved:
            if item not in seen:
                manifest["moved"].append(item)
                seen.add(item)
        seen_conflicts = set(manifest["conflicts"])
        for item in conflicts:
            if item not in seen_conflicts:
                manifest["conflicts"].append(item)
                seen_conflicts.add(item)
        manifest["archived"] = True
        _atomic_bytes(
            manifest_path,
            (
                json.dumps(manifest, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":"))
                + "\n"
            ).encode("utf-8"),
        )
    return manifest


def _restore_tree(source_dir: Path, target_dir: Path,
                  conflicts_root: Path, moved: List[str]) -> None:
    """Move ``source_dir`` contents back into ``target_dir`` (unarchive).

    Never overwrites: identical files are deduplicated, divergent files are
    kept under ``conflicts_root`` so neither side is lost.
    """
    if not source_dir.is_dir() or source_dir.is_symlink():
        return
    for path in sorted(source_dir.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_file():
            continue
        destination = target_dir / path.name
        if destination.exists():
            if destination.is_file() and destination.read_bytes() == path.read_bytes():
                path.unlink()
                continue
            conflict = conflicts_root / path.name
            conflict.parent.mkdir(parents=True, exist_ok=True)
            os.replace(str(path), str(conflict))
            moved.append(str(conflict))
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(str(path), str(destination))
        moved.append(str(destination))
    try:
        source_dir.rmdir()
    except OSError:
        pass


def unarchive_run(paths: ProjectPaths, run_id: str) -> bool:
    """Restore an archived run under ``.vibe/runs/`` (issue #274).

    Inverse of :func:`archive_run`: a completed run reopened by
    reauthorization must write under ``.vibe/runs/`` again.  The run
    directory, its provider-action artifacts and its writer leases move back;
    ``restore-conflicts/`` keeps any divergent file that could not be restored
    without overwriting newer evidence.  Idempotent; returns True when the
    run directory is live under ``.vibe/runs/`` afterwards.
    """
    validate_run_id(run_id)
    primary = _safe_project_path(paths, ".vibe", "runs", run_id)
    archive = runs_archive_dir(paths, run_id)
    if primary.is_symlink() or archive.is_symlink():
        raise ValueError("run archive paths may not be symlinks")
    if primary.exists():
        if not primary.is_dir():
            raise ValueError("run restore destination is not a directory")
        return True
    if not archive.is_dir():
        return False
    lock = _safe_project_path(paths, ".vibe", "runs-archive", ".archive.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    with interprocess_lock(lock):
        if primary.exists() or not archive.is_dir():
            return primary.is_dir()
        # Bring the whole archive back first; the provider-actions/leases
        # subtrees inside it are then dispersed to their live homes so a
        # reopened run regains its mailbox requests and writer leases.
        os.rename(str(archive), str(primary))
        conflicts_root = primary / "restore-conflicts"
        provider_root = primary / "provider-actions"
        store_root = _safe_project_path(paths, ".vibe", "provider-actions")
        if provider_root.is_dir() and not provider_root.is_symlink():
            for sub in sorted(provider_root.iterdir(), key=lambda item: item.name):
                if sub.is_dir() and not sub.is_symlink():
                    _restore_tree(
                        sub, store_root / sub.name,
                        conflicts_root / "provider-actions" / sub.name, [],
                    )
            try:
                provider_root.rmdir()
            except OSError:
                pass
        _restore_tree(
            primary / "leases",
            _safe_project_path(paths, ".vibe", "leases"),
            conflicts_root / "leases", [],
        )
    return True
