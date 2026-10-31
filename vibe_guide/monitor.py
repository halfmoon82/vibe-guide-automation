"""Authorization-bound, write-ahead monitor for developer/reviewer DAG tasks."""

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from datetime import datetime, timezone
import subprocess
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid
import copy

from .authorization import (
    WORKER_TOPOLOGY_BACKGROUND,
    WORKER_TOPOLOGY_DUAL_VISIBLE,
    WORKER_TOPOLOGY_VISIBLE_SDD,
    AuthorizationRecord,
    affected_node_closure,
    canonical_node_contracts,
    legacy_executable_contract_digest,
    verified_authorized_nodes,
    route_only_acceptance_binds,
    contract_invariant_entries,
    digest_integration_contract,
    dispatch_topology_for_node,
    executable_contract_digest,
    integration_contract_projection,
    is_authorization_valid,
    validate_runtime_contract,
    _scoped_values,
)
from .contracts import RunEvent, RunHandle, Runner
from .models import (
    DAGNode,
    Plan,
    BindingIntent,
    BindingObservation,
    BindingVerification,
    IssueComplexity,
    LocalModel,
    HealingResult,
    ObservationDisposition,
    node_branch,
    node_worktree,
)
from .paths import ProjectPaths
from .path_ownership import validate_path_ownership
from .binding_lifecycle import VisibleSddAcceptance
from . import digests, fsio, monitor_acceptance, monitor_contracts, monitor_handoff
from .monitor_acceptance import VISIBLE_SDD_PROTOCOL_REF
from .planner import (
    ScopeExpansionResolution,
    _scope_compare_key,
    auto_scope_rule,
    resolve_consistency,
    resolve_scope_expansion,
)
from . import dag as _dag_module
from . import worker_liveness
from .dag import (
    _write_scope_paths,
    audit_dag,
    is_integration_review_node,
    node_scoped_ready,
    ready_nodes,
)
from .adapters.task_provider import ProviderActionStore, ProviderPending, ProviderUnavailable
from .state import (
    CONSISTENCY_CORRECTION_KEYS,
    RECOVERY_SIGNATURE_KEYS,
    RunSnapshot,
    require_not_legacy_run,
    acquire_writer_lease,
    append_event,
    archive_run as _archive_run_artifacts,
    is_redacted_value,
    load_events,
    load_snapshot,
    map_user_status,
    quarantine_writer_lease,
    read_writer_lease,
    redact_provider_text,
    release_writer_lease,
    rebind_writer_lease,
    run_dir,
    save_snapshot,
    scope_only_expansion_binds,
    interprocess_lock,
    unarchive_run,
    validate_run_id,
    WORKTREE_PREFLIGHT_KEY,
    WORKTREE_PREFLIGHT_KEYS,
    WORKTREE_REASON_CODES,
)
from .task_registry import (
    REGISTRY_SCHEMA_VERSION,
    TaskBinding,
    _all_bindings,
    _read_registry,
    _registry_lock,
    _run_tasks_path,
    active_registry_issues,
    binding_contract_enabled,
    load_task_binding,
    runtime_binding_gate,
    save_task_binding,
    reconcile_shadowed_successor,
    load_historical_task_binding,
)
from .config import load_project_config
from .worker_questions import WORKER_QUESTION_EVENT, park_question as park_worker_question
from .worker_questions import REPLAY_EVENTS as WORKER_QUESTION_REPLAY_EVENTS
from .worker_questions import replay as replay_worker_question
from .worktree_base import upstream_branches
from .scope_landing import check_delivery as check_delivery_landing
from .scope_landing import check_delivery_content as check_delivery_content_landing
from .workflow_gate import require_capability_contract, require_entry, verify_workflow
from .authorize_entry import load_live_workflow, select_plan_workflow, verify_workflow_artifacts
from .diagnostics import validate_child_session_binding
from .models import WorkerProfile
from .model_router import ModelRouter
from . import model_catalog
from .change_requests import (
    ChangeRequest,
    ChangeRequestEvidence,
    LocalMergeEvidence,
    execute_change_request,
    merge_local,
)
from .checkpoint import (
    ContextBudgetEstimator,
    ContextBudgetPolicy,
    MonitorCheckpoint,
    resume_from_checkpoint,
    write_checkpoint,
)
from .brief import ImplementationBrief, validate_implementation_brief
from .manifest import RunManifest
from .evidence import (
    blocked_self_report_fields,
    build_integration_review_evidence,
    evaluate_v41_closeout,
    evaluate_delivery_evidence,
    record_integration_review as _record_integration_review,
)
from .engine_attestation import validate_engine_attestation
from .decision_boundary import NotificationBridge, ingest_worker_decisions
from .orphan_recovery import ORPHAN_DISPATCH_ADOPTED, replay_orphan_adoption
from .providers import CODEX_PROVIDER
from .self_report_recovery import SELF_REPORT_DELIVERY_RECONCILED
from .decision_boundary import ScopedDecisionMailbox
from .decisions import DecisionMailbox


class ScopeDecisionRequired(PermissionError):
    """The requested expansion crosses a product-owned scope boundary."""


DECISION_NOTIFICATION_UNKNOWN_REASON = "decision notification bridge status unknown"
# A node that reaches blocked_design with no decision record is a supervisor
# lifecycle defect, not a wait-for-user.  Recovery: the supervisor either
# records the missing decision via `vibe decision-raise` (only
# product_acceptance / external_authorization may suspend) or returns the node
# to its developer.
LIFECYCLE_DECISION_RECOVERY_ACTION = "raise_missing_decision"
# Decision-less blocked_design producers record a fixed enum cause; the monitor
# registers a vibe-authored (never provider-derived) decision for it.
_BLOCKED_DESIGN_MAX_ATTEMPTS = 20
#: Findings from one reviewer identity before the node parks for a replacement.
REVIEW_LOOP_LIMIT = 3
_BLOCKED_DESIGN_DECISIONS = {
    "integration_scope_review": {
        "question": "整合审查要求把文件扩进已验收节点的范围，自动规则无法判定，需要产品经理复核是否允许扩范围",
        "options": [
            {"id": "approve_scope_expansion", "label": "允许扩范围",
             "impact": "选此项后由监工把文件并入该节点范围并安排该节点返工"},
            {"id": "keep_scope", "label": "不扩范围",
             "impact": "选此项后该节点范围不变，由监工安排整合审查另寻修法"},
        ],
    },
    "out_of_contract_finding": {
        "question": "审查发现节点合同之外的问题，无法自动判定唯一修法，需要产品经理决定是否纳入本节点（审查原文见该节点 reviewer 交付）",
        "options": [
            {"id": "accept_as_contract_change", "label": "纳入本节点",
             "impact": "选此项后由监工变更合同并安排该节点返工"},
            {"id": "defer_finding", "label": "不纳入",
             "impact": "选此项后由监工把该问题登记为后续问题，本节点按原合同验收"},
        ],
    },
}

#: Event written once per consumed answer set; replay re-applies the same
#: transition from the event alone (see ``_apply_decision_answer_state``).
DECISION_ANSWER_APPLIED = "decision_answer_applied"

_ANSWER_ACTIONS = {
    ("integration_scope_review", "approve_scope_expansion"): "scope_expanded",
    ("integration_scope_review", "keep_scope"): "scope_kept",
    ("out_of_contract_finding", "accept_as_contract_change"): "contract_changed",
    ("out_of_contract_finding", "defer_finding"): "finding_deferred",
}


def _requested_scope_files(scope_request: Any) -> List[str]:
    """File paths named by a provider scope-expansion request (strings only)."""
    files = scope_request.get("files") if isinstance(scope_request, dict) else scope_request
    if not isinstance(files, list):
        return []
    return [item for item in files if isinstance(item, str)]








# --- V4.6 ISSUE-04: topology-aware dispatch of visible worker sessions ---

#: Dispatch topologies a node can be ruled into.  ``visible-sdd`` binds one
#: visible worker session per node (review is in-session evidence, never a
#: second task); ``dual-visible`` keeps the developer/reviewer pair;
#: ``background`` is the disclosed downgrade when no visible bridge exists.
#: The ruling itself lives in
#: :func:`vibe_guide.authorization.dispatch_topology_for_node` so the
#: authorization card and the dispatcher can never disagree; these names are
#: re-exported for the supervisor's own reads.
TOPOLOGY_VISIBLE_SDD = WORKER_TOPOLOGY_VISIBLE_SDD
TOPOLOGY_DUAL_VISIBLE = WORKER_TOPOLOGY_DUAL_VISIBLE
TOPOLOGY_BACKGROUND = WORKER_TOPOLOGY_BACKGROUND

#: ISSUE-249: the reason the legacy acceptance ordering wrote when an
#: ``accepted`` event was recorded and the reviewer binding flipped before
#: the payload's top-level P0-P2 clearance evidence was checked.  The
#: snapshot persists every node ``reason`` redacted, so recovery never
#: matches this text; the durable signature is the written acceptance plus
#: the accepted reviewer binding.  Kept for the audit trail and for tests
#: that exercise the in-memory live path.
REVERSE_BLOCK_REASON = (
    "review acceptance has no registered P0-P2 clearance evidence"
)

#: ISSUE-254/255: how many consecutive validate-first format rejections the
#: same session (same handle + generation) may accumulate before the monitor
#: stops re-reading the same delivery.  Rejection stays recoverable by design
#: (#249/#83: the same session corrects and re-reports), so the first
#: rejection must never escalate; a live session corrects within one or two
#: re-reads.  Past this limit the delivery is assumed un-reportable -- the
#: observed incident (run-1ebcb792) had the reviewer session exit right after
#: delivering, so every ``vibe resume`` tick re-read the same payload and
#: re-rejected it forever while the node sat in ``review`` -- and the node is
#: escalated to a visible ``blocked_unknown`` with a human recovery action
#: instead of looping.
ACCEPTANCE_FORMAT_REJECTION_LIMIT = 3

#: Fixed text sent to the reviewer session whose delivery was rejected for its
#: format (D2).  It is a program constant: nothing the reviewer wrote is ever
#: echoed into it, the audit event or the supervisor-visible node field.
ACCEPTANCE_REJECTION_NOTICE = (
    "Your acceptance report was rejected for format. Re-report in this session: "
    "evidence must be an object with exactly the keys findings, "
    "iteration_compatibility, test_runtime_delivery, out_of_scope."
)

#: The node ``recovery_action`` stamped by the escalation above.  It names the
#: human recovery entries, both of which exist in the CLI: reshape the
#: delivery per the pinned schema and re-deliver through ``vibe reconcile``
#: (``Monitor.reconcile_evidence`` reuses the original bindings and replays
#: ordinary delivered/accepted events), or ``vibe requeue``/``repair`` to put
#: the node back into the scheduler.  For the integration reviewer specifically
#: the reshaped claim is recorded through ``Monitor.record_integration_review``.
ACCEPTANCE_FORMAT_ESCALATION_RECOVERY = "reshape_delivery_and_reconcile"


#: Disposition kinds that close one ``blocked_unknowns`` item reported by an
#: in-session review (issue #140 design draft section 3.6).  A non-empty
#: ``blocked_unknowns`` array never counts into the P0-P2 clearance numbers;
#: every item must instead be closed by an explicit disposition event before
#: acceptance may continue, so "cannot confirm" stops being a silent escape
#: hatch for real defects.
BLOCKED_UNKNOWN_DISPOSITION_KINDS = (
    "confirmed_benign",
    "escalated_to_findings",
    "resolved_with_facts",
    # Structural pending-verification item: only real devices, production or
    # the acceptance period can verify it, so the node proceeds to acceptance
    # and the item is listed in the delivery/acceptance report.
    "deferred_to_acceptance",
)

#: Dispositions that release an item for acceptance.  ``escalated_to_findings``
#: documents that the unknown is a real defect the worker session must rework;
#: it is recorded for the audit chain but never clears the gate.
BLOCKED_UNKNOWN_CLEARING_DISPOSITIONS = frozenset(
    {"confirmed_benign", "resolved_with_facts", "deferred_to_acceptance"}
)

#: Structural pending-verification classifier.  An item is structural only when
#: it names an environment that cannot be reached before acceptance AND defers
#: to the acceptance period (or is an unpersisted throwaway verification
#: script).  Anything else stays an ordinary blocked_unknown.
_PV_ENVIRONMENT_MARKERS = (
    "真机", "实机", "真实设备", "生产环境", "生产数据", "线上", "微信", "真实环境",
    "real device", "production", "on-device", "live environment",
)
#: An item that reports an already observed defect is never structural: it is a
#: finding, not something that merely cannot be checked yet.  One list, used by
#: both the classifier and the same-item comparison.
_PV_DEFECT_MARKERS = (
    "崩溃", "失败", "报错", "错误", "异常", "丢失", "算错", "不一致", "损坏", "回归",
    "出错", "故障", "缺陷", "不对", "有误", "闪退", "卡死", "卡住", "无响应", "白屏",
    "黑屏", "超时", "重复", "漏", "偶发",
    "crash", "fail", "error", "corrupt", "lost", "wrong", "regress", "broken", "bug",
    "hang", "hung", "freeze", "frozen", "timeout", "timed out", "duplicate", "flaky",
    "leak", "闪烁", "发热", "打不开", "收不到", "登录不上", "加载很慢",
)
#: Wording that describes something already seen happen ("出现/发生/报/显示/发现
#: + noun").  Deliberately broad: a false hit only keeps the item blocked_unknown.
_PV_OBSERVED_MARKERS = (
    "出现", "发生", "显示", "发现", "看到", "观察到", "抛出", "报",
    "observed", "occur", "shows", "showed", "found", "reported", "throws", "saw",
    "appears",
)
_PV_DEFERRAL_MARKERS = ("验收期", "acceptance period", "acceptance phase")
_PV_FILLER = (
    "验收期", "只能", "需要", "才能", "完成", "验证", "真实设备", "生产环境",
    "真机", "实机", "仅", "需", "在", "的", "了",
)


def _pv_reports_defect(text: str) -> bool:
    """Defect or already-observed wording; ASCII words match at word starts only."""
    for marker in _PV_DEFECT_MARKERS + _PV_OBSERVED_MARKERS:
        if marker[0].isascii():
            if re.search(r"\b" + re.escape(marker), text):
                return True
        elif marker in text:
            return True
    return False


#: The ONLY wordings accepted as a structural pending-verification item (also
#: documented in prd-guide.md); anything else is "unknown" and stays
#: blocked_unknown.  Shape: [env prefix] <short noun phrase> [10/10]
#: <only/need modal> [在] <acceptance-time word> <verify verb>, nothing else.
_PV_ZH_NOUN = r"[A-Za-z\u4e00-\u9fff]{1,8}?"
_PV_ZH_TEMPLATE = re.compile(
    r"^(?:(?:真机|实机|生产环境|线上|真实设备|生产)(?:上|中)?)?"
    r"(?P<noun>" + _PV_ZH_NOUN + r")"
    r"(?:\s*\d+/\d+)?\s*"
    r"(?:只能|仅能|仅|需要|需|须|待)在?"
    r"(?:验收期|上线后|真机上|生产环境|实机|线上)(?:上|中)?"
    r"(?:验证|确认|完成|验收)$"
)
_PV_EN_TEMPLATE = re.compile(
    r"^(?P<noun>[a-z]+(?: [a-z]+){0,4}?)(?: \d+/\d+)? "
    r"(?:can only be|must be|needs to be|to be|only) "
    r"(?:verified|confirmed|validated|completed) "
    r"(?:in|during|on|after) (?:the )?"
    r"(?:acceptance period|acceptance phase|production|a real device|real device|release)$"
)
_PV_SCRIPT_TEMPLATE = re.compile(
    r"^(?:临时)?验证脚本(?:不入库|仅本地运行)"
    r"(?:[，,](?:临时)?验证脚本(?:不入库|仅本地运行))?$"
)
#: Negation / predicate / degree words that make a noun phrase a report about
#: something that happened rather than a thing that cannot be checked yet.
_PV_NOUN_EXCLUDED_ZH = (
    "不", "没", "无", "未", "很", "太", "严重", "偶", "常", "总是", "老是", "反馈",
    "返回", "出现", "发生", "显示", "发现", "报", "慢", "差", "卡", "闪", "热",
    "收不", "打不", "登录不", "错", "坏", "挂", "断", "丢", "漏", "死",
)
#: A phrase that ENDS in a state/degree word reports a condition, not a thing.
_PV_NOUN_STATE_SUFFIX_ZH = (
    "空白", "模糊", "低", "高", "慢", "快", "失灵", "失效", "延迟", "多扣", "少扣",
    "爆音", "错位", "偏", "乱", "跳", "了", "多", "少", "大", "小", "长", "短",
    "弱", "强", "卡", "闪", "抖",
)
#: Digits with a unit/number word inside the phrase ("十分钟", "3 秒") never pass.
_PV_NUMBER_ZH = re.compile(r"[零一二两三四五六七八九十百千万几半]+(?:分钟|秒|小时|天|次|个|条|倍|%|元|块)")
_PV_NOUN_EXCLUDED_EN = re.compile(
    r"\b(?:not|no|cannot|never|without|very|too|slow|sometimes|intermittent|"
    r"returns?|shows?|reports?|working|stuck|missing|late|delayed|delay|slow|"
    r"blurry|blank|wrong|broken|low|high|fast|laggy|jittery|failing|failed)\b"
)


def is_structural_pending_verification(item: Any) -> bool:
    """True only for the whitelisted wording of a real-device/production/acceptance item.

    The defect/observation word lists stay as a second safety net; the main
    path is the sentence template.  Anything not judgeable is False.
    """
    if not isinstance(item, str) or not item.strip():
        return False
    text = " ".join(item.strip().lower().split())
    if _pv_reports_defect(text):
        return False
    if _PV_SCRIPT_TEMPLATE.fullmatch(text):
        return True
    zh = _PV_ZH_TEMPLATE.fullmatch(text)
    if zh:
        noun = zh.group("noun")
        return not (
            any(word in noun for word in _PV_NOUN_EXCLUDED_ZH)
            or noun.endswith(_PV_NOUN_STATE_SUFFIX_ZH)
            or _PV_NUMBER_ZH.search(noun)
        )
    en = _PV_EN_TEMPLATE.fullmatch(text)
    if en:
        return not _PV_NOUN_EXCLUDED_EN.search(en.group("noun"))
    return False


def _pv_core_tokens(item: str) -> frozenset:
    text = item.lower()
    for filler in sorted(_PV_FILLER, key=len, reverse=True):
        text = text.replace(filler, " ")
    tokens = set()
    for chunk in re.findall(r"[a-z][a-z-]*|[\u4e00-\u9fff]+", text):
        if chunk[0].isascii():
            tokens.add(chunk)
        elif len(chunk) == 1:
            tokens.add(chunk)
        else:
            tokens.update(chunk[index:index + 2] for index in range(len(chunk) - 1))
    return frozenset(tokens)


def same_pending_verification(first: Any, second: Any) -> bool:
    """Whether two structural items are one matter reported in different words."""
    if not (is_structural_pending_verification(first)
            and is_structural_pending_verification(second)):
        return False
    if first.strip() == second.strip():
        return True
    left, right = _pv_core_tokens(first), _pv_core_tokens(second)
    if not left or not right:
        return False
    # Extra words on one side must only name the environment; any other extra
    # word (e.g. a different feature) makes it a different matter.
    extras = (left ^ right)
    return (left <= right or right <= left) and all(
        any(token in marker for marker in _PV_ENVIRONMENT_MARKERS) for token in extras
    )

#: Disclosure recorded when a node is dispatched without a visible bridge.
BACKGROUND_TOPOLOGY_DISCLOSURE = (
    "平台无可见任务桥：降级为 background subagent，不可见、不可直接进入、返工续接受限"
)

#: Identities reserved for the supervisor itself.  A node whose writer
#: resolves to one of these is structurally rejected: the monitor must never
#: become the writer of any node, regardless of authorization-card text.
_SUPERVISOR_WRITER_IDENTITIES = frozenset(
    {"monitor", "vibeguide_monitor", "vibe_guide_monitor", "supervisor", "vibe_supervisor"}
)


def reconcile_pending_binding(snapshot: Any, node_id: str, runner: Any) -> bool:
    """Reconcile a provider setup placeholder without creating a successor.

    A ``clientThreadId`` is only a setup handle.  Until the provider returns an
    exact real task identity this function keeps the existing generation and
    writer lease, marks the node retryable, and returns ``False``.
    """
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else getattr(snapshot, "nodes", None)
    if not isinstance(nodes, dict) or not isinstance(node_id, str):
        return False
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return False
    binding = node.get("binding") or node.get("active_binding") or node.get("task")
    client_id = None
    if isinstance(binding, dict):
        client_id = binding.get("clientThreadId") or binding.get("client_thread_id")
    client_id = client_id or node.get("clientThreadId") or node.get("client_thread_id")
    thread_id = binding.get("threadId") if isinstance(binding, dict) else node.get("threadId")
    if not client_id or thread_id:
        return False
    # Only a verified provider locate/list operation may promote the identity.
    try:
        resolver = getattr(runner, "resolve_pending", None)
        resolved = resolver(binding) if callable(resolver) else None
    except Exception:
        resolved = None
    if not isinstance(resolved, dict) and resolved is not None and hasattr(resolved, "__dict__"):
        resolved = dict(resolved.__dict__)
    if isinstance(resolved, dict) and resolved.get("threadId"):
        expected_host = binding.get("hostId") or binding.get("host") if isinstance(binding, dict) else None
        observed_host = resolved.get("hostId") or resolved.get("host")
        required_identity = ("provider", "status", "clientThreadId", "issue_id", "role", "generation")
        if any(resolved.get(key) in (None, "") for key in required_identity) or not expected_host or not observed_host:
            resolved = None
        elif resolved.get("clientThreadId") != client_id:
            resolved = None
        if resolved is None:
            pass
        elif not isinstance(binding, dict) or not binding.get("provider") or resolved.get("provider") != binding.get("provider"):
            resolved = None
        elif expected_host and observed_host != expected_host:
            resolved = None
        elif isinstance(binding, dict) and resolved.get("clientThreadId") != client_id:
            # A locate result for a different setup request must never be
            # promoted in place: doing so would silently change the writer
            # identity while retaining the old generation and lease.
            resolved = None
        elif isinstance(binding, dict) and resolved.get("issue_id") != binding.get("issue_id"):
            resolved = None
        elif isinstance(binding, dict) and resolved.get("role") != binding.get("role"):
            resolved = None
        elif isinstance(binding, dict) and resolved.get("generation") != binding.get("generation"):
            resolved = None
        elif resolved.get("status") not in ("verified", "visible"):
            resolved = None
    if isinstance(resolved, dict) and resolved.get("threadId"):
        target = binding if isinstance(binding, dict) else node
        target["threadId"] = resolved["threadId"]
        target["clientThreadId"] = client_id
        return True
    node["status"] = "retry_pending"
    node["retryable_action"] = {"action": "reconcile_pending_binding", "clientThreadId": client_id}
    node.setdefault("generation", 1)
    node["successor"] = None
    return False


def _user_status(record: Dict[str, Any]) -> str:
    """Keep internal recovery detail behind the four public statuses."""
    return map_user_status(record)


def classify_observation(observation: Any) -> ObservationDisposition:
    """Classify provider drift without claiming unavailable capability."""
    if not isinstance(observation, dict):
        return ObservationDisposition("unknown", "isolate", "observation is malformed")
    kind = str(observation.get("kind", observation.get("type", "unknown"))).strip().lower()
    repairable = {"worktree_branch_drift", "worktree_drift", "branch_drift", "stale_cursor"}
    uncertain = {
        "missing_visible_locate_evidence", "provider_timeout", "timeout",
        "permission_denied", "missing_locate_evidence", "unknown",
    }
    if kind in repairable and observation.get("repairable", True) is not False:
        return ObservationDisposition("repairable", "repair", kind)
    if kind in uncertain or not kind:
        return ObservationDisposition("degraded", "isolate", kind or "unknown observation")
    return ObservationDisposition("unknown", "isolate", kind)


def classify_provider_failure(observation: Any) -> Dict[str, str]:
    """Classify provider outcomes without turning missing evidence into failure.

    Provider bridges expose a mixture of structured observations and exception
    text.  Transient/repairable outcomes are engineering conditions and may be
    retried on the same task identity.  Only structured user/system
    authorization fields are external boundaries; free-form wording cannot
    create one.  Plain-text "permission denied" / "authorization digest"
    errors are engineering failures and repairable (auto-recoverable); free-form "permission
    required" / "credential required" / "login required" stay fail-closed
    ``binding_unknown``.  Anything else remains ``unknown`` so callers fail closed.
    """
    if isinstance(observation, BaseException) and isinstance(
        getattr(observation, "observation", None), dict
    ):
        observation = observation.observation
    class _ClassificationKind(str):
        """Current V4.5 kind with equality aliases for V4.2 callers."""
        _aliases = {
            "retryable": "engineering",
            "capacity_wait": "engineering",
            "repairable": "engineering",
            "external_decision": "external",
            "binding_unknown": "unknown",
        }
        def __eq__(self, other):
            return str.__eq__(self, other) or self._aliases.get(str(self)) == other

        __hash__ = str.__hash__

    def _result(kind: str, legacy: str) -> Dict[str, str]:
        return {"kind": _ClassificationKind(kind), "legacy_kind": legacy}

    structured_external = {
        "external_decision", "external_authorization_required",
        "credential_required", "login_required", "permission_required",
        "product_scope_change", "deploy_required",
        "irreversible_action_required", "system_permission_required",
    }
    if isinstance(observation, BaseException):
        text = str(observation)
        kind = getattr(observation, "kind", None) or getattr(observation, "type", None)
    elif isinstance(observation, dict):
        values = [observation.get(key) for key in ("kind", "type", "status", "reason", "error", "message", "detail")]
        text = " ".join(str(value) for value in values if value is not None)
        kind = observation.get("kind") or observation.get("type")
        if (
            observation.get("credential_required")
            or observation.get("login_required")
            or observation.get("permission_required")
            or observation.get("permission_denied")
        ):
            return _result("external_decision", "external")
    else:
        text = str(observation or "")
        kind = None
    normalized = (str(kind or "") + " " + text).casefold()
    if str(kind or "").strip().casefold() in structured_external:
        return _result("external_decision", "external")
    binding_markers = (
        "identity conflict", "identity mismatch", "writer conflict", "lease conflict",
        "binding conflict", "binding mismatch", "task identity", "contract digest",
        "permission required", "credential required",
        "login required", "system permission",
        "身份冲突", "租约冲突", "绑定冲突",
    )
    if any(marker in normalized for marker in binding_markers):
        return _result("binding_unknown", "unknown")
    # A bound provider observation can carry a recovery class even when its
    # message is new. Never let this hint override authorization or identity
    # conflicts; it schedules reconciliation, not acceptance or availability.
    if isinstance(observation, dict):
        hint = observation.get("reason_class")
        if isinstance(hint, str) and hint in {"engineering", "retryable", "capacity_wait", "repairable"}:
            return _result("retryable" if hint == "engineering" else hint, "engineering")
    # Native task creation can reject a model/reasoning combination before a
    # task identity exists.  This is a provider request-shape problem: the
    # monitor may retry the same role/generation after adapting the optional
    # reasoning field, and must not turn it into a durable DAG block.
    parameter_markers = (
        "could not validate reasoning",
        "reasoning effort",
        "invalid reasoning",
        "unsupported reasoning",
        "reasoning level",
        "invalid model",
        "unsupported model",
        "model/reasoning",
        "model and reasoning",
        "参数校验",
        "思考程度",
        "模型参数",
    )
    if any(marker in normalized for marker in parameter_markers):
        return _result("retryable", "engineering")
    capacity_markers = ("429", "rate limit", "too many requests", "capacity", "quota", "限流", "容量", "配额")
    if any(marker in normalized for marker in capacity_markers):
        return _result("capacity_wait", "engineering")
    retryable_markers = (
        "task-create", "task create", "task creation", "create task failed", "创建任务失败",
        "timeout", "timed out", "disconnect", "disconnected", "connection reset",
        "empty response", "no response", "pending", "clientthreadid", "setup",
        "worker exit", "worker exited", "worker crash", "snapshot interruption",
        "snapshot interrupted", "interrupted", "temporarily unavailable",
        "provider unavailable", "超时", "断开", "空响应", "创建中", "工作进程退出", "快照中断",
    )
    if any(marker in normalized for marker in retryable_markers):
        return _result("retryable", "engineering")
    repairable_markers = (
        "dirty checkout", "detached head", "detached", "branch drift", "worktree drift",
        "stale cursor", "dirty 工作树", "游离头", "分支漂移", "工作树漂移", "游标过期",
        "contract test execution failed", "invariant test failed", "合同测试失败",
        # Local-evidence-recoverable (AC-B2).  Kept out of retryable_markers so
        # the provider parameter-recovery gate (kind == "retryable") never
        # treats a rejected create as licence for a second task.
        "permission denied", "authorization digest",
    )
    if any(marker in normalized for marker in repairable_markers):
        return _result("repairable", "engineering")
    engineering_markers = (
        "timeout", "timed out", "429", "rate limit", "too many requests",
        "disconnect", "disconnected", "connection reset", "empty response",
        "no response", "pending", "clientthreadid", "setup", "worker exit",
        "worker exited", "worker crash", "snapshot interruption", "snapshot interrupted",
        "interrupted", "binding drift", "branch drift", "worktree drift",
        "stale cursor", "temporarily unavailable", "provider unavailable",
        "超时", "限流", "断开", "空响应", "创建中", "工作进程退出", "快照中断",
    )
    if any(marker in normalized for marker in engineering_markers):
        return _result("retryable", "engineering")
    return _result("binding_unknown", "unknown")


def recovery_action_for_failure(observation: Any) -> str:
    """Return the bounded engineering recovery entry for an observation.

    The action is a routing hint only.  ``reconcile_binding`` is deliberately
    separate from automatic retry so an unknown task identity can never be
    dispatched blindly.
    """
    classification = classify_provider_failure(observation)
    kind = str(classification.get("kind"))
    if kind == "repairable":
        return "repair_node"
    if kind in {"retryable", "capacity_wait"}:
        return "requeue_same_task"
    if kind == "external_decision":
        return "await_external_decision"
    return "reconcile_binding"


#: Provider-error wording that moves dispatch to the next model in the
#: profile's fallback chain (quota exhausted / rate limit / model unavailable).
_MODEL_FAILURE_QUOTA_MARKERS = (
    "quota", "额度", "insufficient balance", "credits exhausted",
    "balance exhausted", "usage limit",
)
_MODEL_FAILURE_RATE_MARKERS = ("rate limit", "rate-limit", "too many requests", "429", "限流")
_MODEL_FAILURE_UNAVAILABLE_MARKERS = (
    "model unavailable", "unsupported model", "invalid model", "model not found",
    "no such model", "unknown model", "model disabled", "模型不可用", "不支持的模型",
)


def model_failure_reason_class(text: Any) -> Optional[str]:
    """Classify provider error text as a model-chain failure class, else ``None``.

    Only quota/rate-limit/model-unavailable conditions justify switching
    models; anything else (binding, worktree, plain timeouts) keeps the
    existing recovery and never consumes the chain.
    """
    normalized = str(text or "").casefold()
    if not normalized.strip():
        return None
    if any(marker in normalized for marker in _MODEL_FAILURE_QUOTA_MARKERS):
        return "quota_exhausted"
    if any(marker in normalized for marker in _MODEL_FAILURE_RATE_MARKERS):
        return "rate_limit"
    if any(marker in normalized for marker in _MODEL_FAILURE_UNAVAILABLE_MARKERS):
        return "model_unavailable"
    return None


#: #256 ③: the ``recovery_action`` values a rescue may treat as the defect's
#: own residue.  ``_mark_blocked_unknown`` derives the action from the failure
#: reason's keywords, so a block that is *not* a defect also lands on
#: ``reconcile_binding`` (reverse-block text is one such case) -- but any other
#: action names a live recovery route somebody is already walking, and must
#: keep vetoing.
DEFECT_ADJACENT_RECOVERY_ACTIONS = frozenset({None, "reconcile_binding"})


def defect_adjacent_marker_allows_rescue(recovery_action: Any, quarantine: Any) -> bool:
    """Whether the #256 defect-adjacent markers permit a rescue.

    ``_mark_blocked_unknown`` always stamps ``recovery_action`` plus a
    ``quarantine`` object holding the handle the block retired.  For the
    reverse-block residue that pair is ``reconcile_binding`` and
    ``{"handle_id": None}``: it is the bug's fingerprint, not live work.
    Treating either field as "somebody is still working on this" vetoes every
    rescue of a genuinely stuck node, so the criterion is narrow:

    * ``recovery_action`` must be absent or the block's own
      ``reconcile_binding``;
    * a ``quarantine`` still bound to a non-null ``handle_id`` is live work;
    * a ``quarantine`` that is neither absent nor a mapping is malformed.

    Fail-closed by construction: only the program-authored shapes above pass.
    """
    if recovery_action not in DEFECT_ADJACENT_RECOVERY_ACTIONS:
        return False
    if quarantine is None:
        return True
    if not isinstance(quarantine, dict):
        return False
    return quarantine.get("handle_id") is None


@dataclass(frozen=True)
class BindingFacts:
    """A read-only copy of one binding's identity, taken at capture time.

    ``_load_task_binding`` returns a shared/cached ``TaskBinding`` and both
    ``_set_binding_status`` and ``_archive_pair`` mutate its ``status`` in
    place.  A rollback that re-reads the object therefore sees the
    post-mutation value and turns itself into a no-op -- exactly the #249
    ``019a619`` trace.  Gates capture this copy before the first mutation and
    roll back from it (#256 ①).
    """

    role: str
    status: Optional[str]
    task_id: Optional[str]
    generation: Optional[int]

    def __post_init__(self) -> None:
        # ``frozen=True`` blocks attribute rebinding but not a field that holds
        # a mutable list/dict, which would make "read-only copy" a convention
        # instead of a property.  Pinning the field types keeps it executable.
        for name in ("status", "task_id", "generation"):
            value = getattr(self, name)
            if not isinstance(value, (str, int, type(None))):
                raise TypeError(
                    "binding fact {} must be a scalar, not {}".format(
                        name, type(value).__name__
                    )
                )

    @classmethod
    def capture(cls, role: str, binding: Any) -> "BindingFacts":
        return cls(
            role=role,
            status=getattr(binding, "status", None),
            task_id=getattr(binding, "task_id", None),
            generation=getattr(binding, "generation", None),
        )


@dataclass(frozen=True)
class RecoveryFacts:
    """The facts a recovery or rollback gate may decide on (#256 ①).

    Two rules make the decision reproducible:

    1. **Structural fields only.**  Every field is read from the snapshot node
       and deep-copied at capture time, so a later mutation of the live node
       mapping cannot change what the gate decided on.  The provider-text
       ``reason`` is carried solely so a rollback can put it back -- no gate
       may compare it, because persistence redacts it (``RECOVERY_SIGNATURE_KEYS``).
    2. **Unreadable facts fail closed.**  If a signature fact reads back as a
       redaction placeholder, ``unreadable`` names it and the gate refuses:
       comparing against ``[REDACTED_PROVIDER_TEXT]`` can only produce the
       silent miss this contract exists to prevent.

    Bindings are captured separately (``BindingFacts``) because they live in
    the task registry and are shared mutable objects.
    """

    node_id: str
    status: Optional[str]
    contract_digest: Optional[str]
    acceptance: Optional[Dict[str, Any]]
    review_clearance: Optional[Dict[str, Any]]
    recovery_action: Any
    quarantine: Any
    pair_archived: bool
    active_task: Any
    active_role: Any
    start_intent: Any
    retryable_action: Any
    handle_registered: bool
    reviewer_identity: Optional[str]
    review_generation: Any
    reason: Any

    #: ``(field, snapshot key)`` for every fact the gate decides on.  Each key
    #: must be declared structural in ``state.RECOVERY_SIGNATURE_KEYS``;
    #: ``__post_init__`` refuses a capture that would decide on a key
    #: persistence may redact.  ``reason`` is deliberately absent: it is
    #: provider text carried only so a rollback can restore it, never compared.
    SIGNATURE_FIELDS = (
        ("status", "status"),
        ("contract_digest", "contract_digest"),
        ("acceptance", "acceptance"),
        ("review_clearance", "review_clearance"),
        ("recovery_action", "recovery_action"),
        ("quarantine", "quarantine"),
        ("pair_archived", "pair_archived"),
        ("active_task", "active_task"),
        ("active_role", "active_role"),
        ("start_intent", "start_intent"),
        ("retryable_action", "retryable_action"),
        ("reviewer_identity", "reviewer_identity"),
        ("review_generation", "review_generation"),
    )

    def __post_init__(self) -> None:
        stray = [
            key
            for _field, key in self.SIGNATURE_FIELDS
            if key not in RECOVERY_SIGNATURE_KEYS
        ]
        if stray:
            raise ValueError(
                "recovery facts must be structural: " + ", ".join(sorted(stray))
            )

    @classmethod
    def capture(
        cls, node_id: str, node: Dict[str, Any], handle_registered: bool = False
    ) -> "RecoveryFacts":
        values = {
            field: copy.deepcopy(node.get(key)) for field, key in cls.SIGNATURE_FIELDS
        }
        values["pair_archived"] = bool(values["pair_archived"])
        return cls(
            node_id=node_id,
            handle_registered=bool(handle_registered),
            reason=copy.deepcopy(node.get("reason")),
            **values,
        )

    @property
    def acceptance_contract_digest(self) -> Any:
        acceptance = self.acceptance if isinstance(self.acceptance, dict) else {}
        return acceptance.get("contract_digest")

    @property
    def authorization_epoch(self) -> Any:
        acceptance = self.acceptance if isinstance(self.acceptance, dict) else {}
        return acceptance.get("authorization_epoch")

    @property
    def unreadable(self) -> Tuple[str, ...]:
        """Signature facts that read back as a redaction placeholder."""
        candidates = (
            ("status", self.status),
            ("contract_digest", self.contract_digest),
            ("acceptance.contract_digest", self.acceptance_contract_digest),
            ("acceptance.authorization_epoch", self.authorization_epoch),
            ("recovery_action", self.recovery_action),
            ("reviewer_identity", self.reviewer_identity),
        )
        return tuple(name for name, value in candidates if is_redacted_value(value))


#: #256 ④: same-type audit of the three call surfaces issue #256 names.  Each
#: entry records the callers inspected and one of two verdicts:
#: ``same_type_fixed`` -- the caller had the distortion and now decides on
#: captured facts, or ``different_type`` -- the caller cannot reach the
#: distortion, for the stated reason.  Kept as data (not prose in three
#: docstrings) so tests/test_recovery_fidelity_256.py can pin coverage: every
#: named site must be present with a verdict and a rationale, and
#: ``call_sites`` -- the number of ``<receiver>.<site>(`` call expressions in
#: this module, counted by AST in that test -- must match the real call count.
#: Call sites inside ``except`` branches are counted too.
RECOVERY_FIDELITY_AUDIT = (
    {
        "site": "_archive_pair",
        "call_sites": 3,
        "callers": (
            {
                "caller": "_recover_reverse_blocked_acceptance",
                "verdict": "same_type_fixed",
                "rationale": (
                    "The only caller with a rollback.  _archive_pair flips the "
                    "shared binding objects to archived in place, so the pass "
                    "captures BindingFacts before the first mutation and rolls "
                    "back from those copies instead of re-reading .status."
                ),
            },
            {
                "caller": "_apply_event",
                "verdict": "different_type",
                "rationale": (
                    "No rollback reads binding state: the durable accepted "
                    "event is recorded before any mutation and "
                    "_reconcile_unapplied_events re-archives both bindings "
                    "from it, so a crash between them is repaired by replay."
                ),
            },
            {
                "caller": "_accept_visible_sdd_delivery",
                "verdict": "different_type",
                "rationale": (
                    "Same ordering as _apply_event (accepted event first, then "
                    "archive) and the visible-sdd replay archives the developer "
                    "binding itself; nothing reads a mutated status back."
                ),
            },
        ),
    },
    {
        "site": "_release_node_lease",
        "call_sites": 26,
        "callers": (
            {
                "caller": "_recover_reverse_blocked_acceptance",
                "verdict": "same_type_fixed",
                "rationale": (
                    "Released before any lifecycle mutation inside the guarded "
                    "block, so a failure leaves the signature intact and the "
                    "next pass retries; no state is read after the unlink."
                ),
            },
            {
                "caller": (
                    "_apply_event, _start_task, reauthorize, _schedule_ready "
                    "helpers, _reconcile_unapplied_events and the remaining "
                    "terminal-transition callers (the other 25 sites, including the #290 contract_error replay that re-plans an integration-review node, the #335 _park_blocked_engineering / blocked_engineering replay pair that parks a blocked self-report, and the #336 landing-unreadable replay twin)"
                ),
                "verdict": "different_type",
                "rationale": (
                    "_release_node_lease itself reads only the node worktree "
                    "and run id (structural), and every caller uses it as a "
                    "terminal side effect: none wraps it in a rollback that "
                    "then reads a binding status or a reason."
                ),
            },
        ),
    },
    {
        "site": "_mark_blocked_unknown",
        "call_sites": 53,
        "callers": (
            {
                "caller": (
                    "_apply_event, _start_task, _schedule_ready, "
                    "_accept_visible_sdd_delivery and the remaining block "
                    "sites (the other 46 sites)"
                ),
                "verdict": "different_type",
                "rationale": (
                    "Callers pass a reason and never read the stamps back as "
                    "evidence; the block is fail-closed by construction."
                ),
            },
            {
                "caller": (
                    "_start_task (workspace reconciliation / platform handoff "
                    "predecessor unproven), _apply_event (same guard "
                    "and the integration reviewer write violation)"
                ),
                "verdict": "different_type",
                "rationale": (
                    "#289/#290 new sites: each only parks the node fail-closed "
                    "on a structural flag or an unproven predecessor and "
                    "returns; none reads the stamps back as evidence or "
                    "rolls anything back from them."
                ),
            },
            {
                "caller": "_recover_quarantined_delivered_developer",
                "verdict": "different_type",
                "rationale": (
                    "Requires a quarantine mapping, but only as a precondition "
                    "corroborated by a delivered developer binding with matching "
                    "identity and generation -- the marker alone never unlocks "
                    "recovery."
                ),
            },
            {
                "caller": "_schedule_ready (quarantined no-binding branch)",
                "verdict": "different_type",
                "rationale": (
                    "Reads the quarantine marker to park the node for manual "
                    "repair -- a fail-closed direction, and the #249 rescue "
                    "that must tolerate the marker runs before it."
                ),
            },
            {
                "caller": "repair_node (reads the stamp, not a caller)",
                "verdict": "different_type",
                "rationale": (
                    "Not a caller: it re-classifies from the persisted "
                    "(redacted) reason at monitor.py repair_node, so after a "
                    "restart it always lands on the binding_unknown branch. "
                    "Fail-closed and pre-existing; recorded as out-of-scope "
                    "residual for #256 follow-up, not changed here."
                ),
            },
        ),
    },
)


def self_heal_binding(snapshot: Any, node_id: str, observation: Any) -> HealingResult:
    """Repair only fields proven by the frozen node contract, in place.

    This deliberately accepts a plain snapshot mapping so recovery and replay
    callers can use the same fail-closed operation before persistence.
    """
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else getattr(snapshot, "nodes", None)
    if not isinstance(nodes, dict):
        return HealingResult(False, degraded=True, isolated=True, reason="snapshot is malformed")
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return HealingResult(False, degraded=True, isolated=True, reason="node is unknown")
    disposition = classify_observation(observation)
    if disposition.status != "repairable" or not isinstance(observation, dict):
        isolate_affected_action(snapshot, node_id, disposition.reason)
        return HealingResult(False, degraded=True, isolated=True, reason=disposition.reason,
                             task_id=node.get("task_id") or node.get("developer_identity"))
    # Current facts may identify drift, but never become the new contract.
    expected = node.get("binding")
    if not isinstance(expected, dict):
        expected = node.get("frozen_binding")
    if not isinstance(expected, dict):
        expected = node.get("binding_contract")
    if not isinstance(expected, dict) and isinstance(node.get("contract"), dict):
        nested = node["contract"].get("binding")
        expected = nested if isinstance(nested, dict) else node["contract"]
    if not isinstance(expected, dict):
        isolate_affected_action(snapshot, node_id, "frozen binding evidence is missing")
        return HealingResult(False, degraded=True, isolated=True,
                             reason="frozen binding evidence is missing",
                             task_id=node.get("task_id") or node.get("developer_identity"))
    for field in ("task_id", "worktree", "branch", "allowlist", "cursor",
                  "token", "continuation_token", "continuation_digest"):
        if field in expected:
            node[field] = expected[field]
    node["healing"] = {"status": "repaired", "observation_kind": disposition.reason}
    return HealingResult(True, reason=disposition.reason, task_id=node.get("task_id"))


def isolate_affected_action(snapshot: Any, node_id: str, reason: str) -> None:
    """Quarantine one node/action while leaving all other nodes untouched."""
    nodes = snapshot.get("nodes") if isinstance(snapshot, dict) else getattr(snapshot, "nodes", None)
    if not isinstance(nodes, dict):
        return
    node = nodes.get(node_id)
    if not isinstance(node, dict):
        return
    node["status"] = "blocked_unknown"
    node["reason"] = str(reason)
    node["isolated"] = True
    node["retryable_action"] = None


class Monitor:
    # Platform dispatch-topology rulings (adapter_id -> ruling), normally
    # filled by ``__init__`` from the ISSUE-07 probe matrix.  Declared at
    # class level as well because partially constructed instances are a
    # supported shape in this codebase -- the tests build a Monitor with
    # ``Monitor.__new__(Monitor)`` and assign only the attributes the case
    # needs.  A missing ruling map must stay fail-closed (conservative
    # ``dual-visible``) rather than raise, so the default is the empty map.
    # ``__init__`` rebinds it to a fresh per-instance dict, so instances built
    # the normal way never share this object.
    _topology_rulings: Dict[str, str] = {}

    @staticmethod
    def _prospective_intent_digest(snapshot: RunSnapshot, node_id: str, role: str, generation: int, current: Dict[str, Any]) -> str:
        payload = {"run_id": snapshot.run_id, "node_id": node_id, "role": role, "generation": generation, "task_id": current.get(role + "_identity") or f"{role}:{node_id}", "writer": current.get("writer") or current.get("worker") or role, "worktree": current.get("worktree", ""), "branch": current.get("branch", ""), "authorization_digest": snapshot.authorization_digest, "node_contract_digest": current.get("contract_digest") or snapshot.node_contract_digest}
        return digests.sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())

    @staticmethod
    def _start_intent_digest(snapshot: RunSnapshot, node_id: str, role: str, task_id: str, generation: int, contract: Dict[str, Any], current: Dict[str, Any]) -> str:
        payload = {"run_id": snapshot.run_id, "node_id": node_id, "role": role, "task_id": task_id, "generation": generation, "writer": contract.get("writer") or current.get("worker"), "worktree": contract.get("worktree") or current.get("worktree"), "branch": contract.get("branch") or current.get("branch"), "authorization_digest": snapshot.authorization_digest, "node_contract_digest": current.get("contract_digest") or getattr(snapshot, "node_contract_digest", "")}
        return digests.sha256_bytes(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())

    def _dispatch_with_intent(self, runner: Runner, contract: Dict[str, Any], intent: Any):
        """Dispatch one provider request and accept only a matching proof.

        The provider response must be structured; prose or a partial envelope
        is retained as a retry/binding observation and never confirmed.
        """
        payload = dict(contract)
        # The immutable intent is a V3.9/V4.4 binding contract field.  Do not
        # inject a plain JSON intent into legacy contracts: its mere presence
        # opts the provider runner into the protected binding gate, where the
        # downgraded mapping is correctly rejected as untrusted evidence.
        if binding_contract_enabled(contract):
            payload["binding_intent"] = (
                intent.to_dict() if hasattr(intent, "to_dict") else dict(intent)
            )
        try:
            worktree = Path(str(payload.get("worktree", ".")))
            project_root = payload.get("project_root")
            if not worktree.is_absolute() and isinstance(project_root, str) and project_root:
                worktree = Path(project_root) / worktree
            handle = runner.start(payload, worktree)
        except ProviderUnavailable as error:
            # Preserve the provider's actual request-shape diagnosis.  A
            # broad catch here used to erase it as ``ProviderUnavailable``;
            # the scheduler then treated every model/parameter rejection as
            # an unknown binding and stopped the DAG.
            failure = classify_provider_failure(error)
            error_text = str(error).casefold()
            if "worker model route conflicts" in error_text or "worker model routing" in error_text:
                reason_code = "worker_profile_conflict"
            elif "child binding" in error_text or "task binding" in error_text:
                reason_code = "child_binding_conflict"
            else:
                reason_code = None
            if reason_code is not None:
                action_basis = payload.get("binding_intent") or (
                    intent.to_dict() if hasattr(intent, "to_dict") else dict(intent)
                )
                action_ref = "dispatch-" + digests.sha256_bytes(
                    json.dumps(action_basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
                )[:24]
                return {
                    "kind": "contract_error",
                    "reason": "provider dispatch contract rejected",
                    "reason_code": reason_code,
                    "phase": "start",
                    "action_ref": action_ref,
                    "advance_generation": False,
                    "intent": action_basis,
                }
            named = getattr(error, "observation", None)
            fixed = (
                {"reason_code": named["reason_code"], "worktree": named.get("worktree")}
                if isinstance(named, dict) and isinstance(named.get("reason_code"), str) else {}
            )
            return {
                "kind": str(failure["kind"]),
                "reason_class": str(failure["kind"]),
                "reason": redact_provider_text(str(error)),
                "error_type": type(error).__name__,
                # Computed from the raw error: the redacted ``reason`` no
                # longer carries the model-failure markers.  Internal routing
                # only; never displayed.
                "model_failure_class": model_failure_reason_class(str(error)),
                **fixed,
                "intent": payload.get("binding_intent") or (
                    intent.to_dict() if hasattr(intent, "to_dict") else dict(intent)
                ),
            }
        except Exception as error:
            text = str(error).casefold()
            if "worker model route conflicts" in text or "worker model routing" in text:
                reason_code = "worker_profile_conflict"
            elif "child binding" in text or "task binding" in text:
                reason_code = "child_binding_conflict"
            elif isinstance(error, (ValueError, TypeError)):
                reason_code = "dispatch_contract_invalid"
            else:
                reason_code = "provider_unavailable"
            action_basis = payload.get("binding_intent") or (
                intent.to_dict() if hasattr(intent, "to_dict") else dict(intent)
            )
            action_ref = "dispatch-" + digests.sha256_bytes(
                json.dumps(action_basis, sort_keys=True, separators=(",", ":")).encode("utf-8")
            )[:24]
            return {
                "kind": "contract_error" if reason_code != "provider_unavailable" else "retryable",
                "reason": "provider dispatch contract rejected",
                "reason_code": reason_code,
                "phase": "start",
                "action_ref": action_ref,
                "advance_generation": False,
                "model_failure_class": model_failure_reason_class(str(error)),
                "intent": action_basis,
            }
        # Existing runners expose RunHandle; only V4.4 contracts require
        # structured proof validation.  The call still traverses this helper.
        if contract.get("binding_contract_version") != "4.4" and hasattr(handle, "run_id"):
            return handle
        response = handle if isinstance(handle, dict) else getattr(handle, "__dict__", None)
        if not isinstance(response, dict):
            return {"kind": "binding_unknown", "reason": "provider response is unstructured", "intent": payload["binding_intent"]}
        expected = payload.get("binding_intent") or (
            intent.to_dict() if hasattr(intent, "to_dict") else dict(intent)
        )
        strict_v44 = contract.get("binding_contract_version") == "4.4"
        if strict_v44 and (not isinstance(payload.get("provider"), str) or not payload.get("provider") or not isinstance(payload.get("host") or payload.get("host_id"), str) or not (payload.get("host") or payload.get("host_id"))):
            return {"kind": "binding_unknown", "reason": "provider/host proof expectation is missing", "intent": expected}
        checks = {"provider": payload.get("provider"), "host": payload.get("host") or payload.get("host_id"),
                  "task_id": expected.get("task_id"), "issue_id": expected.get("node_id"), "role": expected.get("role"),
                  "generation": expected.get("generation"), "worktree": expected.get("worktree"),
                  "branch": expected.get("branch"), "writer": expected.get("writer"),
                  "authorization_digest": expected.get("authorization_digest"),
                  "node_contract_digest": expected.get("node_contract_digest")}
        # Cursor is runtime proof; it may be absent only while a provider
        # reports an explicitly pending setup, never on a confirmed start.
        required = dict(checks)
        required["cursor"] = None
        if strict_v44 and any(key not in response or not response.get(key) for key in required if key != "cursor"):
            return {"kind": "binding_unknown", "reason": "provider proof field missing", "intent": expected, "proof": response}
        if any(key not in response or (value is not None and response.get(key) != value) for key, value in checks.items()) or not response.get("cursor"):
            return {"kind": "binding_unknown", "reason": "provider proof mismatch", "intent": expected, "proof": response}
        return response

    def _v42_retry_status(self) -> str:
        """Use the explicit retry phase only for V4.2 initialized projects.

        Older snapshots retain their historical ``blocked_unknown`` label;
        V4.2 exposes the durable engineering phase as ``retry_pending``.
        """
        try:
            state_path = self.paths.vibe / "state.json"
            if state_path.is_file():
                state = json.loads(state_path.read_text(encoding="utf-8"))
                if isinstance(state, dict) and state.get("workflow_version") == 4:
                    return "retry_pending"
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
        return "blocked_unknown"

    @staticmethod
    def _lineage_matches_snapshot(snapshot: RunSnapshot, prd_digest: str, spec_digest: str) -> bool:
        return bool(prd_digest and spec_digest and snapshot.prd_digest == prd_digest and snapshot.spec_digest == spec_digest)

    def _execution_engine_binding(self, record: AuthorizationRecord) -> Tuple[str, str]:
        """Require the V4.1 complex plan to run through the DAG Monitor."""
        if getattr(self.plan, "complexity_band", "") != "complex":
            return "", ""
        override = getattr(record, "explicit_execution_mode_override", None)
        if isinstance(override, dict) and override:
            if not all(isinstance(override.get(key), str) and override.get(key).strip() for key in ("original_instruction", "alternative_mode")):
                raise PermissionError("execution_engine_unverified: malformed explicit execution override")
            raise PermissionError("execution_engine_unverified: explicit execution mode override has no V4.1 DAG guarantees")
        if record.execution_engine != "vibeguide_monitor" or record.engine_mode != "dag":
            raise PermissionError("execution_engine_unverified: V4.1 complex runs require vibeguide_monitor DAG")
        if not record.engine_evidence_ref or record.engine_evidence_ref.startswith("unverified:"):
            raise PermissionError("execution_engine_unverified: engine evidence is not verified")
        if record.dag_revision != self.plan.version:
            raise PermissionError("blocked_by_execution_topology_mismatch: DAG revision mismatch")
        # The authorization card's reference must resolve to fresh, intact
        # engine evidence before any provider dispatch is considered.  This
        # check is deliberately separate from authorization validation: an
        # attestation can prove engine identity, never grant execution rights.
        try:
            raw_path = self.paths.vibe / "plans" / self.plan.plan_id / "engine-attestation.json"
            if any(part.is_symlink() for part in (self.paths.vibe / "plans", self.paths.vibe / "plans" / self.plan.plan_id, raw_path)):
                raise ValueError("engine attestation must be a regular file")
            path = self.paths.resolve_vibe_path(
                Path("plans") / self.plan.plan_id / "engine-attestation.json"
            )
            if path.is_symlink() or not path.is_file():
                raise ValueError("engine attestation must be a regular file")
            attestation = json.loads(path.read_text(encoding="utf-8"))
            validate_engine_attestation(
                attestation, self.plan.plan_id, self.plan.version,
                now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            )
            if attestation.get("evidence_ref") != record.engine_evidence_ref:
                raise ValueError("engine evidence reference mismatch")
            if record.agent_id and attestation.get("provider") != record.agent_id:
                raise ValueError("engine attestation provider mismatch")
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            raise PermissionError(
                "execution_engine_unverified: engine attestation is missing or invalid"
            ) from error
        return record.execution_engine, record.engine_mode

    def _topology_projection(self, snapshot: RunSnapshot) -> Tuple[List[str], str]:
        """Return the current ready-set and a stable dependency projection digest."""
        expected = []
        for node in self.nodes.values():
            expected.append({"id": node.id, "depends_on": list(node.depends_on), "parallel_group": node.parallel_group, "allowlist": list(node.allowlist), "owned_paths": list(node.owned_paths), "writer": node.writer, "reviewer": node.reviewer, "worktree": node.worktree})
        digest = digests.sha256_bytes(json.dumps(expected, sort_keys=True, separators=(",", ":")).encode("utf-8"))
        blocked_ids = {
            node_id for node_id, current in snapshot.nodes.items()
            if current.get("status") in {"blocked_unknown", "blocked_design", "blocked_deploy"}
        }
        # Use the same node-scoped hard-dependency projection as DAG audit.
        # A repair/waiting node remains active for identity/capacity accounting,
        # while unrelated planned nodes stay eligible to start.
        projected = []
        legacy_topology = False
        for node in self.nodes.values():
            current = snapshot.nodes.get(node.id, {})
            runtime_status = current.get("status", getattr(node, "status", "planned"))
            # Snapshot recovery phases (retry_pending, timeout, failed, ...)
            # are intentionally richer than the DAG model. They are not
            # rewritten into the plan; use the node's last valid plan status
            # for readiness while retaining the snapshot phase elsewhere.
            if runtime_status not in {"planned", "ready", "running", "delivered", "review", "accepted", "rework", "blocked_design", "blocked_deploy", "blocked_unknown", "brief_pending"}:
                # Recovery phases such as retry_pending, timeout and failed
                # must stay non-ready until the same binding is confirmed.
                runtime_status = "running"
            if hasattr(node, "__dataclass_fields__"):
                projected.append(replace(node, status=runtime_status))
            else:
                projected_node = copy.copy(node)
                if not hasattr(projected_node, "contract"):
                    # Compatibility snapshots from V4.1 carried only the
                    # topology fields. Treat their absent contract as empty
                    # so the DAG gate can report not-ready without raising.
                    projected_node.contract = {}
                    legacy_topology = True
                projected_node.status = runtime_status
                projected.append(projected_node)
        if legacy_topology:
            by_id = {node.id: node for node in projected}
            ready = [
                node.id for node in projected
                if node.id not in blocked_ids
                and node.status in {"planned", "ready"}
                and all(by_id.get(dep) is not None and by_id[dep].status == "accepted" for dep in node.depends_on)
            ]
        else:
            ready = node_scoped_ready(projected, blocked_ids=blocked_ids)
        return ready, digest

    def _refresh_execution_projection(self, snapshot: RunSnapshot, entry: str = "monitor.unknown") -> None:
        ready, digest = self._topology_projection(snapshot)
        snapshot.ready_set = ready
        snapshot.topology_digest = digest
        snapshot.started_nodes = [n for n, c in snapshot.nodes.items() if c.get("active_task") or n in snapshot.handles]
        snapshot.active_concurrency = len(self._occupied_nodes(snapshot)[0])
        snapshot.capacity = self._effective_worker_session_limit(
            self._snapshot_record(snapshot)
        )
        snapshot.monitor_entry_evidence = entry
        snapshot.parallel_groups = {n: ([self.nodes[n].parallel_group] if self.nodes[n].parallel_group else []) for n in snapshot.started_nodes}

    def _record_topology_projection(self, snapshot: RunSnapshot, entry: str, recovery_evidence=None) -> None:
        self._refresh_execution_projection(snapshot, entry)
        self._record(snapshot, "execution_topology_observed", {
            "run_id": snapshot.run_id, "plan_revision": snapshot.plan_version,
            "node_ids": sorted(self.nodes), "started_nodes": list(snapshot.started_nodes),
            "active_concurrency": snapshot.active_concurrency, "capacity": snapshot.capacity,
            "parallel_groups": snapshot.parallel_groups,
            "monitor_entry_evidence": entry,
            **({"recovery_evidence": recovery_evidence} if recovery_evidence is not None else {}),
        })

    def _occupied_nodes(self, snapshot: RunSnapshot) -> Tuple[List[str], Optional[str]]:
        """Nodes holding a worker-session slot, plus why the registry was unreadable.

        The task registry is the source of truth for live sessions; the
        snapshot's own started set only adds nodes the registry has not yet
        recorded.  An unreadable or contradictory registry yields the snapshot
        view and a reason, never a guessed count.
        """
        started = [n for n, c in snapshot.nodes.items() if c.get("active_task") or n in snapshot.handles]
        if not hasattr(self, "paths"):
            return sorted(started), None
        try:
            registered = active_registry_issues(self.paths, snapshot.run_id, self.nodes)
        except (OSError, ValueError, TypeError, KeyError) as error:
            return sorted(started), str(error)
        return sorted(set(started) | set(registered)), None

    def _reconcile_capacity(self, snapshot: RunSnapshot) -> None:
        """Correct the capacity counter from the task registry; never void authorization.

        Exceeding the effective capacity is a dispatch condition (new starts
        pause until a slot frees), not topology drift.  A stored counter that
        disagrees with the registry is rewritten and logged; an unreadable or
        contradictory registry holds dispatch as ``blocked_unknown``.
        """
        hold = self.__dict__.setdefault("_capacity_hold", {})
        reported = self.__dict__.setdefault("_capacity_unknown_reported", set())
        occupied, unknown = self._occupied_nodes(snapshot)
        if unknown is None and not hasattr(snapshot, "run_id"):
            return
        if unknown is not None:
            key = (snapshot.run_id, unknown)
            if key not in reported:
                reported.add(key)
                self._record(snapshot, "capacity_registry_unknown", {
                    "run_id": snapshot.run_id, "capacity": snapshot.capacity,
                    "change_reason": "capacity_registry_unknown", "reason": unknown,
                })
            hold[snapshot.run_id] = unknown
            return
        hold.pop(snapshot.run_id, None)
        if getattr(snapshot, "active_concurrency", None) in (None, len(occupied)):
            return
        previous = snapshot.active_concurrency
        snapshot.active_concurrency = len(occupied)
        self._record(snapshot, "capacity_counter_corrected", {
            "run_id": snapshot.run_id, "capacity": snapshot.capacity,
            "active_concurrency": snapshot.active_concurrency,
            "started_nodes": occupied,
            "change_reason": "capacity_counter_corrected_from_registry",
            "reason": "stored active_concurrency {} disagrees with task registry {}; capacity {}".format(
                previous, len(occupied), snapshot.capacity),
        })
        self._record_topology_projection(snapshot, snapshot.monitor_entry_evidence)

    def _apply_capacity_hold(self, snapshot: RunSnapshot) -> None:
        if self.__dict__.get("_capacity_hold", {}).get(snapshot.run_id) and snapshot.status != "complete":
            snapshot.status = "blocked_unknown"

    def _validate_execution_topology(self, snapshot: RunSnapshot) -> None:
        if getattr(self.plan, "complexity_band", "") != "complex":
            return
        ready, digest = self._topology_projection(snapshot)
        if snapshot.dag_revision != self.plan.version or snapshot.topology_digest != digest:
            raise PermissionError("blocked_by_execution_topology_mismatch: topology projection drift")
        if list(snapshot.ready_set) != ready:
            raise PermissionError("blocked_by_execution_topology_mismatch: ready-set drift")
        started = [n for n, c in snapshot.nodes.items() if c.get("active_task") or n in snapshot.handles]
        # Order-insensitive: save_snapshot sorts node keys, so a reloaded
        # snapshot lists nodes alphabetically while started_nodes keeps
        # dispatch order.
        if sorted(snapshot.started_nodes) != sorted(started):
            raise PermissionError("blocked_by_execution_topology_mismatch: started-set drift")
        self._reconcile_capacity(snapshot)
        expected_groups = {n: ([self.nodes[n].parallel_group] if self.nodes[n].parallel_group else []) for n in started}
        if snapshot.parallel_groups != expected_groups:
            raise PermissionError("blocked_by_execution_topology_mismatch: parallel-group drift")
        if not snapshot.monitor_entry_evidence.startswith("monitor."):
            raise PermissionError("blocked_by_execution_topology_mismatch: monitor entry evidence missing")
        if not hasattr(self, "paths"):
            return
        events = load_events(self.paths, snapshot.run_id)
        observed = [e for e in events if e.get("event") == "execution_topology_observed"]
        if not observed:
            raise PermissionError("blocked_by_execution_topology_mismatch: topology evidence missing")
        latest = observed[-1].get("data", {})
        expected = {"run_id": snapshot.run_id, "plan_revision": snapshot.plan_version, "node_ids": sorted(self.nodes), "started_nodes": snapshot.started_nodes, "active_concurrency": snapshot.active_concurrency, "capacity": snapshot.capacity, "parallel_groups": snapshot.parallel_groups}
        for key, value in expected.items():
            if latest.get(key) != value:
                raise PermissionError("blocked_by_execution_topology_mismatch: topology evidence drift")
        if latest.get("monitor_entry_evidence") != snapshot.monitor_entry_evidence:
            raise PermissionError(
                "blocked_by_execution_topology_mismatch: monitor entry evidence drift "
                "(snapshot={!r}, events={!r})".format(
                    snapshot.monitor_entry_evidence, latest.get("monitor_entry_evidence")
                )
            )

    def _heal_entry_evidence(self, snapshot: RunSnapshot) -> None:
        """Re-record the entry evidence projection from the event stream.

        The snapshot's ``monitor_entry_evidence`` is bookkeeping written by the
        last monitor entry point; the event stream is the source of truth for
        which entry ran last.  When only that one field disagrees (a recovery
        entry such as ``monitor.reconcile_adopted_dispatch`` saved the snapshot
        after the stream's last observation), the stream wins: the projection
        is re-recorded and the correction is logged with both actual values.
        Any other topology disagreement is left for the normal check to reject.
        """
        if getattr(self.plan, "complexity_band", "") != "complex":
            return
        observed = [
            e for e in load_events(self.paths, snapshot.run_id)
            if e.get("event") == "execution_topology_observed"
        ]
        if not observed:
            return
        latest = observed[-1].get("data", {})
        stream_entry = latest.get("monitor_entry_evidence")
        if (
            not isinstance(stream_entry, str)
            or not stream_entry.startswith("monitor.")
            or stream_entry == snapshot.monitor_entry_evidence
        ):
            return
        expected = {
            "run_id": snapshot.run_id, "plan_revision": snapshot.plan_version,
            "node_ids": sorted(self.nodes), "started_nodes": snapshot.started_nodes,
            "active_concurrency": snapshot.active_concurrency,
            "capacity": snapshot.capacity, "parallel_groups": snapshot.parallel_groups,
        }
        if any(latest.get(key) != value for key, value in expected.items()):
            return
        self._record(snapshot, "monitor_entry_evidence_reconciled", {
            "run_id": snapshot.run_id,
            "previous_monitor_entry_evidence": snapshot.monitor_entry_evidence,
            "monitor_entry_evidence": stream_entry,
            "change_reason": "event_stream_is_source_of_truth",
        })
        snapshot.monitor_entry_evidence = stream_entry

    def __init__(
        self,
        paths: ProjectPaths,
        plan: Plan,
        nodes: List[DAGNode],
        context_policy: Optional[ContextBudgetPolicy] = None,
        topology_rulings: Optional[Any] = None,
    ):
        self.paths = paths
        # Set by Supervisor: raises when this process no longer holds the
        # current supervisor lease epoch, so a stale supervisor cannot write.
        self.lease_guard = None
        self.plan = plan
        self.nodes = {node.id: node for node in nodes}
        # Platform dispatch-topology rulings (adapter_id -> ruling), normally
        # derived from the ISSUE-07 probe matrix.  Anything missing or
        # unrecognized is fail-closed to the conservative dual-visible
        # topology; UNKNOWN never upgrades a node to visible-sdd.
        self._topology_rulings: Dict[str, str] = {}
        if isinstance(topology_rulings, dict):
            for adapter_id, ruling in topology_rulings.items():
                if isinstance(adapter_id, str) and isinstance(ruling, str):
                    self._topology_rulings[adapter_id] = ruling
        # Binding reads are scoped to one public monitor operation.  The
        # registry is still the durable source of truth; clearing this cache
        # at each operation boundary prevents stale cross-tick identities.
        self._binding_cache: Dict[Tuple[str, str, str], TaskBinding] = {}
        self.context_policy = context_policy

    def _reset_binding_cache(self) -> None:
        self._binding_cache.clear()

    def _node_dispatch_topology(self, node: DAGNode) -> str:
        """Rule the dispatch topology for one node.

        Delegates to
        :func:`vibe_guide.authorization.dispatch_topology_for_node`, which is
        also what ``refresh_authorization_card`` rules the card's workers
        through.  Keeping one implementation is the point: the card must
        describe the dispatch the supervisor will actually perform, and a
        second copy of this resolution order is exactly how the two drifted
        apart before.
        """
        return dispatch_topology_for_node(node, self._topology_rulings)

    def _parallel_group_blocked(self, snapshot: RunSnapshot) -> set:
        """Node ids refused by the V4.6 parallel-group audit.

        The audit runs on the same status projection the scheduler uses for
        readiness, so ``running`` siblings hold their write scope against new
        dispatches.  ``_parallel_group_errors`` arrives with ISSUE-06; before
        that dag.py lands, the blocked set is empty (no behavior change).
        """
        audit = getattr(_dag_module, "_parallel_group_errors", None)
        if not callable(audit):
            return set()
        projected = self._projection_nodes(snapshot)
        try:
            return set(audit(projected))
        except (TypeError, ValueError):
            # Unverifiable audit metadata is fail-closed inside the audit
            # itself; a broken projection must not silently widen dispatch.
            return set(self.nodes)

    _OWNERSHIP_IN_FLIGHT = ("running", "rework", "review", "brief_pending")

    def _refuse_path_ownership_conflicts(self, snapshot: RunSnapshot) -> None:
        """Refuse the whole dispatch round when candidate writers collide.

        ``validate_path_ownership`` (V3.8) is the existing owned_paths check;
        until V4.7 nothing on the dispatch path called it.  The candidates
        are the planned nodes eligible for a first write this round plus the
        in-flight writers they would run beside, regardless of
        parallel_group.  Any overlap, missing ownership metadata or invalid
        path fails closed: every planned candidate becomes blocked_unknown,
        the conflict is recorded as an event and written to the run-scoped
        ``.vibe/runs/<run-id>/dag-audit.json`` (the plan-level dag-audit.json
        is digest-locked after authorization and must never be rewritten).
        """
        planned = []
        in_flight = []
        for node_id, node in self.nodes.items():
            current = snapshot.nodes[node_id]
            status = current.get("status")
            if status == "planned" and all(
                snapshot.nodes[dependency].get("status") == "accepted"
                for dependency in node.depends_on
            ):
                planned.append(node_id)
            elif status in self._OWNERSHIP_IN_FLIGHT:
                in_flight.append(node_id)
        if not planned:
            return
        candidates = planned + in_flight
        try:
            result = validate_path_ownership([self.nodes[node_id] for node_id in candidates])
        except (TypeError, ValueError) as error:
            conflicts, missing = [], []
            reason = "path ownership unverifiable: {}".format(error)
        else:
            if result.valid:
                return
            conflicts = [{"path": item.path, "nodes": list(item.nodes)} for item in result.conflicts]
            missing = list(result.missing_nodes)
            details = ["{} owned by {}".format(item["path"], ", ".join(item["nodes"])) for item in conflicts]
            if missing:
                details.append("ownership metadata missing on " + ", ".join(missing))
            reason = "path ownership conflict: " + "; ".join(details)
        affected = sorted({node_id for item in conflicts for node_id in item["nodes"]} | set(missing))
        for node_id in planned:
            self._mark_blocked_unknown(snapshot, node_id, reason, quarantine_lease=False)
        self._record(
            snapshot,
            "path_ownership_conflict",
            {
                "run_id": snapshot.run_id,
                "node_ids": list(candidates),
                "affected_nodes": affected,
                "reason": reason,
            },
        )
        audit = {
            "status": "blocked_unknown",
            "run_id": snapshot.run_id,
            "plan_revision": str(snapshot.plan_version),
            "node_count": len(candidates),
            "node_ids": list(candidates),
            "path_ownership": {
                "valid": False,
                "conflicts": conflicts,
                "missing_nodes": missing,
                "blocked_nodes": list(planned),
                "reason": reason,
            },
        }
        audit_path = run_dir(self.paths, snapshot.run_id, create=True) / "dag-audit.json"
        # Append rather than overwrite: multiple conflict rounds in the same run
        # each contribute a record so the full ownership history survives.
        existing: list = []
        if audit_path.is_file():
            try:
                raw = json.loads(audit_path.read_text(encoding="utf-8"))
                if isinstance(raw, list):
                    existing = raw
                elif isinstance(raw, dict):
                    existing = [raw]
            except (OSError, ValueError):
                existing = []
        existing.append(audit)
        fsio.atomic_write_bytes(
            audit_path,
            (json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8"),
        )

    def _projection_nodes(self, snapshot: RunSnapshot) -> List[DAGNode]:
        """The status-projected node list used for readiness decisions."""
        projected = []
        for node in self.nodes.values():
            current = snapshot.nodes.get(node.id, {})
            runtime_status = current.get(
                "status", getattr(node, "status", "planned")
            )
            if runtime_status not in {"planned", "ready", "running", "delivered", "review", "accepted", "rework", "blocked_design", "blocked_deploy", "blocked_unknown", "brief_pending"}:
                runtime_status = "running"
            projected.append(replace(node, status=runtime_status))
        return projected

    def _effective_worker_session_limit(self, record: AuthorizationRecord) -> int:
        """Concurrency cap = min(authorization card snapshot, project config).

        The project config (``.vibe/config.json``,
        ``max_active_worker_sessions``) is validated by ``load_project_config``;
        an explicit but illegal value is a configuration error and propagates
        instead of being silently replaced.
        """
        card_limit = int(getattr(record, "active_pair_limit", 0) or 0)
        config_limit = load_project_config(self.paths.root).max_active_worker_sessions
        if card_limit < 1:
            return config_limit
        return min(card_limit, config_limit)

    def _supervisor_writer_rejection(
        self, node: DAGNode, current: Dict[str, Any]
    ) -> Optional[str]:
        """Return a reason when the node's writer is the supervisor itself.

        Structural guard: the monitor/supervisor identity must never be the
        writer of any node.  This does not rely on authorization-card text;
        every dispatch path passes through it.
        """
        contract = node.contract if isinstance(node.contract, dict) else {}
        candidates = [
            contract.get("worker"),
            contract.get("reviewer_worker"),
            current.get("worker"),
            getattr(node, "writer", ""),
            getattr(node, "reviewer", ""),
        ]
        profile = contract.get("worker_profile")
        if isinstance(profile, dict):
            candidates.extend([profile.get("worker"), profile.get("writer")])
        for value in candidates:
            if (
                isinstance(value, str)
                and value.strip().casefold() in _SUPERVISOR_WRITER_IDENTITIES
            ):
                return (
                    "supervisor identity cannot be a node writer: "
                    + value.strip()
                )
        return None

    def _current_plan_digests(self) -> Tuple[str, str]:
        values = []
        for ref in ("prd", "spec"):
            supplied = getattr(self.plan, ref + "_digest", "")
            if supplied:
                values.append(str(supplied))
                continue
            path_value = getattr(self.plan, ref + "_path", "")
            path = Path(path_value) if path_value else Path("")
            if path_value and not path.is_absolute():
                path = self.paths.root / path
            try:
                values.append(digests.sha256_bytes(path.read_bytes()) if path_value else "")
            except (OSError, ValueError):
                values.append("")
        return values[0], values[1]

    def _load_task_binding(
        self, snapshot: RunSnapshot, node_id: str, role: str
    ) -> TaskBinding:
        key = (snapshot.run_id, node_id, role)
        binding = self._binding_cache.get(key)
        if binding is None:
            binding = load_task_binding(
                self.paths, node_id, role, run_id=snapshot.run_id
            )
            self._binding_cache[key] = binding
        return binding

    def _guard_lease(self, run_id: Optional[str] = None) -> None:
        guard = getattr(self, "lease_guard", None)
        if guard is not None:
            guard()
        if run_id is not None:
            # Independent of the --watch guard: a superseded session starting
            # its own watch must still stop (the lease pid check cannot see it).
            from .supervisor import ensure_caller_not_superseded
            ensure_caller_not_superseded(self.paths, run_id)

    def _save_snapshot(self, snapshot: Any) -> None:
        self._guard_lease(getattr(snapshot, "run_id", None))
        save_snapshot(self.paths, snapshot)

    def _archive_finished_runs(self) -> None:
        """Sweep terminal runs out of ``.vibe/runs/`` (issue #274).

        Runs archive at monitor entry rather than inside the save path: the
        run that just reached ``complete`` may still be reopened by an
        accepted patch (``reauthorize`` restores it), and post-command writers
        like bounded supervisor output must not resurrect a stub.  Archiving
        is best-effort per run -- a failure stays on disk for the manual
        ``vibe reconcile --evidence {"kind": "run_archive"}`` path instead of
        breaking the command that triggered the sweep.
        """
        runs_root = self.paths.vibe / "runs"
        if runs_root.is_symlink() or not runs_root.is_dir():
            return
        terminal = {"complete"}
        for child in sorted(runs_root.iterdir(), key=lambda item: item.name):
            if child.is_symlink() or not child.is_dir():
                continue
            try:
                validate_run_id(child.name)
            except ValueError:
                continue
            state_file = child / "state.json"
            if not state_file.is_file() or state_file.is_symlink():
                continue
            try:
                status = json.loads(state_file.read_text(encoding="utf-8")).get("status")
            except (OSError, ValueError, UnicodeDecodeError):
                continue
            # Only ``complete`` archives automatically: a failed or
            # blocked_unknown run may still hold a quarantined writer lease
            # that is exactly what keeps a possibly-live session from being
            # redispatched.  Those statuses archive through the human-facing
            # ``vibe reconcile`` run_archive package (they are terminal, so
            # no force is needed; force is only for live or stateless runs).
            if status not in terminal:
                continue
            try:
                self.archive_run(child.name)
            except (OSError, ValueError, TypeError) as error:
                # Archiving is best-effort at monitor entry, but a failure
                # must still leave on-disk evidence with its reason class:
                # a partially archived run (directory moved, mailbox
                # requests still pending, lease unreleased) is otherwise
                # invisible until someone notices the stuck run.
                self._record_archive_failed(child.name, error)
                continue

    def _save_task_binding(self, binding: TaskBinding) -> None:
        self._guard_lease(binding.run_id)
        save_task_binding(self.paths, binding)
        if binding.run_id is not None:
            self._binding_cache[(binding.run_id, binding.issue_id, binding.role)] = binding

    def _require_binding_gate(
        self, contract: Dict[str, Any], binding: TaskBinding, runner: Runner
    ) -> None:
        """Fail closed before a real runner can start or write.

        ProviderActionRunner exposes the same gate so both boundaries execute
        it.  A generic runner is checked here as well, preserving legacy
        contracts that do not opt into V3.9.
        """
        if not binding_contract_enabled(contract):
            return
        verification = runtime_binding_gate(contract, binding)
        if not isinstance(verification, BindingVerification):
            raise ValueError("local provider binding gate returned invalid verification")
        if verification.binding_state != "binding_verified" or not verification.business_write_allowed:
            raise ValueError(
                "provider binding gate blocked_unknown: missing={} conflicts={}".format(
                    ",".join(verification.missing), ",".join(verification.conflicts)
                )
            )
        # Refresh the supervisor-owned lease at the write boundary.  A cached
        # observation is not sufficient for generic runners that expose no
        # provider hook; a released/expired lease must fail closed.
        node_id = str(contract.get("node_id") or binding.issue_id)
        fresh_lease = read_writer_lease(self.paths, node_id, binding.worktree)
        observed = getattr(binding, "binding_observation", None)
        if fresh_lease is None or observed is None or observed.lease != fresh_lease:
            raise ValueError("supervisor lease is stale or unavailable")
        refreshed = runtime_binding_gate(contract, binding)
        if (
            not isinstance(refreshed, BindingVerification)
            or refreshed.binding_state != "binding_verified"
            or not refreshed.business_write_allowed
        ):
            raise ValueError("provider binding gate blocked after lease refresh")
        # Provider hooks are advisory consistency checks only.  A provider
        # cannot manufacture permission by returning an object with
        # ``verified=True`` or another untrusted shape.
        probe = getattr(runner, "provider_binding_probe", None)
        if callable(probe):
            provider_probe = probe(contract, binding)
            if (
                not isinstance(provider_probe, BindingVerification)
                or provider_probe.binding_state != "binding_verified"
                or not provider_probe.verified
                or provider_probe != refreshed
            ):
                raise ValueError("provider binding probe returned invalid verification")
        gate = getattr(runner, "binding_gate", None)
        if callable(gate):
            provider_verification = gate(contract, binding)
            if not isinstance(provider_verification, BindingVerification):
                raise ValueError("provider binding gate returned invalid verification")
            if provider_verification != refreshed:
                raise ValueError("provider binding gate disagrees with local evidence")

    def _preflight_binding_before_provider(self, contract: Dict[str, Any]) -> None:
        """Gate provider actions before ``task_binding`` can emit a request.

        Unknown provider task ids are the sole exception: an explicit
        ``binding_probe`` may be emitted, and is marked non-business in its
        request.  Any ordinary V3.9 start requires complete protected live
        evidence first.
        """
        if not binding_contract_enabled(contract):
            return
        verification = runtime_binding_gate(contract)
        if not isinstance(verification, BindingVerification):
            raise ValueError("provider binding preflight returned invalid verification")
        if contract.get("binding_probe") is True:
            # An unknown provider task id is the sole controlled exception:
            # invoke the local gate first, then permit a non-business probe
            # only when no untrusted evidence was supplied.
            if verification.verified:
                return
            if (
                verification.missing == ["binding_provenance"]
                and "binding_intent" not in contract
                and "binding_observation" not in contract
            ):
                return
            raise ValueError("provider binding probe preflight blocked_unknown")
        if not verification.verified:
            raise ValueError("provider binding preflight blocked_unknown")

    @staticmethod
    def _binding_recovery_requested(contract: Dict[str, Any]) -> bool:
        """Identify the narrow JSON-restored continuation recovery path."""
        if not binding_contract_enabled(contract):
            return False
        if contract.get("continuation") is not True or contract.get("binding_bootstrap") is not True:
            return False
        return not (
            isinstance(contract.get("binding_intent"), BindingIntent)
            and isinstance(contract.get("binding_observation"), BindingObservation)
        )

    def _preflight_binding_recovery(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        contract: Dict[str, Any],
        current: Dict[str, Any],
    ) -> None:
        """Validate only the durable identity needed to enter bootstrap.

        A persisted binding has intentionally lost its private lease/cursor
        provenance.  This preflight therefore never returns a verified gate;
        it only proves that recovery will address the same task and the same
        constrained worktree.  The runner bootstrap performs the fresh Git
        observation and the subsequent full protected-evidence gate.
        """
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
            root = self._worktree_path(current).resolve(strict=True)
            bound = Path(str(binding.worktree))
            if not bound.is_absolute():
                bound = self.paths.root / bound
            bound = bound.resolve(strict=True)
            managed_root = contract.get("managed_root")
            branch = contract.get("branch")
            base_sha = contract.get("base_sha")
            if not isinstance(managed_root, str) or not managed_root:
                raise ProviderUnavailable("binding recovery managed_root is missing")
            if not isinstance(branch, str) or not branch:
                raise ProviderUnavailable("binding recovery branch is missing")
            if not isinstance(base_sha, str) or not base_sha:
                raise ProviderUnavailable("binding recovery base_sha is missing")
            managed = Path(managed_root).resolve(strict=True)
            root.relative_to(managed)
            if bound != root:
                raise ProviderUnavailable("binding recovery worktree drift")
            if binding.issue_id != node_id or binding.role != role:
                raise ProviderUnavailable("binding recovery task scope drift")
            if binding.run_id != snapshot.run_id:
                raise ProviderUnavailable("binding recovery run drift")
            if binding.task_id != contract.get("task_id"):
                raise ProviderUnavailable("binding recovery task identity drift")
            if binding.branch != branch:
                raise ProviderUnavailable("binding recovery branch drift")
            expected_host = contract.get("host_id")
            if expected_host not in (None, "") and binding.host != expected_host:
                raise ProviderUnavailable("binding recovery host drift")
            expected_project = contract.get("project_id")
            if expected_project not in (None, ""):
                persisted_intent = binding.binding_intent
                if isinstance(persisted_intent, BindingIntent) and persisted_intent.project_id != expected_project:
                    raise ProviderUnavailable("binding recovery project drift")
        except ProviderUnavailable:
            raise
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ProviderUnavailable("binding recovery preflight blocked_unknown") from error

    def _run_binding_bootstrap(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        contract: Dict[str, Any],
        runner: Runner,
        current: Dict[str, Any],
    ) -> None:
        """Invoke the real runner bootstrap only for an explicit recovery."""
        if not binding_contract_enabled(contract) or (
            contract.get("binding_bootstrap") is not True
            and contract.get("continuation") is not True
        ):
            return
        bootstrap = getattr(runner, "binding_bootstrap", None)
        if not callable(bootstrap):
            raise ProviderUnavailable("binding bootstrap is unavailable")
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ProviderUnavailable("binding bootstrap has no same-task binding") from error
        result = bootstrap(contract, binding, self._worktree_path(current))
        if not isinstance(result, BindingVerification) or not result.verified:
            raise ProviderUnavailable("binding bootstrap remains blocked_unknown")
        contract["binding_intent"] = binding.binding_intent
        contract["binding_observation"] = binding.binding_observation

    @staticmethod
    def classify_observation(observation: Any) -> ObservationDisposition:
        return classify_observation(observation)

    def self_heal_binding(self, snapshot: RunSnapshot, node_id: str, observation: Any) -> HealingResult:
        result = self_heal_binding(snapshot, node_id, observation)
        node_state = snapshot.nodes[node_id]
        frozen_binding = node_state.get("binding")
        if not isinstance(frozen_binding, dict):
            frozen_binding = {
                key: node_state.get(key)
                for key in ("task_id", "worktree", "branch", "allowlist", "cursor", "continuation_digest")
                if key in node_state
            }
        self._record(snapshot, "binding_self_healed" if result.repaired else "binding_degraded", {
            "run_id": snapshot.run_id, "node_id": node_id,
            "observation_kind": classify_observation(observation).reason,
            "disposition": "repaired" if result.repaired else "degraded",
            "action": "repair" if result.repaired else "isolate",
            "repaired": result.repaired, "isolated": result.isolated,
            "binding": frozen_binding,
        })
        self._save_snapshot(snapshot)
        return result

    def isolate_affected_action(self, snapshot: RunSnapshot, node_id: str, reason: str) -> None:
        isolate_affected_action(snapshot, node_id, reason)
        self._record(snapshot, "action_isolated", {
            "run_id": snapshot.run_id, "node_id": node_id,
            "reason": reason, "disposition": "degraded", "action": "isolate", "isolated": True,
        })
        self._save_snapshot(snapshot)

    def merge_change_request_local(
        self,
        run_id: str,
        change_request: ChangeRequest,
        local_facts: Optional[Dict[str, Any]] = None,
    ) -> LocalMergeEvidence:
        """Record a local-only Change Request outcome for an existing run.

        The method never invokes Git or a provider.  It only validates the
        persisted authorization and appends the evidence returned by
        :func:`merge_local` to the run event log.
        """
        snapshot = load_snapshot(self.paths, run_id)
        self._validate_execution_topology(snapshot)
        record = self._require_snapshot_authorization(snapshot)
        evidence = merge_local(change_request, record, local_facts)
        self._record(
            snapshot,
            "change_request_" + evidence.status,
            {
                "run_id": snapshot.run_id,
                "change_request": change_request.to_dict(),
                "evidence": evidence.to_dict(),
            },
        )
        self._record_topology_projection(snapshot, "monitor.merge_local")
        self._save_snapshot(snapshot)
        return evidence

    # Short alias for callers that already operate in the Monitor boundary.
    merge_local = merge_change_request_local

    def start(
        self, record: Optional[AuthorizationRecord], runner: Runner
    ) -> RunSnapshot:
        self._reset_binding_cache()
        self._archive_finished_runs()
        self._require_record(record)
        state = self.paths.vibe / "state.json"
        capability_contract_digest = ""
        task_workflow = None
        state_data = {}
        if state.is_file():
            try:
                require_entry(self.paths, "monitor:" + self.plan.plan_id, "monitor")
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise PermissionError("session_gate_blocked") from error
            capability_contract_digest = require_capability_contract(
                self.paths
            ).contract_digest
            try:
                state_data = json.loads(state.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as error:
                raise PermissionError("session_gate_blocked") from error
            # Evidence is selected by plan identity: a token supplied for one
            # plan must not start a different one, so unrelated evidence reads
            # as absent and the required-workflow gate below still blocks.
            task_workflow = select_plan_workflow(state_data, self.plan.plan_id)
            if task_workflow is not None:
                workflow_result = verify_workflow(task_workflow)
                if workflow_result.get("status") != "complete":
                    raise PermissionError("required_workflow_blocked: {}".format(workflow_result.get("node", "unknown")))
                # Recorded digests are otherwise never re-checked, so an
                # artifact edited after authorization would execute on stale
                # evidence.
                verify_workflow_artifacts(self.paths, task_workflow)
        elif self.paths.vibe.exists():
            raise PermissionError("session_gate_blocked: V2 state.json is missing")
        assert record is not None
        integration_run = any(node.id == "integration-review" for node in self.nodes.values())
        if integration_run and task_workflow is None:
            raise PermissionError("required_workflow_blocked: workflow")
        prd_digest, spec_digest = self._current_plan_digests()
        if integration_run and (not prd_digest or not spec_digest):
            raise PermissionError("integration lineage blocked_unknown: PRD/Spec digest is missing")
        run_id = "run-" + uuid.uuid4().hex
        node_state: Dict[str, Dict[str, Any]] = {}
        for node_id, node in self.nodes.items():
            node_state[node_id] = {
                "status": "delivered" if node.status == "delivered" else "planned",
                "worker": node.contract.get("worker"),
                "worktree": node.contract.get("worktree", node_worktree(node_id)),
                "branch": node.contract.get("branch", node_branch(node_id)),
                "status_file": node.contract.get("status_file", ""),
                "handoff_file": node.contract.get("handoff_file", ""),
                "evidence": [],
                "active_role": None,
                "active_task": None,
                "start_intent": None,
                "quarantine": None,
                "developer_identity": None,
                "reviewer_identity": None,
                "developer_generation": 0,
                "review_generation": 0,
                "reviewer_started": False,
                "pair_archived": False,
                "old_task_reconciled": False,
                "retryable_action": None,
                "binding_phase": None,
                "contract_overrides": {},
                "corrections": [],
                "contract_digest": executable_contract_digest([node]),
                "acceptance": None,
            }
        snapshot = RunSnapshot(
            run_id=run_id,
            plan_id=self.plan.plan_id,
            plan_version=self.plan.version,
            status="initialized",
            nodes=node_state,
            handles={},
            tasks={},
            authorization=record.to_dict(),
            authorization_digest=record.digest,
            node_contract_digest=record.node_contract_digest,
            capability_contract_digest=capability_contract_digest,
            event_sequence=0,
            workflow=task_workflow,
            prd_digest=prd_digest,
            spec_digest=spec_digest,
            execution_engine=record.execution_engine,
            engine_mode=record.engine_mode,
            engine_evidence_ref=record.engine_evidence_ref,
            dag_revision=record.dag_revision,
        )
        self._refresh_execution_projection(snapshot, "monitor.start")
        self._record(
            snapshot,
            "run_started",
            {
                "run_id": run_id,
                "authorization_digest": record.digest,
                "node_contract_digest": record.node_contract_digest,
                "capability_contract_digest": capability_contract_digest,
                "node_ids": sorted(self.nodes),
                "prd_digest": snapshot.prd_digest,
                "spec_digest": snapshot.spec_digest,
                "authorized_node_contracts": self._encoded_node_contracts(),
                "node_contract_digests": {
                    key: current["contract_digest"] for key, current in snapshot.nodes.items()
                },
                "node_scope_digests": {
                    key: legacy_executable_contract_digest([node]) for key, node in self.nodes.items()
                },
            },
        )
        # _schedule_ready() validates the execution topology, and that check
        # requires an `execution_topology_observed` event in this run's log.  A
        # brand-new complex run has none yet, and the projection below was only
        # recorded after dispatch, so the very first dispatch of every complex
        # plan failed with "topology evidence missing".  Publish the observation
        # now: the projection was refreshed above, so this records exactly the
        # state the validator is about to read.
        self._record_topology_projection(snapshot, "monitor.start")
        self._save_snapshot(snapshot)
        if not self._context_allows_dispatch(snapshot, runner):
            self._save_snapshot(snapshot)
            return snapshot
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        self._record_topology_projection(snapshot, "monitor.start")
        self._save_snapshot(snapshot)
        return snapshot

    def resume(self, run_id: str, runner: Runner, poll_handles: bool = True) -> RunSnapshot:
        self._reset_binding_cache()
        self._archive_finished_runs()
        require_entry(self.paths, "resume:" + str(run_id), "resume")
        require_not_legacy_run(self.paths, run_id)
        snapshot = load_snapshot(self.paths, run_id)
        if isinstance(snapshot.workflow, dict):
            result = verify_workflow(snapshot.workflow)
            if result.get("status") != "complete":
                raise PermissionError("required_workflow_blocked: {}".format(result.get("node", "unknown")))
            # Symmetric with start(): the lineage check below covers prd.md and
            # the spec path only, so without this a post-authorization edit to
            # nodes.json, dag-audit.json, plan-confirmation.json or the card
            # would go undetected on every tick after the first.  The digests
            # must be read from `.vibe/state.json`, not from
            # `snapshot.workflow`: `evidence` is one of the redacted keys, so
            # the copy saved with the run has every `ref` and `sha256` replaced
            # by a placeholder and could never match.
            live_workflow = load_live_workflow(self.paths, snapshot.plan_id)
            if live_workflow is None:
                raise PermissionError("required_workflow_blocked: authorizing evidence is no longer present")
            verify_workflow_artifacts(self.paths, live_workflow, run_started=True)
        if "integration-review" in snapshot.nodes and snapshot.workflow is None:
            raise PermissionError("required_workflow_blocked: workflow")
        if "integration-review" in snapshot.nodes:
            current_prd, current_spec = self._current_plan_digests()
            if not self._lineage_matches_snapshot(snapshot, current_prd, current_spec):
                raise PermissionError("integration lineage blocked_unknown: PRD/Spec source drift")
        checkpoint_path = self.paths.root / ".vibe" / "runs" / run_id / "monitor_checkpoint.json"
        if checkpoint_path.is_file():
            # Validate the recovery package before touching leases or polling.
            resume_from_checkpoint(self.paths, run_id)
        current_capability_contract = require_capability_contract(self.paths)
        if (
            not snapshot.capability_contract_digest
            or snapshot.capability_contract_digest
            != current_capability_contract.contract_digest
        ):
            raise PermissionError("capability_contract_unknown: run binding mismatch")
        self._require_snapshot_authorization(snapshot)
        self._reconcile_unapplied_events(snapshot)
        self._heal_entry_evidence(snapshot)
        self._consume_answered_decisions(snapshot)
        self._route_undecided_blocked_design(snapshot)
        for node_id, current in snapshot.nodes.items():
            if current.get("status") == "start_pending":
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "start intent has no provider-confirmed result",
                )
            elif current.get("status") in {"running", "review", "rework"}:
                if node_id not in snapshot.handles and not isinstance(
                    current.get("retryable_action"), dict
                ):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "active task handle is missing during resume",
                    )
        # Poll durable handles before considering any new dispatch.  This is
        # the recovery ordering that prevents chat/context loss from creating
        # a second writer for an already-running provider task.
        if poll_handles:
            self._poll_active_handles(snapshot, runner)
        if self._context_allows_dispatch(snapshot, runner):
            replay_retry_pending = any(
                isinstance(current.get("retryable_action"), dict)
                and current.get("binding_phase") != "retry_pending"
                for current in snapshot.nodes.values()
            )
            # A caller explicitly replaying events without polling must see
            # the recovered snapshot before scheduling a new cycle. This is
            # especially important after an interrupted timeout event.
            if not (poll_handles is False and replay_retry_pending):
                self._schedule_ready(snapshot, runner, recover_missing_reviewer=True)
        self._refresh_run_status(snapshot)
        self._apply_capacity_hold(snapshot)
        self._record_topology_projection(snapshot, "monitor.resume")
        self._save_snapshot(snapshot)
        return snapshot

    def _encoded_node_contracts(self) -> Dict[str, str]:
        return {
            item["id"]: json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for item in canonical_node_contracts(list(self.nodes.values()))
        }

    def _previous_authorized_nodes(
        self, snapshot: RunSnapshot, previous: AuthorizationRecord
    ) -> Dict[str, DAGNode]:
        for event in reversed(load_events(self.paths, snapshot.run_id)[:snapshot.event_sequence]):
            data = event["data"]
            if event["event"] in {"run_started", "authorization_reauthorized"} and data.get("authorization_digest") == previous.digest:
                return verified_authorized_nodes(
                    data.get("authorized_node_contracts"), data.get("node_contract_digests"),
                    previous, data.get("node_scope_digests"),
                )
        return {}

    def reauthorize(
        self,
        run_id: str,
        record: AuthorizationRecord,
        runner: Runner,
        change_reason: str,
    ) -> RunSnapshot:
        """Continue the same run under a newly confirmed same-plan contract."""

        self._reset_binding_cache()
        self._require_record(record)
        # A completed run is archived out of .vibe/runs/ (issue #274);
        # reauthorization reopens it, so its evidence moves back first.
        unarchive_run(self.paths, run_id)
        snapshot = load_snapshot(self.paths, run_id)
        state = self.paths.vibe / "state.json"
        current_capability_contract_digest = snapshot.capability_contract_digest
        capability_contract_changed = False
        if state.is_file():
            current_capability_contract = require_capability_contract(self.paths)
            current_capability_contract_digest = current_capability_contract.contract_digest
            capability_contract_changed = (
                snapshot.capability_contract_digest
                != current_capability_contract_digest
            )
            if capability_contract_changed and change_reason not in {
                "capability_contract_changed",
                "executable_contract_changed",
                # #289: switching platforms changes the capability contract
                # by construction; the handoff gate below still refuses any
                # acceptance-contract change under this reason.
                "platform_handoff",
            }:
                raise PermissionError(
                    "capability_contract_unknown: explicit recovery reason required"
                )
        self._reconcile_unapplied_events(snapshot)
        # The replay may already have applied this exact authorization and
        # capability transition.  Re-read the live contract after replay so
        # an interrupted call cannot append the same transition again.
        if state.is_file():
            current_capability_contract = require_capability_contract(self.paths)
            current_capability_contract_digest = current_capability_contract.contract_digest
            capability_contract_changed = (
                snapshot.capability_contract_digest
                != current_capability_contract_digest
            )
        if (
            snapshot.plan_id != self.plan.plan_id
            or snapshot.plan_version != self.plan.version
            or set(snapshot.nodes) != set(self.nodes)
        ):
            raise PermissionError("reauthorization must remain on the same plan revision")
        if any(current.get("workspace_reconciliation_required") is True for current in snapshot.nodes.values()):
            raise PermissionError("workspace reconciliation required before reauthorization")
        live_prd, live_spec = self._current_plan_digests()
        if (snapshot.prd_digest and not live_prd) or (snapshot.spec_digest and not live_spec):
            raise PermissionError("reauthorization source lineage is unreadable")
        source_changed = (
            bool(live_prd) and live_prd != snapshot.prd_digest
        ) or (bool(live_spec) and live_spec != snapshot.spec_digest)
        if snapshot.authorization_digest == record.digest and not capability_contract_changed and not source_changed:
            self._require_snapshot_authorization(snapshot)
            # A replay of an interrupted reauthorization lands here with the
            # transition already applied but its projection never recorded,
            # so refresh the projection before dispatching for the same
            # reason as the full path below.
            self._record_topology_projection(snapshot, "monitor.reauthorize")
            if change_reason == "platform_handoff":
                self._hide_stale_handoff_creates(snapshot, runner)
            self._schedule_ready(snapshot, runner)
            self._refresh_run_status(snapshot)
            self._record_topology_projection(snapshot, "monitor.reauthorize")
            self._save_snapshot(snapshot)
            return snapshot

        previous = self._snapshot_record(snapshot)
        current_prd_digest, current_spec_digest = live_prd, live_spec
        integration_changed = (
            previous.integration_contract_digest != record.integration_contract_digest
            or current_prd_digest != snapshot.prd_digest
            or current_spec_digest != snapshot.spec_digest
        )
        previous_node_contract_digests = {
            node_id: snapshot.nodes[node_id].get("contract_digest")
            for node_id in snapshot.nodes
        }
        new_node_contract_digests = {
            node_id: executable_contract_digest([self.nodes[node_id]])
            for node_id in snapshot.nodes
        }
        # A node is unchanged when its previous digest still binds the new
        # contract.  Runs from 5.0.7 hold the scope-inclusive form, which a
        # plain comparison with the new digest would always call changed.
        # A 5.0.7 digest also binds file scope, so a scope-only expansion
        # would always look changed.  scope_only_expansion_binds proves
        # "only file scope changed" by stripping the union increment (known
        # ceiling: A adds and B drops the same file cancel out in the union,
        # nothing is provable, and both are redispatched).  state.py replay
        # applies the same predicate.
        previous_authorized_nodes = self._previous_authorized_nodes(snapshot, previous)

        def unchanged(node_id, digest):
            return route_only_acceptance_binds(
                self.nodes[node_id], previous_authorized_nodes.get(node_id), digest
            ) or executable_contract_digest([self.nodes[node_id]]) == digest or scope_only_expansion_binds(
                self.nodes[node_id], digest, previous.file_scope, record.file_scope
            )

        changed_nodes = sorted(
            node_id
            for node_id in snapshot.nodes
            if (node_id == "integration-review" and integration_changed)
            or not unchanged(node_id, previous_node_contract_digests[node_id])
        )
        if change_reason == "platform_handoff":
            # #289 draft section 2: a platform handoff may move only the
            # dispatch binding (adapter/worker/model routing) and the
            # capability contract.  The scope-only-expansion allowance of an
            # ordinary reauthorization does not apply: file scope is part of
            # the acceptance contract.  Refuse before any side effect (task
            # stop, event append) so a mislabelled change leaves the run as
            # it was.
            drifted = sorted(
                node_id
                for node_id in snapshot.nodes
                if not (
                    route_only_acceptance_binds(
                        self.nodes[node_id],
                        previous_authorized_nodes.get(node_id),
                        previous_node_contract_digests[node_id],
                    )
                    or executable_contract_digest([self.nodes[node_id]]) == previous_node_contract_digests[node_id]
                )
            )
            if drifted or integration_changed or record.file_scope != previous.file_scope:
                raise PermissionError(
                    "contract_drift: platform_handoff may only change dispatch "
                    "binding and capability contract (acceptance contract "
                    "changed: {})".format(
                        ", ".join(drifted)
                        or ("integration contract" if integration_changed else "file scope")
                    )
                )
        affected_nodes = affected_node_closure(
            list(self.nodes.values()), changed_nodes
        )
        authorized_node_contracts = {
            item["id"]: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in canonical_node_contracts(list(self.nodes.values()))
        }
        accepted_nodes = sorted(
            node_id
            for node_id, current in snapshot.nodes.items()
            if current.get("status") == "accepted"
        )
        retained_acceptances = {}
        invalidated_acceptances = {}
        for node_id, current in snapshot.nodes.items():
            if current.get("status") != "accepted":
                continue
            acceptance = current.get("acceptance")
            if not isinstance(acceptance, dict):
                raise RuntimeError("accepted node acceptance evidence is missing")
            evidence = {
                "contract_digest": acceptance.get("contract_digest"),
                "authorization_epoch": acceptance.get("authorization_epoch"),
            }
            if (
                node_id not in affected_nodes
                and unchanged(node_id, previous_node_contract_digests.get(node_id))
                and evidence["contract_digest"]
                == previous_node_contract_digests[node_id]
            ):
                retained_acceptances[node_id] = evidence
            else:
                invalidated_acceptances[node_id] = evidence
        for node_id, handle_id in list(snapshot.handles.items()):
            current = snapshot.nodes[node_id]
            active = current.get("active_task")
            if not isinstance(active, dict) or active.get("handle_id") != handle_id:
                raise RuntimeError("active task cannot be reconciled for reauthorization")
            # A provider may have completed the worker and persisted the
            # terminal delivery before the supervisor received the terminal
            # callback.  In that case ``stop`` is neither necessary nor
            # reliable: asking the provider to stop an already-complete task
            # produces the same unknown state as a dead provider handle.
            # Reconcile the existing binding through the normal registry
            # lifecycle and keep the task identity for rework continuation.
            if self._reconcile_completed_delivery_for_transition(
                snapshot, node_id, active
            ):
                role = str(active["role"])
                self._set_binding_status(snapshot, node_id, role, "delivered")
                current["old_task_reconciled"] = True
                current["active_role"] = None
                current["active_task"] = None
                current["quarantine"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
                continue
            if not self._stop_active_for_transition(
                snapshot,
                node_id,
                str(active["role"]),
                handle_id,
                runner,
                "stopped",
            ):
                self._save_snapshot(snapshot)
                raise RuntimeError("active task stop cannot be proven for reauthorization")
            snapshot.nodes[node_id]["old_task_reconciled"] = True

        continuation: Dict[str, Any] = {}
        for node_id in sorted(snapshot.nodes):
            for role in ("developer", "reviewer"):
                key = "{}:{}".format(node_id, role)
                try:
                    binding = self._load_task_binding(snapshot, node_id, role)
                except FileNotFoundError:
                    continue
                snapshot.tasks[key] = binding.to_dict()
                continuation[key] = {
                    "task_id": binding.task_id,
                    "cursor": binding.cursor,
                }

        # A same-run reauthorization is the user re-confirming the contract, so
        # it is also where an edited PRD/Spec is re-bound.  ``resume`` keeps
        # its strict lineage check: only an explicit reauthorization may move
        # the pinned source digests, and the move is recorded old -> new in
        # the transition so the audit chain shows what was re-bound.
        transition = {
            "run_id": snapshot.run_id,
            "previous_authorization": previous.to_dict(),
            "previous_authorization_digest": previous.digest,
            "previous_node_contract_digest": previous.node_contract_digest,
            "previous_capability_contract_digest": snapshot.capability_contract_digest,
            "previous_prd_digest": snapshot.prd_digest,
            "previous_spec_digest": snapshot.spec_digest,
            "new_authorization": record.to_dict(),
            "authorization_digest": record.digest,
            "node_contract_digest": record.node_contract_digest,
            "capability_contract_digest": current_capability_contract_digest,
            "prd_digest": current_prd_digest,
            "spec_digest": current_spec_digest,
            "previous_node_contract_digests": previous_node_contract_digests,
            "node_contract_digests": new_node_contract_digests,
            "authorized_node_contracts": authorized_node_contracts,
            "node_scope_digests": {
                key: legacy_executable_contract_digest([node]) for key, node in self.nodes.items()
            },
            "changed_nodes": changed_nodes,
            "affected_nodes": affected_nodes,
            "accepted_nodes": accepted_nodes,
            "retained_acceptances": retained_acceptances,
            "invalidated_acceptances": invalidated_acceptances,
            "change_reason": change_reason,
            "continuation": continuation,
        }
        # Validate the complete transition before publishing its durable event.
        # A rejected proof must not poison replay with an unappliable event.
        self._apply_reauthorization_transition(copy.deepcopy(snapshot), transition)
        self._record(snapshot, "authorization_reauthorized", transition)
        self._apply_reauthorization_transition(snapshot, transition)
        # The transition resets every never-started node back to ``planned``,
        # so the readiness projection the snapshot carries is stale by
        # construction.  Refresh and record it *before* dispatching:
        # ``_schedule_ready`` opens with ``_validate_execution_topology`` and
        # would otherwise compare the new ready set against the stale one and
        # fail closed with ``ready-set drift`` — the same double-record
        # ordering ``tick`` already uses.
        self._record_topology_projection(snapshot, "monitor.reauthorize")
        if change_reason == "platform_handoff":
            self._hide_stale_handoff_creates(snapshot, runner)
        self._schedule_ready(snapshot, runner)
        self._refresh_run_status(snapshot)
        self._record_topology_projection(snapshot, "monitor.reauthorize")
        self._save_snapshot(snapshot)
        return snapshot

    def tick(self, run_id: str, runner: Runner) -> RunSnapshot:
        self._reset_binding_cache()
        self._archive_finished_runs()
        require_not_legacy_run(self.paths, run_id)
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._validate_execution_topology(snapshot)
        self._reconcile_unapplied_events(snapshot)
        self._consume_answered_decisions(snapshot)
        self._route_undecided_blocked_design(snapshot)
        self._poll_active_handles(snapshot, runner)
        self._record_topology_projection(snapshot, "monitor.tick")
        if self._context_allows_dispatch(snapshot, runner):
            self._schedule_ready(snapshot, runner)
        self._record_topology_projection(snapshot, "monitor.tick")
        self._refresh_run_status(snapshot)
        self._apply_capacity_hold(snapshot)
        self._save_snapshot(snapshot)
        return snapshot

    def record_integration_review(self, run_id: str, evidence: Dict[str, Any]) -> RunSnapshot:
        """Persist a validated integration review and refresh run closeout."""
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._validate_execution_topology(snapshot)
        if any(node.get("workspace_reconciliation_required") is True for node in snapshot.nodes.values()):
            raise PermissionError("workspace reconciliation required before integration evidence")
        _record_integration_review(snapshot, evidence)
        self._record(snapshot, "integration_review_recorded", {
            "run_id": snapshot.run_id,
            "node_id": "integration-review",
            "evidence": evidence,
            "clearance": evidence.get("clearance"),
        })
        self._refresh_run_status(snapshot)
        self._record_topology_projection(snapshot, "monitor.record_integration_review")
        self._save_snapshot(snapshot)
        return snapshot

    def record_blocked_unknown_disposition(
        self,
        run_id: str,
        node_id: str,
        item: str,
        disposition: str,
        note: str = "",
    ) -> RunSnapshot:
        """Record one per-item disposition for a paused blocked_unknown delivery.

        Dispositions are the audit chain that closes the escape hatch: each
        pending item from the latest paused delivery must be explicitly
        confirmed benign, resolved after supplementing environment facts, or
        escalated to P0-P2 rework before acceptance may continue.  Every
        disposition answers the item (it leaves pending) and is appended to
        the event log, but only clearing dispositions
        (``confirmed_benign`` / ``resolved_with_facts``) release the
        acceptance gate; after ``escalated_to_findings`` the reworked
        re-delivery must no longer carry the item, otherwise the delivery
        pauses again.  Nothing here accepts the delivery itself.
        """
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        if node_id not in snapshot.nodes:
            raise ValueError("blocked_unknown disposition targets an unknown node")
        current = snapshot.nodes[node_id]
        if not isinstance(item, str) or not item.strip():
            raise ValueError("blocked_unknown disposition item must be a non-empty string")
        if disposition not in BLOCKED_UNKNOWN_DISPOSITION_KINDS:
            raise ValueError(
                "blocked_unknown disposition must be one of: {}".format(
                    ", ".join(BLOCKED_UNKNOWN_DISPOSITION_KINDS)
                )
            )
        if not isinstance(note, str):
            raise ValueError("blocked_unknown disposition note must be a string")
        pending = current.get("blocked_unknowns_pending") or []
        if item not in pending:
            raise ValueError(
                "blocked_unknown disposition item is not pending on this node"
            )
        dispositions = current.setdefault("blocked_unknown_dispositions", [])
        if any(
            entry.get("item") == item and entry.get("disposition") == disposition
            for entry in dispositions
        ):
            raise ValueError(
                "blocked_unknown disposition already recorded for this item and kind"
            )
        dispositions.append({"item": item, "disposition": disposition, "note": note})
        if disposition == "deferred_to_acceptance":
            current.setdefault("acceptance_pending_verification", []).append(
                {"item": item, "note": note}
            )
        # Any disposition answers the paused item, so it leaves pending; the
        # acceptance gate separately requires a *clearing* disposition before
        # a re-delivery carrying the same item may pass.
        current["blocked_unknowns_pending"] = [
            pending_item for pending_item in pending if pending_item != item
        ]
        self._record(
            snapshot,
            "blocked_unknown_disposition",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "disposition": disposition,
                # ``proof`` is the durable pass-through key for the structured
                # item/note payload (see _record_contract_test_execution).
                "proof": {"item": item, "note": note},
            },
        )
        self._refresh_run_status(snapshot)
        self._save_snapshot(snapshot)
        return snapshot

    def archive_run(
        self, run_id: str, *, force: bool = False, snapshot: Any = None,
        sessions_verified: bool = False,
    ) -> Dict[str, Any]:
        """Archive a finished run's evidence out of ``.vibe/runs/`` (issue #274).

        Moves the run directory, its provider-action mailbox artifacts, and
        its writer leases into ``.vibe/runs-archive/<run_id>/`` (plans-archive
        shape).  Without ``force`` only runs whose persisted state reached a
        terminal status (``complete``/``failed``/``blocked_unknown``/
        ``blocked_design``/``stopped``) archive; live or stateless runs need
        the explicit ``force`` flag from a human reconciliation package.  A
        terminal run that did not finish cleanly may hold a quarantined writer
        lease that keeps a possibly live session from being dispatched twice;
        archiving moves that lease away, so every status but ``complete``
        first proves its sessions are retired (``RunArchiveDeferred`` when one
        is live or cannot be read) unless the caller already did
        (``sessions_verified``).  The operation is idempotent and resumable;
        already-archived runs return the existing manifest.
        """
        validate_run_id(run_id)
        from .state import _safe_project_path, runs_archive_dir

        primary = _safe_project_path(self.paths, ".vibe", "runs", run_id)
        destination = runs_archive_dir(self.paths, run_id)
        if primary.is_symlink() or destination.is_symlink():
            raise ValueError("run archive paths may not be symlinks")

        status: Optional[str] = None
        provenance: Dict[str, Any] = {}
        for candidate in (primary / "state.json", destination / "state.json"):
            if candidate.is_file() and not candidate.is_symlink():
                try:
                    raw = json.loads(candidate.read_text(encoding="utf-8"))
                except (OSError, ValueError, UnicodeDecodeError):
                    raw = None
                if isinstance(raw, dict):
                    if isinstance(raw.get("status"), str):
                        status = raw["status"]
                    provenance = {
                        key: raw[key]
                        for key in ("authorization_digest", "node_contract_digest")
                        if isinstance(raw.get(key), str)
                    }
                    break
        terminal = {"complete", "failed", "blocked_unknown", "blocked_design",
                    "stopped"}
        if status not in terminal and not force:
            raise ValueError(
                "run {} is not in a terminal state (status={}); archiving a "
                "live or unverifiable run requires an explicit force"
                .format(run_id, status if status is not None else "missing")
            )

        if status != "complete" and not force and not sessions_verified and primary.is_dir():
            self._require_sessions_retired(run_id, snapshot)

        events_path = primary / "events.jsonl"
        already_recorded = False
        if events_path.is_file() and not events_path.is_symlink():
            try:
                already_recorded = any(
                    record.get("event") == "run_archived"
                    for record in load_events(self.paths, run_id)
                )
            except (OSError, ValueError):
                already_recorded = False
        try:
            manifest = _archive_run_artifacts(self.paths, run_id)
        except (OSError, ValueError, TypeError) as error:
            # Leave the failure on disk with its reason class: an archive
            # that died mid-move (directory gone but mailbox requests still
            # pending, lease unreleased) is otherwise indistinguishable from
            # a clean archive, and the auto-sweep would keep retrying a run
            # nobody can see is stuck.
            self._record_archive_failed(run_id, error)
            raise
        if not already_recorded:
            # The archive event is appended only after the move succeeded so
            # a failed archive never leaves a false ``run_archived`` record;
            # run_dir() resolves into .vibe/runs-archive/ at this point, so
            # the event still lands inside the archived directory.  Its
            # provenance carries the run's authorization/contract digests
            # (read from state.json, or supplied by the snapshot the caller
            # just saved) so event-lineage validation keeps passing.
            if snapshot is not None:
                for key in ("authorization_digest", "node_contract_digest"):
                    value = getattr(snapshot, key, None)
                    if isinstance(value, str) and value:
                        provenance[key] = value
            append_event(
                self.paths,
                RunEvent("run_archived", {
                    "run_id": run_id,
                    "status": status,
                    "source": "manual" if force else "terminal",
                }),
                provenance=provenance or None,
            )
        return manifest

    def _require_sessions_retired(self, run_id: str, snapshot: Any = None) -> None:
        from .live_sessions import RunArchiveDeferred, probe_live_sessions

        if snapshot is None:
            try:
                snapshot = load_snapshot(self.paths, run_id)
            except (OSError, ValueError, TypeError):
                snapshot = None
        probe = probe_live_sessions(self.paths, snapshot)
        if probe["unknown"]:
            raise RunArchiveDeferred(
                "run {} was not archived: {}".format(run_id, probe["unknown"]))
        if probe["live"]:
            raise RunArchiveDeferred(
                "run {} was not archived: session(s) still alive: {}; stop them first"
                .format(run_id, ", ".join(probe["live"])))

    def _record_archive_failed(self, run_id: str, error: BaseException) -> None:
        """Append a ``run_archive_failed`` event with the failure reason class.

        Best-effort: recording must never mask or replace the original
        failure, so any error here is swallowed.  Duplicate failure events
        for the same reason class are suppressed so the auto-sweep does not
        flood the log on every monitor entry.  When the run directory is
        already gone from ``.vibe/runs/`` the event lands inside
        ``.vibe/runs-archive/<run_id>/`` via :func:`run_dir`'s redirect.
        """
        try:
            directory = run_dir(self.paths, run_id)
        except (OSError, ValueError):
            return
        if not directory.is_dir():
            # No evidence directory exists anywhere (e.g. a stateless run
            # whose artifacts failed before the archive root was created);
            # appending here would resurrect a stub under .vibe/runs/.
            return
        reason_class = type(error).__name__
        try:
            records = load_events(self.paths, run_id)
        except (OSError, ValueError):
            records = []
        if records:
            last = records[-1]
            if (
                last.get("event") == "run_archive_failed"
                and last.get("data", {}).get("reason_class") == reason_class
            ):
                return
        try:
            append_event(
                self.paths,
                RunEvent("run_archive_failed", {
                    "run_id": run_id,
                    "reason_class": reason_class,
                }),
            )
        except (OSError, ValueError, TypeError):
            pass

    def reconcile_evidence(self, run_id: str, package: Dict[str, Any]) -> RunSnapshot:
        """Promote a verified, same-run evidence package through normal events.

        This path is deliberately provider-free: it only reuses the original
        task bindings and appends ordinary ``delivered``/``accepted`` events.
        Any malformed, stale, mixed, or unverifiable package is rejected before
        the first event is appended.
        """
        if isinstance(package, dict) and package.get("kind") == "run_archive":
            # Manual archive of a dead run (issue #274): works even when the
            # run has no loadable snapshot, because the archive contract is
            # plan-independent filesystem evidence.
            if package.get("run_id") not in (None, run_id):
                raise ValueError("run_archive package run_id does not match --run-id")
            self.archive_run(run_id, force=package.get("force") is True)
            try:
                return load_snapshot(self.paths, run_id)
            except (OSError, ValueError) as error:
                raise ValueError(
                    "run {} is archived but has no loadable snapshot: {}"
                    .format(run_id, error)
                )
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        if any(node.get("workspace_reconciliation_required") is True for node in snapshot.nodes.values()):
            raise PermissionError("workspace reconciliation required before evidence recovery")
        self._validate_reconciliation_package(snapshot, package)
        if all(node.get("status") == "accepted" for node in snapshot.nodes.values()):
            return snapshot
        for item in package["nodes"]:
            current = snapshot.nodes[item["node_id"]]
            if current.get("active_task") is not None or item["node_id"] in snapshot.handles:
                raise ValueError("reconciliation target has an active writer")

        for item in package["nodes"]:
            node_id = item["node_id"]
            current = snapshot.nodes[node_id]
            developer = self._load_task_binding(snapshot, node_id, "developer")
            reviewer = self._load_task_binding(snapshot, node_id, "reviewer")
            # Establish an in-memory active registration for event application;
            # this is not a new task or writer and is never dispatched.
            dev_handle = str(item["developer"].get("handle_id") or "reconcile:" + node_id + ":developer")
            current["active_role"] = "developer"
            current["active_task"] = {
                "role": "developer", "task_id": developer.task_id,
                "generation": developer.generation, "handle_id": dev_handle,
            }
            snapshot.handles[node_id] = dev_handle
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "delivered",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "evidence": item["developer"].get("evidence_ref", "reconciliation")},
                current["active_task"],
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)
            current.setdefault("evidence", []).append(
                redact_provider_text(item["developer"].get("evidence_ref", "reconciliation"))
            )

            reviewer_handle = str(item["reviewer"].get("handle_id") or "reconcile:" + node_id + ":reviewer")
            current["active_role"] = "reviewer"
            current["active_task"] = {
                "role": "reviewer", "task_id": reviewer.task_id,
                "generation": reviewer.generation, "handle_id": reviewer_handle,
            }
            snapshot.handles[node_id] = reviewer_handle
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "accepted",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "contract_digest": current["contract_digest"],
                 "authorization_epoch": snapshot.authorization_digest,
                 "evidence": {"source": "reconciliation", "clearance": item["reviewer"]["clearance"]}},
                current["active_task"],
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)
            current.setdefault("evidence", []).append(
                redact_provider_text(item["reviewer"].get("evidence_ref", "reconciliation"))
            )
            previous_sequence = snapshot.event_sequence
            self._record(
                snapshot,
                "pair_archived",
                {"run_id": snapshot.run_id, "node_id": node_id,
                 "clearance": current.get("review_clearance", {"p0": 0, "p1": 0, "p2": 0})},
            )
            snapshot.event_sequence = previous_sequence
            self._reconcile_unapplied_events(snapshot)

        self._refresh_run_status(snapshot)
        self._save_snapshot(snapshot)
        return snapshot

    def reconcile_scope(self, run_id: str, package: Dict[str, Any]) -> RunSnapshot:
        """Register mechanical scope for the original active developer.

        This never creates provider actions; the caller delivers the resulting
        evidence through the task's existing authorized continuation channel.
        """
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._reconcile_unapplied_events(snapshot)
        if (not isinstance(package, dict) or set(package) != {
                "kind", "node_id", "task_binding", "consistency"}
                or package.get("kind") != "scope_correction"):
            raise ValueError("scope correction package is invalid")
        node_id = package["node_id"]
        if not isinstance(node_id, str) or node_id not in self.nodes:
            raise ValueError("scope correction node is invalid")
        binding = self._load_task_binding(snapshot, node_id, "developer")
        supplied = TaskBinding.from_dict(package["task_binding"]).to_dict()
        actual = binding.to_dict()
        if self._scope_binding_digest(supplied) != self._scope_binding_digest(actual):
            raise ValueError("scope correction task binding is stale")
        proof = {"consistency": package["consistency"],
                 "active_task": copy.deepcopy(snapshot.nodes[node_id].get("active_task")),
                 "binding_digest": self._scope_binding_digest(actual),
                 "allowlist_before": list(supplied["allowlist"])}
        resolution = self._validate_scope_correction(snapshot, node_id, proof)
        allowed = list(supplied["allowlist"])
        for item in resolution.files:
            if item not in allowed:
                allowed.append(item)
        proof["allowlist_after"] = allowed
        if actual["allowlist"] not in (supplied["allowlist"], allowed):
            raise ValueError("scope correction allowlist is stale")
        if not resolution.scope_expanded_files and actual["allowlist"] == allowed:
            self._save_snapshot(snapshot)
            return snapshot
        previous_sequence = snapshot.event_sequence
        self._record(snapshot, "scope_correction_registered", {
            "run_id": snapshot.run_id, "node_id": node_id, "proof": proof,
            "files": list(resolution.files)})
        snapshot.event_sequence = previous_sequence
        self._reconcile_unapplied_events(snapshot)
        self._save_snapshot(snapshot)
        return snapshot

    @staticmethod
    def _scope_binding_digest(binding: Dict[str, Any]) -> str:
        values = {k: v for k, v in binding.items() if k != "allowlist"}
        return digests.sha256_bytes(json.dumps(values, sort_keys=True).encode())

    def _validate_scope_correction(self, snapshot: RunSnapshot, node_id: str,
                                   proof: Dict[str, Any]):
        required = {"consistency", "active_task", "binding_digest", "allowlist_before"}
        if (not isinstance(proof, dict) or not required.issubset(proof)
                or set(proof) - required - {"allowlist_after"}
                or not isinstance(proof["consistency"], dict)
                or not isinstance(proof["active_task"], dict)
                or not isinstance(proof["binding_digest"], str)
                or len(proof["binding_digest"]) != 64
                or not isinstance(proof["allowlist_before"], list)
                or not all(isinstance(p, str) and p for p in proof["allowlist_before"])):
            raise ValueError("scope correction proof is invalid")
        record = self._require_snapshot_authorization(snapshot)
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if (not isinstance(active, dict) or active.get("role") != "developer"
                or not isinstance(active.get("handle_id"), str) or not active["handle_id"]
                or not isinstance(active.get("task_id"), str) or not active["task_id"]
                or type(active.get("generation")) is not int or active["generation"] < 0
                or active != proof.get("active_task")
                or snapshot.handles.get(node_id) != active.get("handle_id")):
            raise ValueError("scope correction requires the original active developer")
        binding = load_task_binding(self.paths, node_id, "developer", run_id=snapshot.run_id)
        if (binding.status in {"archived", "stopped", "failed", "accepted", "delivered", "completed"}
                or binding.issue_id != node_id or binding.role != "developer"
                or binding.run_id != snapshot.run_id
                or self._worktree_path({"worktree": binding.worktree}).resolve()
                != self._worktree_path(current).resolve()
                or binding.branch != current.get("branch")
                or binding.task_id != active.get("task_id")
                or binding.generation != active.get("generation")
                or binding.task_id != current.get("developer_identity")
                or binding.generation != current.get("developer_generation")
                or self._scope_binding_digest(binding.to_dict()) != proof.get("binding_digest")):
            raise ValueError("scope correction task identity or cursor is stale")
        node = self.nodes[node_id]
        resolution = resolve_consistency(proof.get("consistency"),
            self.plan.decisions, node.contract, list(record.allowed_actions),
            list(record.file_scope), self._consistency_binding(record, node),
            node_files=self._node_scope_files(node_id, current),
            auto_scope_paths=self._auto_scope_paths(),
            occupied_files=self._files_held_by_other_active_nodes(snapshot, node_id),
            acceptance_rule_approved=self._acceptance_rule_approved())
        if (resolution is None or resolution.action not in {"test", "rework", "develop"}
                or resolution.field not in node.contract
                or resolution.value != node.contract[resolution.field]):
            raise ValueError("scope correction is not an authorized unchanged acceptance")
        if any(auto_scope_rule(p, self._auto_scope_paths()) is None
               for p in resolution.files if p not in binding.allowlist):
            raise ValueError("scope correction files are not mechanical")
        return resolution

    def _apply_scope_correction(self, snapshot: RunSnapshot, node_id: str,
                                proof: Dict[str, Any]) -> None:
        resolution = self._validate_scope_correction(snapshot, node_id, proof)
        binding = load_task_binding(self.paths, node_id, "developer", run_id=snapshot.run_id)
        if self._scope_binding_digest(binding.to_dict()) != proof["binding_digest"]:
            raise ValueError("scope correction binding changed during replay")
        before, after = proof.get("allowlist_before"), proof.get("allowlist_after")
        if not isinstance(before, list) or not isinstance(after, list):
            raise ValueError("scope correction allowlist evidence is invalid")
        expected = list(before)
        for item in resolution.files:
            if item not in expected:
                expected.append(item)
        if after != expected or binding.allowlist not in (before, after):
            raise ValueError("scope correction allowlist drift")
        if binding.allowlist != after:
            expected_binding = binding.to_dict()
            binding = replace(binding, allowlist=list(after))
            save_task_binding(self.paths, binding, expected_binding=expected_binding)
            self._binding_cache[(snapshot.run_id, node_id, "developer")] = binding
        current = snapshot.nodes[node_id]
        expanded = current.setdefault("scope_expansions", [])
        for item in resolution.scope_expanded_files:
            if item not in expanded:
                expanded.append(item)
        snapshot.tasks[node_id + ":developer"] = binding.to_dict()

    def _validate_reconciliation_package(
        self, snapshot: RunSnapshot, package: Dict[str, Any]
    ) -> None:
        if not isinstance(package, dict):
            raise ValueError("reconciliation package is invalid")
        required = {
            "schema_version", "run_id", "plan_id", "plan_revision",
            "authorization_digest", "node_contract_digest", "nodes",
        }
        if set(package) != required or package.get("schema_version") != 1:
            raise ValueError("reconciliation package schema is invalid")
        if (
            package["run_id"] != snapshot.run_id
            or package["plan_id"] != self.plan.plan_id
            or package["plan_revision"] != self.plan.version
            or package["authorization_digest"] != snapshot.authorization_digest
            or package["node_contract_digest"] != snapshot.node_contract_digest
        ):
            raise ValueError("reconciliation run or contract digest mismatch")
        records = package["nodes"]
        if not isinstance(records, list):
            raise ValueError("reconciliation package nodes are invalid")
        ids = [item.get("node_id") if isinstance(item, dict) else None for item in records]
        all_accepted = all(current.get("status") == "accepted" for current in snapshot.nodes.values())
        expected_ids = {
            node_id for node_id, current in snapshot.nodes.items()
            if current.get("status") != "accepted"
        }
        if all_accepted and not records:
            return
        if all_accepted:
            expected_ids = set(snapshot.nodes)
        if any(not isinstance(node_id, str) for node_id in ids) or len(ids) != len(set(ids)) or set(ids) != expected_ids:
            raise ValueError("reconciliation package node set is invalid")
        if all_accepted:
            for item in records:
                if not isinstance(item, dict) or set(item) != {"node_id", "developer", "reviewer"}:
                    raise ValueError("reconciliation node record is invalid")
            return
        for item in records:
            node_id = item["node_id"]
            if set(item) != {"node_id", "developer", "reviewer"}:
                raise ValueError("reconciliation node record is invalid")
            current = snapshot.nodes[node_id]
            for role in ("developer", "reviewer"):
                claim = item[role]
                if not isinstance(claim, dict):
                    raise ValueError("reconciliation task evidence is invalid")
                binding = self._load_task_binding(snapshot, node_id, role)
                for key in ("task_id", "generation", "worktree", "branch", "status"):
                    if claim.get(key) != getattr(binding, key):
                        raise ValueError("reconciliation {} identity mismatch".format(role))
                self._validate_head(binding, claim.get("head"))
                if binding.task_id != current.get(role + "_identity"):
                    raise ValueError("reconciliation {} task identity is stale".format(role))
                if binding.worktree != str(current.get("worktree")) or binding.branch != str(current.get("branch")):
                    raise ValueError("reconciliation {} worktree or branch mismatch".format(role))
                self._validate_evidence_file(binding, claim.get("status_file"), "status_file")
                self._validate_evidence_file(binding, claim.get("handoff_file"), "handoff_file")
            reviewer_claim = item["reviewer"]
            clearance = reviewer_claim.get("clearance")
            if not isinstance(clearance, dict) or set(clearance) != {"p0", "p1", "p2"} or any(clearance[key] != 0 for key in clearance):
                raise ValueError("reviewer P0-P2 clearance is not zero")

    def _validate_evidence_file(self, binding: TaskBinding, value: Any, field: str) -> None:
        if not isinstance(value, dict) or set(value) != {"path", "sha256"}:
            raise ValueError("reconciliation {} evidence is invalid".format(field))
        path_value = value["path"]
        digest = value["sha256"]
        if not isinstance(path_value, str) or not path_value or Path(path_value).is_absolute() or "\x00" in path_value:
            raise ValueError("reconciliation {} path is invalid".format(field))
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("reconciliation {} hash is invalid".format(field))
        expected = getattr(binding, field)
        if expected and path_value != expected:
            raise ValueError("reconciliation {} path does not match binding".format(field))
        root = self._worktree_path({"worktree": binding.worktree})
        candidate = (root / path_value).resolve(strict=False)
        try:
            candidate.relative_to(root.resolve())
        except ValueError as error:
            raise ValueError("reconciliation evidence path escapes worktree") from error
        if candidate.is_symlink() or not candidate.is_file():
            raise ValueError("reconciliation evidence file is unavailable")
        if digests.sha256_bytes(candidate.read_bytes()) != digest:
            raise ValueError("reconciliation evidence hash mismatch")

    def _validate_head(self, binding: TaskBinding, claimed: Any) -> None:
        if not isinstance(claimed, str) or len(claimed) != 40 or any(c not in "0123456789abcdef" for c in claimed.lower()):
            raise ValueError("reconciliation HEAD is invalid")
        worktree = self._worktree_path({"worktree": binding.worktree})
        try:
            observed = subprocess.run(
                ["git", "-C", str(worktree), "rev-parse", "--verify", "HEAD"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            observed_branch = subprocess.run(
                ["git", "-C", str(worktree), "branch", "--show-current"],
                check=True, capture_output=True, text=True, timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as error:
            raise ValueError("reconciliation HEAD is unavailable") from error
        if observed.lower() != claimed.lower():
            raise ValueError("reconciliation HEAD mismatch")
        if observed_branch != binding.branch:
            raise ValueError("reconciliation branch mismatch: {} != {}".format(observed_branch, binding.branch))

    def _poll_active_handles(self, snapshot: RunSnapshot, runner: Runner) -> None:
        for node_id, handle_id in list(snapshot.handles.items()):
            self._require_snapshot_authorization(snapshot)
            current = snapshot.nodes.get(node_id, {})
            if handle_id in (current.get("stale_handles") or ()):
                # A newer dispatch for this node superseded the handle; a
                # dead handle is never polled and can never emit the
                # unregistered-handle blocked_unknown again.
                continue
            try:
                events = runner.poll(RunHandle(handle_id))
            except Exception as error:
                reason = "runner poll failed ({})".format(type(error).__name__)
                if classify_provider_failure({"reason": reason}).get("legacy_kind") == "engineering":
                    active = snapshot.nodes[node_id].get("active_task") or {}
                    self._queue_active_retry(snapshot, node_id, reason)
                    if active.get("role"):
                        self._set_binding_status(snapshot, node_id, str(active["role"]), "blocked_unknown")
                    snapshot.nodes[node_id]["status"] = self._v42_retry_status()
                    snapshot.nodes[node_id]["reason"] = reason
                else:
                    self._mark_blocked_unknown(
                        snapshot, node_id, reason,
                        quarantine_lease=False,
                        retryable_same_task=True,
                        cause=error,
                    )
                continue
            for event in events:
                if event.event in {"context_overflow", "context_exhausted", "overflow"}:
                    active = snapshot.nodes[node_id].get("active_task")
                    self._record_runner_event(snapshot, node_id, event, active or {})
                    self._checkpoint_context(snapshot, "provider reported context overflow", exhausted=True)
                    continue
                self._apply_event(snapshot, node_id, handle_id, event, runner)

    def _require_record(self, record: Optional[AuthorizationRecord]) -> None:
        if record is None or not is_authorization_valid(
            record, self.plan, list(self.nodes.values())
        ):
            raise PermissionError("a valid executable-contract authorization is required")
        if record.node_ids != tuple(sorted(self.nodes)):
            raise PermissionError("authorization node scope does not match monitor scope")
        self._execution_engine_binding(record)

    def _snapshot_record(self, snapshot: RunSnapshot) -> AuthorizationRecord:
        try:
            return AuthorizationRecord.from_dict(snapshot.authorization)
        except (TypeError, ValueError) as error:
            raise PermissionError("snapshot authorization record is invalid") from error

    def _node_scope_files(self, node_id: str, current: Dict[str, Any]) -> List[str]:
        """The node's authorized files plus files auto-added to its scope."""
        files = list(_scoped_values(self.nodes[node_id].contract, "files"))
        for item in current.get("scope_expansions", []):
            if item not in files:
                files.append(item)
        return files

    def _files_held_by_other_active_nodes(
        self, snapshot: RunSnapshot, node_id: str
    ) -> List[str]:
        """Write scope of every other node that is not yet accepted.

        The write scope is what the node may actually write -- its allowlist
        and owned paths (`dag._write_scope_paths`) -- not just the `files` it
        listed: a node without `files` gets the whole-repository allowlist
        ``["."]`` from `node_spec`.  An unverifiable scope counts as ``"."``.
        Earlier auto-expansions of that node are added on top.
        """
        held: List[str] = []
        for other_id, other in snapshot.nodes.items():
            if other_id == node_id or other_id not in self.nodes:
                continue
            # The integration reviewer lists every business file but is
            # read-only: it never writes, so it holds nothing.
            if is_integration_review_node(self.nodes[other_id]):
                continue
            if other.get("status") == "accepted":
                continue
            # A planned downstream node has no writer until its dispatch
            # transaction creates an active task, start intent, or lease.
            # Counting its eventual scope here creates a dependency cycle:
            # the upstream correction can never start while the dormant
            # downstream node waits for that correction.  Unknown states stay
            # conservative and continue to hold their scope.
            def _is_downstream(candidate: str, ancestor: str, seen=None) -> bool:
                seen = set() if seen is None else seen
                if candidate in seen:
                    return False
                seen.add(candidate)
                dependencies = self.nodes.get(candidate).depends_on if candidate in self.nodes else []
                return ancestor in dependencies or any(_is_downstream(dep, ancestor, seen) for dep in dependencies)
            if (
                other.get("status") == "planned"
                and _is_downstream(other_id, node_id)
                and not other.get("active_task")
                and not other.get("start_intent")
            ):
                worktree = str(other.get("worktree") or "")
                lease = None
                lease_unknown = False
                if not worktree and hasattr(self, "paths"):
                    lease_unknown = True
                if worktree and hasattr(self, "paths"):
                    try:
                        lease = read_writer_lease(self.paths, other_id, worktree)
                    except (OSError, TypeError, ValueError):
                        # A lease read failure is unresolved ownership, not
                        # proof that the planned writer is dormant.
                        lease_unknown = True
                    else:
                        # ``read_writer_lease`` intentionally maps malformed
                        # records to ``None``.  Before treating that as a
                        # clean absence, inspect the lease directory so a
                        # corrupt ownership record remains fail-closed.
                        lease_dir = self.paths.vibe / "leases"
                        try:
                            for candidate in lease_dir.glob("*.json"):
                                try:
                                    raw = json.loads(candidate.read_text(encoding="utf-8"))
                                except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError):
                                    lease_unknown = True
                                    break
                                if (
                                    isinstance(raw, dict)
                                    and raw.get("node_id") == other_id
                                    and raw.get("worktree") == worktree
                                    and raw.get("status") in {"active", "quarantined"}
                                    and lease is None
                                ):
                                    lease_unknown = True
                                    break
                        except OSError:
                            lease_unknown = True
                pending_request = False
                pending_unknown = False
                if hasattr(self, "paths"):
                    try:
                        store = ProviderActionStore(self.paths)
                        pending_request = any(
                            store.has_request(snapshot.run_id, other_id, role)
                            for role in ("developer", "reviewer")
                        )
                    except (OSError, TypeError, ValueError):
                        pending_unknown = True
                if lease_unknown or pending_unknown or pending_request:
                    # Preserve the downstream reservation until every local
                        # ownership/provider side effect is positively absent.
                    write_scope = _write_scope_paths(self.nodes[other_id])
                    held.extend(["."] if write_scope is None else write_scope)
                    held.extend(self._node_scope_files(other_id, other))
                    continue
                if lease is None or not getattr(lease, "active", False):
                    continue
            write_scope = _write_scope_paths(self.nodes[other_id])
            held.extend(["."] if write_scope is None else write_scope)
            held.extend(self._node_scope_files(other_id, other))
        return held

    def _auto_scope_paths(self) -> Tuple[str, ...]:
        # An unreadable or invalid config never widens scope: fall back to
        # the empty list so the correction stops for the user instead.
        try:
            return load_project_config(self.paths.root).auto_scope_paths
        except ValueError:
            return ()

    def _acceptance_rule_approved(self) -> bool:
        # Invalid config never widens scope (integrated PRD N5).
        try:
            return load_project_config(self.paths.root).acceptance_scope_rule_approved
        except ValueError:
            return False

    def _resolve_scope_request(
        self, snapshot: RunSnapshot, node_id: str, request: Any
    ):
        record = self._snapshot_record(snapshot)
        current = snapshot.nodes[node_id]
        return resolve_scope_expansion(
            request,
            authorized_files=list(record.file_scope),
            node_files=self._node_scope_files(node_id, current),
            auto_scope_paths=self._auto_scope_paths(),
            occupied_files=self._files_held_by_other_active_nodes(
                snapshot, node_id
            ),
            acceptance_rule_approved=self._acceptance_rule_approved(),
        )

    @staticmethod
    def _queue_scope_rework(current: Dict[str, Any]) -> None:
        current["acceptance"] = None
        current.pop("review_clearance", None)
        current["status"] = "rework"
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = None
        current["pair_archived"] = True
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": True,
            "pending_schedule": True,
            "successor": False,
            "successor_candidate": False,
        }

    @staticmethod
    def _invalidate_integration_review(snapshot: RunSnapshot) -> None:
        current = snapshot.nodes.get("integration-review")
        if not isinstance(current, dict) or current.get("status") != "accepted":
            return
        prior = snapshot.integration_review_evidence
        history = []
        if isinstance(prior, dict) and isinstance(prior.get("history"), list):
            history = copy.deepcopy(prior["history"])
        elif prior:
            history = [copy.deepcopy(prior)]
        snapshot.integration_review_evidence = {"history": history}
        current["acceptance"] = None
        current.pop("review_clearance", None)
        current["status"] = "rework"
        current["reason"] = None
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = None
        current["pair_archived"] = True
        current["retryable_action"] = {
            "role": "reviewer",
            "phase": "review",
            "continuation": True,
            "pending_schedule": True,
            "successor": False,
            "successor_candidate": False,
        }

    def _queue_integration_review_rework(self, snapshot: RunSnapshot) -> None:
        current = snapshot.nodes.get("integration-review")
        if not isinstance(current, dict) or current.get("status") not in {
            "review", "rework"
        }:
            return
        current["status"] = "rework"
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = None
        current["retryable_action"] = {
            "role": "reviewer",
            "phase": "review",
            "continuation": True,
            "pending_schedule": True,
            "successor": False,
            "successor_candidate": False,
        }
        snapshot.handles.pop("integration-review", None)
        self._release_node_lease(snapshot, "integration-review")

    def _apply_scope_expansion(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        resolution: Any,
        trigger: str,
        *,
        queue_rework: bool = False,
        active: Optional[Dict[str, Any]] = None,
    ) -> None:
        current = snapshot.nodes[node_id]
        expansions = current.setdefault("scope_expansions", [])
        for item in resolution.files:
            if item not in expansions:
                expansions.append(item)
        event_data = {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "files": list(resolution.files),
            "scope_rules": list(resolution.scope_rules),
        }
        if resolution.reason or queue_rework:
            event_data.update(
                {
                    "trigger": trigger,
                    "acceptance_examples_unchanged": bool(resolution.reason),
                    "scope_reason": resolution.reason,
                    "scope_transition": (
                        "queue_rework" if queue_rework else "none"
                    ),
                }
            )
        self._record(
            snapshot,
            "scope_auto_expanded",
            event_data,
            active,
        )
        if queue_rework:
            self._queue_scope_rework(current)
            if trigger == "integration_rework":
                self._queue_integration_review_rework(snapshot)
            else:
                self._invalidate_integration_review(snapshot)

    def request_scope_expansion(
        self,
        run_id: str,
        node_id: str,
        request: Any,
        *,
        trigger: str = "accepted_patch",
    ) -> RunSnapshot:
        """Queue an accepted-node patch through the original task identities."""

        if trigger != "accepted_patch":
            raise ValueError("unsupported scope expansion trigger")
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        self._require_snapshot_authorization(snapshot)
        self._reconcile_unapplied_events(snapshot)
        if node_id not in self.nodes or node_id not in snapshot.nodes:
            raise ValueError("scope expansion node is outside the authorized DAG")
        if is_integration_review_node(self.nodes[node_id]):
            raise ValueError("accepted patch requires a business node")
        current = snapshot.nodes[node_id]
        if (
            current.get("status") != "accepted"
            or current.get("active_task") is not None
            or node_id in snapshot.handles
        ):
            raise ValueError("accepted patch requires an inactive accepted node")
        resolution = self._resolve_scope_request(snapshot, node_id, request)
        if resolution is None or not resolution.files:
            raise ScopeDecisionRequired("scope expansion requires product-manager review")
        self._apply_scope_expansion(
            snapshot,
            node_id,
            resolution,
            trigger,
            queue_rework=True,
        )
        self._refresh_run_status(snapshot)
        self._save_snapshot(snapshot)
        return snapshot

    def _consistency_binding(
        self, record: AuthorizationRecord, node: DAGNode
    ) -> Dict[str, Any]:
        project_root = str(self.paths.root.resolve())
        return {
            "schema_version": 1,
            "project_digest": digests.sha256_bytes(project_root.encode("utf-8")),
            "plan_id": self.plan.plan_id,
            "plan_version": self.plan.version,
            "decision_digest": record.decision_digest,
            "authorization_digest": record.digest,
            "issue_contract_digest": executable_contract_digest([node]),
        }

    def _require_snapshot_authorization(self, snapshot: RunSnapshot) -> AuthorizationRecord:
        record = self._snapshot_record(snapshot)
        if snapshot.plan_id != self.plan.plan_id or snapshot.plan_version != self.plan.version:
            raise PermissionError("snapshot plan no longer matches monitor plan")
        if snapshot.authorization_digest != record.digest:
            raise PermissionError("snapshot authorization digest is inconsistent")
        if snapshot.node_contract_digest != record.node_contract_digest:
            raise PermissionError("snapshot contract digest is inconsistent")
        self._require_record(record)
        return record

    def _apply_reauthorization_transition(
        self, snapshot: RunSnapshot, data: Dict[str, Any]
    ) -> None:
        previous = AuthorizationRecord.from_dict(data.get("previous_authorization"))
        replacement = AuthorizationRecord.from_dict(data.get("new_authorization"))
        if (
            previous.to_dict() != snapshot.authorization
            or data.get("previous_authorization_digest")
            != snapshot.authorization_digest
            or data.get("previous_node_contract_digest")
            != snapshot.node_contract_digest
            or data.get("previous_capability_contract_digest")
            != snapshot.capability_contract_digest
        ):
            raise ValueError("reauthorization previous lineage is inconsistent")
        self._require_record(replacement)
        if (
            data.get("authorization_digest") != replacement.digest
            or data.get("node_contract_digest")
            != replacement.node_contract_digest
            or data.get("change_reason")
            not in {
                "executable_contract_changed",
                "capability_contract_changed",
                "engine_attestation_renewed",
                "platform_handoff",
            }
        ):
            raise ValueError("reauthorization replacement lineage is inconsistent")
        replacement_capability_digest = data.get("capability_contract_digest")
        if replacement_capability_digest:
            if (
                not isinstance(replacement_capability_digest, str)
                or len(replacement_capability_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in replacement_capability_digest
                )
            ):
                raise ValueError("reauthorization capability contract lineage is invalid")
        elif snapshot.capability_contract_digest:
            raise ValueError("reauthorization capability contract lineage is missing")
        continuation = data.get("continuation")
        if not isinstance(continuation, dict):
            raise ValueError("reauthorization continuation evidence is invalid")
        # Events recorded before the lineage re-pin existed carry none of the
        # four keys; defaulting each to the snapshot's own value keeps those
        # replays a no-op instead of turning a legacy log into a hard failure.
        previous_prd_digest = data.get("previous_prd_digest", snapshot.prd_digest)
        previous_spec_digest = data.get("previous_spec_digest", snapshot.spec_digest)
        replacement_prd_digest = data.get("prd_digest", snapshot.prd_digest)
        replacement_spec_digest = data.get("spec_digest", snapshot.spec_digest)
        if not all(
            isinstance(value, str)
            for value in (
                previous_prd_digest,
                previous_spec_digest,
                replacement_prd_digest,
                replacement_spec_digest,
            )
        ):
            raise ValueError("reauthorization plan lineage is invalid")
        if (
            previous_prd_digest != snapshot.prd_digest
            or previous_spec_digest != snapshot.spec_digest
        ):
            raise ValueError("reauthorization plan lineage is inconsistent")
        for key, evidence in continuation.items():
            if not isinstance(key, str) or ":" not in key or not isinstance(evidence, dict):
                raise ValueError("reauthorization continuation evidence is invalid")
            node_id, role = key.rsplit(":", 1)
            if node_id not in snapshot.nodes or role not in {"developer", "reviewer"}:
                raise ValueError("reauthorization continuation identity is invalid")
            binding = self._load_task_binding(snapshot, node_id, role)
            if (
                set(evidence) != {"task_id", "cursor"}
                or evidence["task_id"] != binding.task_id
                or evidence["cursor"] != binding.cursor
            ):
                raise ValueError("reauthorization continuation cursor is inconsistent")
            snapshot.tasks[key] = binding.to_dict()

        previous_node_contract_digests = data.get("previous_node_contract_digests")
        node_contract_digests = data.get("node_contract_digests")
        if (
            not isinstance(previous_node_contract_digests, dict)
            or not isinstance(node_contract_digests, dict)
            or set(previous_node_contract_digests) != set(snapshot.nodes)
            or set(node_contract_digests) != set(snapshot.nodes)
            or any(
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
                for mapping in (previous_node_contract_digests, node_contract_digests)
                for value in mapping.values()
            )
        ):
            raise ValueError("reauthorization node contract lineage is invalid")
        retained_acceptances = data.get("retained_acceptances")
        invalidated_acceptances = data.get("invalidated_acceptances")
        changed_nodes = data.get("changed_nodes")
        affected_nodes = data.get("affected_nodes")
        accepted_nodes = data.get("accepted_nodes")
        authorized_node_contracts = data.get("authorized_node_contracts")
        if not isinstance(retained_acceptances, dict) or not isinstance(
            invalidated_acceptances, dict
        ) or not isinstance(authorized_node_contracts, dict) or set(
            authorized_node_contracts
        ) != set(snapshot.nodes) or any(
            not isinstance(value, str)
            for value in authorized_node_contracts.values()
        ) or any(
            not isinstance(items, list)
            or any(not isinstance(node_id, str) for node_id in items)
            or len(items) != len(set(items))
            or items != sorted(items)
            or any(node_id not in snapshot.nodes for node_id in items)
            for items in (changed_nodes, affected_nodes, accepted_nodes)
        ):
            raise ValueError("reauthorization acceptance lineage is invalid")
        if set(retained_acceptances) & set(invalidated_acceptances):
            raise ValueError("reauthorization acceptance lineage overlaps")
        if data.get("change_reason") == "platform_handoff" and (
            changed_nodes
            or invalidated_acceptances
            or previous.file_scope != replacement.file_scope
        ):
            # Replay twin of the reauthorize gate: a handoff never moves the
            # acceptance contract, so it can neither change nor invalidate.
            raise ValueError("platform handoff transition changed the acceptance contract")
        previous_authorized_nodes = self._previous_authorized_nodes(snapshot, previous)
        expected_changed_nodes = sorted(
            node_id
            for node_id in snapshot.nodes
            if (
                node_id == "integration-review" and (
                    previous.integration_contract_digest != replacement.integration_contract_digest
                    or replacement_prd_digest != snapshot.prd_digest
                    or replacement_spec_digest != snapshot.spec_digest
                )
            ) or not (
                route_only_acceptance_binds(
                    self.nodes[node_id], previous_authorized_nodes.get(node_id),
                    previous_node_contract_digests[node_id],
                )
                or executable_contract_digest([self.nodes[node_id]]) == previous_node_contract_digests[node_id]
                or scope_only_expansion_binds(
                    self.nodes[node_id],
                    previous_node_contract_digests[node_id],
                    previous.file_scope,
                    replacement.file_scope,
                )
            )
        )
        if changed_nodes != expected_changed_nodes:
            raise ValueError("reauthorization changed node lineage is invalid")
        expected_authorized_node_contracts = {
            item["id"]: json.dumps(
                item,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for item in canonical_node_contracts(list(self.nodes.values()))
        }
        if authorized_node_contracts != expected_authorized_node_contracts:
            raise ValueError("reauthorization authorized DAG proof is invalid")
        if "node_scope_digests" in data and data["node_scope_digests"] != {
            key: legacy_executable_contract_digest([node]) for key, node in self.nodes.items()
        }:
            raise ValueError("reauthorization per-node scope proof is invalid")
        if affected_nodes != affected_node_closure(
            list(self.nodes.values()), changed_nodes
        ):
            raise ValueError("reauthorization affected suffix lineage is invalid")
        retained_ids = set(retained_acceptances)
        invalidated_ids = set(invalidated_acceptances)
        current_accepted_ids = {
            node_id
            for node_id, current in snapshot.nodes.items()
            if current.get("status") == "accepted"
        }
        if (
            accepted_nodes != sorted(retained_ids | invalidated_ids)
            or current_accepted_ids != set(accepted_nodes)
            or set(changed_nodes) - set(affected_nodes)
            or retained_ids & set(affected_nodes)
            or not invalidated_ids.issubset(set(affected_nodes))
            or retained_ids | invalidated_ids != set(accepted_nodes)
        ):
            raise ValueError("reauthorization affected suffix disposition is invalid")

        for node_id, evidence in retained_acceptances.items():
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"contract_digest", "authorization_epoch"}
                or evidence["contract_digest"]
                != previous_node_contract_digests[node_id]
                or not (
                    route_only_acceptance_binds(
                        self.nodes[node_id], previous_authorized_nodes.get(node_id),
                        evidence["contract_digest"],
                    )
                    or executable_contract_digest([self.nodes[node_id]]) == evidence["contract_digest"]
                    or scope_only_expansion_binds(
                        self.nodes[node_id],
                        evidence["contract_digest"],
                        previous.file_scope,
                        replacement.file_scope,
                    )
                )
                or evidence["authorization_epoch"] != previous.digest
            ):
                raise ValueError("retained acceptance evidence is invalid")
        for node_id, evidence in invalidated_acceptances.items():
            if (
                not isinstance(evidence, dict)
                or set(evidence) != {"contract_digest", "authorization_epoch"}
                or evidence["contract_digest"]
                != previous_node_contract_digests[node_id]
                or evidence["authorization_epoch"] != previous.digest
            ):
                raise ValueError("invalidated acceptance evidence is invalid")

        retained_integration_package = None
        if "integration-review" in retained_acceptances and snapshot.integration_review_evidence:
            from .evidence import validate_integration_review_evidence
            retained_integration_package = copy.deepcopy(snapshot.integration_review_evidence)
            validate_integration_review_evidence(
                snapshot, {key: value for key, value in retained_integration_package.items() if key != "history"}
            )
            if replacement_prd_digest != snapshot.prd_digest or replacement_spec_digest != snapshot.spec_digest:
                raise ValueError("retained integration review cannot change PRD/Spec lineage")
        snapshot.authorization = replacement.to_dict()
        snapshot.authorization_digest = replacement.digest
        snapshot.node_contract_digest = replacement.node_contract_digest
        snapshot.capability_contract_digest = replacement_capability_digest or ""
        snapshot.prd_digest = replacement_prd_digest
        snapshot.spec_digest = replacement_spec_digest
        if retained_integration_package is not None:
            history = list(retained_integration_package.pop("history", []))
            history.append(copy.deepcopy(retained_integration_package))
            retained_integration_package.update(
                authorization_digest=replacement.digest,
                node_contract_digest=replacement.node_contract_digest,
            )
            validate_integration_review_evidence(snapshot, retained_integration_package)
            retained_integration_package["history"] = history
            snapshot.integration_review_evidence = retained_integration_package
        snapshot.status = "running"
        snapshot.handles.clear()
        platform_handoff = data.get("change_reason") == "platform_handoff"
        for node_id, current in snapshot.nodes.items():
            current["contract_digest"] = node_contract_digests[node_id]
            if platform_handoff:
                # #289 section 3: every task bound before the handoff lives
                # on the old platform and cannot be resumed from the new one.
                # Record which identities are superseded; _start_task turns
                # a later resume of exactly these into a successor.  Tasks
                # created after the handoff are not listed, so their rework
                # keeps returning to the same worker.
                superseded = {
                    role: current[role + "_identity"]
                    for role in ("developer", "reviewer")
                    if current.get(role + "_identity")
                }
                if superseded:
                    current["handoff_superseded"] = superseded
            current["active_role"] = None
            current["active_task"] = None
            current["start_intent"] = None
            current["quarantine"] = None
            continuation_role = (
                "reviewer" if is_integration_review_node(self.nodes[node_id]) else "developer"
            )
            if current.get("status") == "accepted":
                if node_id in retained_acceptances:
                    # The retained acceptance now binds the replacement digest,
                    # which differs from the evidence only for 5.0.7 runs.
                    current["acceptance"] = {
                        "contract_digest": node_contract_digests[node_id],
                        "authorization_epoch": replacement.digest,
                    }
                    continue
                if node_id in invalidated_acceptances:
                    current["acceptance"] = None
                    self._queue_reauthorization_continuation(
                        current,
                        self._continuation_requires_successor(
                            snapshot, node_id, continuation_role
                        ),
                        continuation_role,
                    )
                    continue
                raise ValueError("accepted node lacks reauthorization disposition")
            generation_key = (
                "review_generation" if continuation_role == "reviewer" else "developer_generation"
            )
            if int(current.get(generation_key, 0)) > 0:
                self._queue_reauthorization_continuation(
                    current,
                    self._continuation_requires_successor(
                        snapshot, node_id, continuation_role
                    ),
                    continuation_role,
                )
            else:
                current["status"] = "planned"
                current["retryable_action"] = None

    def _continuation_requires_successor(
        self, snapshot: RunSnapshot, node_id: str, role: str
    ) -> bool:
        """Return whether reauthorization needs a new task identity.

        A provider-confirmed terminal binding is still a canonical task that
        can be continued.  Only a missing binding (or a non-terminal binding
        whose state must be reconciled by the scheduler) is a successor
        candidate.  Malformed registry data remains fail-closed: the
        scheduler will keep the node blocked rather than guessing.
        """
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
        except FileNotFoundError:
            return True
        except (OSError, TypeError, ValueError):
            return True
        # A provider-confirmed delivery is a terminal developer handoff: the
        # same visible task can be resumed for authorized rework.  Treating
        # it as a successor candidate would require a stop/absence proof even
        # though no active writer remains, leaving ordinary delivered work
        # permanently blocked.  Unknown/running/review states remain
        # successor candidates and still require the fail-closed proof below.
        return binding.status not in {"delivered", "stopped", "failed", "archived"}

    @staticmethod
    def _queue_reauthorization_continuation(
        current: Dict[str, Any], successor_candidate: bool, role: str = "developer"
    ) -> None:
        current["pair_archived"] = True
        current["status"] = "blocked_unknown"
        current["retryable_action"] = {
            "role": role,
            "phase": "review" if role == "reviewer" else "rework",
            "continuation": True,
            "pending_schedule": True,
            "successor_candidate": successor_candidate,
        }

    def _recover_quarantined_delivered_developer(
        self, snapshot: RunSnapshot, node_id: str, record_event: bool = True
    ) -> bool:
        """Rebuild a lost continuation marker from a delivered binding.

        An interrupted reauthorization can persist the quarantine transition
        after the retry marker was lost.  A current-run delivered developer
        binding is sufficient evidence to continue that same visible task;
        it is not evidence for creating a successor or another writer.
        """
        current = snapshot.nodes[node_id]
        if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
            # A delivered visible-sdd binding only reaches blocked_unknown
            # when its in-session review evidence was rejected.  That is a
            # fail-closed terminal doubt, not a recoverable continuation.
            return False
        if (
            current.get("status") != "blocked_unknown"
            or not isinstance(current.get("quarantine"), dict)
            or current.get("active_task") is not None
            or current.get("active_role") is not None
            or current.get("start_intent") is not None
            or node_id in snapshot.handles
            or current.get("retryable_action") is not None
            # If reviewer dispatch already began, a delivered developer is
            # not a rework candidate.  Recovery must reconstruct the missing
            # reviewer binding and keep the node fail-closed until then.
            or current.get("reviewer_started") is True
        ):
            return False
        try:
            binding = self._load_task_binding(snapshot, node_id, "developer")
            generation = int(current.get("developer_generation", 0))
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return False
        if (
            binding.status != "delivered"
            or not binding.task_id
            or binding.task_id != current.get("developer_identity")
            or generation <= 0
            or binding.generation <= 0
            or binding.generation > generation
        ):
            return False
        current["retryable_action"] = {
            "role": "developer",
            "phase": "rework",
            "continuation": True,
            "pending_schedule": True,
            "successor_candidate": False,
        }
        current["pair_archived"] = True
        current["quarantine"] = None
        current["reason"] = "quarantined delivered continuation recovered"
        if record_event:
            self._record(
                snapshot,
                "quarantine_continuation_recovered",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": "developer",
                    "task_id": binding.task_id,
                    "generation": generation,
                },
            )
        return True

    @staticmethod
    def _identity_from_contract(node: DAGNode, role: str) -> Optional[str]:
        return monitor_handoff._identity_from_contract(node, role)

    def _run_has_platform_handoff(self, snapshot: RunSnapshot) -> bool:
        """Whether this run recorded a platform_handoff reauthorization."""
        return any(
            event.get("event") == "authorization_reauthorized"
            and event.get("data", {}).get("change_reason") == "platform_handoff"
            for event in load_events(self.paths, snapshot.run_id)[: snapshot.event_sequence]
        )

    @staticmethod
    def _handoff_superseded_identity(current: Dict[str, Any], role: str) -> Optional[str]:
        return monitor_handoff._handoff_superseded_identity(current, role)

    #: Termination basis for a handoff predecessor: "already delivered".  After
    #: a platform switch the old platform cannot be queried, so there is no
    #: stop confirmation to ask for; the local registry saying the task handed
    #: over its work is the only proof.  Any other state, or a missing or
    #: mismatched binding, is unproven and the node goes to blocked_unknown.
    #: Binding states that are already a terminal handoff of the old task: a
    #: delivered developer, a reviewer that returned its review, an accepted
    #: pair.  After a platform handoff no rework or re-review can ever return
    #: to such a task, so archiving it is the "original task lands terminal"
    #: step of #289 section 3.  Live or unknown states are never archived.
    _HANDOFF_ARCHIVABLE_STATUSES = {
        "developer": frozenset({"delivered", "accepted"}),
        "reviewer": frozenset({"review", "delivered", "accepted"}),
    }

    def _prove_handoff_predecessor_terminal(self, snapshot: RunSnapshot, node_id: str, role: str) -> Optional[Tuple[str, str]]:
        return monitor_handoff._prove_handoff_predecessor_terminal(self, snapshot, node_id, role)

    def _prove_old_task_stopped_or_absent(self, snapshot: RunSnapshot, node_id: str, retry: Dict[str, Any]) -> Optional[str]:
        return monitor_handoff._prove_old_task_stopped_or_absent(self, snapshot, node_id, retry)

    def _live_node_contract_digest(self, node_id: str) -> str:
        """Recompute the node contract digest from the live node contract.

        V4.7 ISSUE-02: every path that turns a visible-sdd accepted event
        into an accepted node compares the digest the event carries against
        this recomputation, not merely against the snapshot copy stamped at
        dispatch.  An empty or non-dict contract is unreadable and raises;
        ``executable_contract_digest`` itself would happily digest ``None``,
        so the refusal has to happen here.
        """
        node = self.nodes[node_id]
        contract = getattr(node, "contract", None)
        if not isinstance(contract, dict) or not contract:
            raise ValueError("node contract content is unreadable")
        return executable_contract_digest([node])

    def _live_node_contract_digest_matches(self, node_id: str, digest: Any) -> bool:
        """Whether ``digest`` binds the live node contract.

        Runs dispatched by 5.0.7 stamped the scope-inclusive digest, so both
        the current and that form are recognized.  Raises like
        ``_live_node_contract_digest`` when the contract is unreadable.
        """
        self._live_node_contract_digest(node_id)
        return executable_contract_digest([self.nodes[node_id]]) == digest

    def _replay_visible_sdd_acceptance(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        data: Dict[str, Any],
        provenance: Dict[str, Any],
    ) -> None:
        """Apply a durable visible-sdd acceptance lost with the snapshot.

        The single-session acceptance binds the developer session identity
        (the in-session reviewer role shares it); there is no reviewer
        binding to load.  Stale identity, lineage or digest evidence raises,
        exactly like the dual-visible replay; missing clearance evidence
        fails closed to ``blocked_unknown`` without archiving.  The carried
        ``contract_digest`` must also equal the digest recomputed from the
        live node contract (ISSUE-02): a tampered contract invalidates the
        event and the node never turns accepted.
        """
        current = snapshot.nodes[node_id]
        if provenance["role"] != "reviewer":
            raise ValueError("unapplied acceptance lacks reviewer provenance")
        generation = int(current.get("developer_generation", 0))
        if (
            provenance["task_id"] != current.get("developer_identity")
            or provenance["generation"] != generation
            or generation <= 0
        ):
            raise ValueError(
                "unapplied visible-sdd acceptance identity or generation is stale"
            )
        try:
            registered = self._load_task_binding(snapshot, node_id, "developer")
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            raise ValueError(
                "unapplied visible-sdd acceptance developer binding is unavailable"
            ) from error
        if (
            registered.task_id != provenance["task_id"]
            or registered.generation != generation
        ):
            raise ValueError(
                "unapplied visible-sdd acceptance registry generation is stale"
            )
        contract_digest = data.get("contract_digest")
        authorization_epoch = data.get("authorization_epoch")
        try:
            live_digest_matches = self._live_node_contract_digest_matches(
                node_id, contract_digest
            )
        except ValueError as error:
            raise ValueError(
                "unapplied visible-sdd acceptance node contract is unreadable"
            ) from error
        if (
            not isinstance(contract_digest, str)
            or len(contract_digest) != 64
            or any(
                character not in "0123456789abcdef" for character in contract_digest
            )
            or contract_digest != current.get("contract_digest")
            or not live_digest_matches
            or authorization_epoch != snapshot.authorization_digest
        ):
            raise ValueError("unapplied acceptance contract epoch is stale")
        self._set_binding_status(snapshot, node_id, "developer", "accepted")
        current["status"] = "accepted"
        current["acceptance"] = {
            "contract_digest": contract_digest,
            "authorization_epoch": authorization_epoch,
        }
        # Match the live path: a replayed acceptance must not resurrect the
        # rejection text the lost snapshot still carried -- nor the #255
        # consecutive-rejection counter or the escalation recovery action the
        # parked node carried into the reconciliation.
        current["reason"] = None
        current.pop("acceptance_format_rejections", None)
        current["recovery_action"] = None
        current["reviewer_identity"] = provenance["task_id"]
        current["review_generation"] = generation
        current["active_role"] = None
        current["active_task"] = None
        current["blocked_unknowns_pending"] = []
        current["quarantine"] = None
        snapshot.handles.pop(node_id, None)
        if not data.get("evidence"):
            current["status"] = "blocked_unknown"
            current["reason"] = (
                "visible-sdd acceptance has no registered in-session review evidence"
            )
        else:
            current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
            self._set_binding_status(snapshot, node_id, "developer", "archived")
            current["pair_archived"] = True
        self._release_node_lease(snapshot, node_id)

    def _reject_replayed_old_task_reconciliation(self, snapshot: RunSnapshot, node_id: str, data: Dict[str, Any]) -> None:
        return monitor_acceptance._reject_replayed_old_task_reconciliation(self, snapshot, node_id, data)

    def _recover_developer_delivery_projection(self, snapshot, node_id, *, require_receipt=False,
                                               replay_sequence=None):
        """Read an already-consumed, bound delivery; never synthesize completion."""
        current = snapshot.nodes[node_id]
        delivery = current.get('delivery_evidence')
        store = ProviderActionStore(self.paths)
        try:
            developer = self._load_task_binding(snapshot, node_id, 'developer')
        except FileNotFoundError:
            # Older reviewer-only successor snapshots legitimately carried a
            # validated delivery projection without a developer registry row.
            # Keep that compatibility path for projection-only recovery, but
            # never allow it to satisfy immutable receipt recovery.
            if require_receipt:
                raise ValueError('successor reconciliation developer delivery binding is unverified')
            developer = None
        generation = current.get('developer_generation')
        status_verified = developer is not None and developer.status == 'delivered'
        if require_receipt and developer is not None and developer.status == 'archived' and type(replay_sequence) is int:
            # A later, authorized pair archive must not invalidate the immutable
            # receipt being replayed. Initial recovery still requires delivered.
            status_verified = any(
                event.get('event') == 'pair_archived'
                and event.get('sequence', 0) > replay_sequence
                and event.get('data', {}).get('run_id') == snapshot.run_id
                and event.get('data', {}).get('node_id') == node_id
                and event.get('data', {}).get('clearance') == {'p0': 0, 'p1': 0, 'p2': 0}
                and event.get('provenance', {}).get('role') == 'system'
                and event.get('provenance', {}).get('authorization_digest') == snapshot.authorization_digest
                and event.get('provenance', {}).get('node_contract_digest') == snapshot.node_contract_digest
                for event in load_events(self.paths, snapshot.run_id)
            )
        if not require_receipt and isinstance(delivery, dict) and delivery.get('completion_marker'):
            if developer is not None and (type(generation) is not int or generation < 1 or developer.generation != generation
                    or developer.task_id != current.get('developer_identity')
                    or not status_verified
                    or developer.worktree != current.get('worktree')
                    or developer.branch != current.get('branch')):
                raise ValueError('successor reconciliation developer delivery binding is unverified')
            return delivery, None
        if developer is None:
            raise ValueError('successor reconciliation developer delivery binding is unverified')
        if (type(generation) is not int or generation < 1 or developer.generation != generation
                or developer.task_id != current.get('developer_identity')
                or not status_verified
                or developer.worktree != current.get('worktree') or developer.branch != current.get('branch')):
            raise ValueError('successor reconciliation developer delivery binding is unverified')
        path = store._directory('deliveries') / f'{snapshot.run_id}-{node_id}-developer-g{generation}.json'
        archived = store._read(path)
        if (archived.get('run_id') != snapshot.run_id or archived.get('issue_id') != node_id
                or archived.get('role') != 'developer' or archived.get('generation') != generation
                or not isinstance(archived.get('consumed_by'), str)):
            raise ValueError('successor reconciliation developer delivery receipt is unverified')
        action = store.action(archived['consumed_by'])
        request = action['request']
        targets = request.get('targets')
        result = store.result(action['action_id'])
        if (action['run_id'] != snapshot.run_id or action['issue_id'] != node_id
                or action['role'] != 'developer' or action['generation'] != generation
                or action['provider'] != developer.provider or action['operation'] != 'wait'
                or request.get('authorization_digest') != snapshot.authorization_digest
                or request.get('plan_id') != self.plan.plan_id or request.get('plan_version') != self.plan.version
                or not isinstance(targets, list) or len(targets) != 1 or not isinstance(targets[0], dict)
                or targets[0].get('threadId') != developer.task_id or targets[0].get('hostId') != developer.host
                or not isinstance(result, dict) or result.get('event') != 'delivered'
                or result.get('status') not in ('complete', 'completed')
                or not isinstance(archived.get('payload'), dict)
                or result.get('delivery_evidence') != archived['payload'].get('delivery_evidence')):
            raise ValueError('successor reconciliation developer delivery action is unverified')
        delivery = result.get('delivery_evidence')
        receipt = store.self_report_receipt(snapshot.run_id, node_id, 'developer', generation,
                                            developer.task_id, developer.host or developer.hostId,
                                            delivery)
        gate = evaluate_delivery_evidence({**current, 'status':'DELIVERED'}, developer, delivery,
                                          self_report_receipt=receipt is not None,
                                          self_report=archived.get('payload'))
        if not gate.complete:
            raise ValueError('successor reconciliation developer delivery is incomplete')
        artifact = Path(delivery['delivery_path'])
        root = self._worktree_path(current).resolve()
        artifact = artifact if artifact.is_absolute() else root/artifact
        if not artifact.resolve().is_relative_to(root) or not artifact.is_file():
            raise ValueError('successor reconciliation developer delivery artifact is unverified')
        return delivery, {'action_id':action['action_id'], 'request_digest':action['request_digest'],
            'archive_path':str(path), 'archive_digest':digests.sha256_bytes(path.read_bytes())}

    def reconcile_package(self, run_id, package, *, runner_factory, invalidation_path, authorization_digest):
        return monitor_handoff.reconcile_package(
            self, run_id, package, runner_factory=runner_factory,
            invalidation_path=invalidation_path, authorization_digest=authorization_digest)

    def reconcile_created_successor(self, run_id, node_id, runner, *, action_id,
                                    request_digest, predecessor_archive, preservation_path,
                                    preservation_digest):
        """Register an existing visible successor after a false local start.

        The archive result is a native observation. This operation rechecks
        current authorization and registers a setup-only task; normal binding
        and writer gates still precede its continuation.
        """
        self._reset_binding_cache()
        snapshot = load_snapshot(self.paths, run_id)
        record = self._require_snapshot_authorization(snapshot)
        current = snapshot.nodes[node_id]
        if (current.get('active_task') is not None or node_id in snapshot.handles
                or current.get('start_intent') is not None):
            raise ValueError('successor reconciliation requires no active task or intent')
        store = ProviderActionStore(self.paths)
        action = store.action(action_id)
        old = load_historical_task_binding(self.paths, run_id, node_id, 'reviewer',
                                          action['request'].get('predecessor_task_id'))
        runner_provider = getattr(runner, 'provider', None)
        # #289: only a run with a recorded platform_handoff may recover a
        # successor whose predecessor was bound by another provider, and only
        # when the create request itself carries that provider as lineage.
        # The archived-predecessor proof below is unchanged.
        handoff = (
            old.provider != runner_provider
            and self._run_has_platform_handoff(snapshot)
            and action['request'].get('handoff_from_provider') == old.provider
            and action.get('provider') == runner_provider
        )
        if (old.mode != 'visible' or (old.provider != runner_provider and not handoff)
                or old.status not in {'stopped', 'failed', 'archived'}
                or not isinstance(predecessor_archive, dict)
                or predecessor_archive.get('threadId') != old.task_id
                or predecessor_archive.get('archived') is not True):
            raise ValueError('successor predecessor archive is unverified')
        if not self._proven_successor_action(snapshot, node_id, old.role, old.generation,
            runner_provider if handoff else old.provider, action_id, request_digest,
            old.task_id, record.digest):
            raise ValueError('successor reconciliation create lineage is invalid')
        profile = action['request'].get('worker_profile')
        if not isinstance(profile, dict):
            raise ValueError('successor reconciliation requires the bound worker profile')
        profile = WorkerProfile.from_dict(profile)
        root = self._worktree_path(current)
        def git(*args):
            result = subprocess.run(['git', '-C', str(root), *args], capture_output=True, text=True)
            if result.returncode:
                raise ValueError('successor reconciliation Git observation failed')
            return result.stdout if args[:1] == ('diff',) else result.stdout.strip()
        if (git('symbolic-ref', '--short', 'HEAD') != old.branch
                or profile.branch != old.branch or Path(profile.worktree).resolve() != root.resolve()
                or profile.allowlist != old.allowlist):
            raise ValueError('successor reconciliation writer scope is unverified')
        head = git('rev-parse', 'HEAD')
        # Reviewer recovery preserves the developer's delivered diff. Its
        # frozen marker must include tracked and untracked content, rather
        # than requiring that legitimate delivery to disappear.
        delivery, delivery_recovery = self._recover_developer_delivery_projection(snapshot, node_id)
        path = Path(preservation_path)
        raw_preservation = path.read_bytes()
        if digests.sha256_bytes(raw_preservation) != preservation_digest:
            raise ValueError('successor reconciliation preservation digest mismatch')
        preserved = json.loads(raw_preservation)
        if (not isinstance(preserved, dict)
                or not isinstance(preserved.get('reviewer_binding'), dict)
                or not isinstance(preserved.get('cumulative_diff'), dict)):
            raise ValueError('successor reconciliation preservation format is invalid')
        if (preserved.get('run_id') != run_id or preserved.get('authorization_digest') != record.digest
                or preserved.get('frozen_head') != head or preserved.get('branch') != old.branch
                or preserved.get('worktree') != old.worktree
                or preserved.get('allowlist') != old.allowlist
                or preserved.get('reviewer_binding', {}).get('task_id') != old.task_id
                or preserved.get('cumulative_diff', {}).get('stdout') != git('diff', '--binary', 'HEAD')):
            raise ValueError('successor reconciliation preserved content changed')
        untracked = git('ls-files', '--others', '--exclude-standard').splitlines()
        hashes = {name: digests.sha256_bytes((root/name).read_bytes()) for name in untracked}
        if hashes != preserved.get('untracked_hashes'):
            raise ValueError('successor reconciliation untracked content changed')
        # Other nodes may not hold the same writer root. The original
        # developer delivery is historical evidence, not another active task.
        if any(other != node_id and (node.get('active_task') or other in snapshot.handles)
               and self._worktree_path(node).resolve() == root.resolve()
               for other, node in snapshot.nodes.items()):
            raise ValueError('successor reconciliation writer root is already active')
        created = store.result(action_id)
        data = created.get('binding') if isinstance(created, dict) else None
        if not isinstance(data, dict):
            raise ValueError('successor reconciliation create result is missing')
        task_id = data.get('threadId') or data.get('task_id')
        host = data.get('hostId') or data.get('host')
        raw = old.to_dict()
        raw.update(task_id=task_id, platform_task_id=task_id, threadId=task_id,
                   host=host, hostId=host,
                   successor_of=old.task_id, status='created', cursor=None,
                   binding_intent=None, binding_observation=None,
                   binding_state='blocked_unknown', business_write_allowed=False,
                   model=profile.model, reasoning=profile.reasoning, route_digest=profile.route_digest)
        if handoff:
            raw.update(provider=runner_provider, handoff_from_provider=old.provider,
                       threadId=task_id if runner_provider == CODEX_PROVIDER else None,
                       hostId=host if runner_provider == CODEX_PROVIDER else None,
                       capability_contract_digest=snapshot.capability_contract_digest or None)
        new = TaskBinding.from_dict(raw)
        reconcile_shadowed_successor(self.paths, old, new, action_id=action_id,
                                    request_digest=request_digest, authorization_digest=record.digest)
        snapshot.tasks[node_id+':reviewer'] = new.to_dict()
        if delivery_recovery is not None:
            current['delivery_evidence'] = dict(delivery)
            current.update({key:delivery[key] for key in ('completion_marker','delivery_path','thread_status')})
        current['reviewer_identity'] = task_id
        current['review_generation'] = old.generation
        current['retryable_action'] = {'role': 'reviewer', 'phase': 'review', 'continuation': True,
            'successor': False, 'same_task': True, 'same_task_required': True,
            'successor_candidate': False, 'reason_class': 'engineering'}
        current['status'] = 'blocked_unknown'
        current['quarantine'] = None
        self._record(snapshot, 'successor_registration_reconciled', {'run_id': run_id,
            'node_id': node_id, 'proof': {'action_id': action_id, 'request_digest': request_digest,
            'predecessor_task_id': old.task_id, 'task_id': task_id, 'head_sha': head,
            'predecessor_archive': predecessor_archive,
            'preservation_path': str(path), 'preservation_digest': preservation_digest,
            **({'delivery_recovery':delivery_recovery} if delivery_recovery is not None else {})}})
        self._record_topology_projection(snapshot, 'monitor.reconcile_successor')
        self._refresh_run_status(snapshot)
        self._save_snapshot(snapshot)
        return snapshot

    def _proven_successor_action(
        self, snapshot, node_id, role, generation, provider, action_id,
        request_digest, predecessor, authorization_digest,
    ) -> bool:
        """Prove replay of one create transaction, never permission for a new writer."""
        store = ProviderActionStore(self.paths)
        try:
            action = store.action(action_id)
            if (action.get("request_digest") != request_digest
                    or action.get("run_id") != snapshot.run_id
                    or action.get("issue_id") != node_id or action.get("role") != role
                    or action.get("generation") != generation
                    or action.get("provider") != provider
                    or action.get("operation") not in {"create", "locate", "visibility"}):
                return False
            created_action = store.related_action(action, "create")
            for candidate in (action, created_action):
                request = candidate["request"]
                if (request.get("authorization_digest") != authorization_digest
                        or request.get("plan_id") != getattr(self.plan, "plan_id", "")
                        or request.get("plan_version") != self.plan.version):
                    return False
            request = created_action["request"]
            if (request.get("successor") is not True
                    or request.get("predecessor_task_id") != predecessor):
                return False
            if action["operation"] == "create":
                return True
            result = store.result(created_action["action_id"])
            binding = result.get("binding") if isinstance(result, dict) else None
            if not isinstance(binding, dict):
                return False
            task_id = binding.get("threadId") or binding.get("task_id")
            host = binding.get("hostId") or binding.get("host")
            if (not isinstance(task_id, str) or not task_id or task_id == predecessor
                    or not isinstance(host, str) or not host):
                return False
            request = action["request"]
            if action["operation"] == "locate":
                return request.get("threadId") == task_id
            return request.get("targets") == [{"threadId": task_id, "hostId": host}]
        except (OSError, ValueError, TypeError, KeyError):
            return False

    def _count_active_sessions(self, snapshot: RunSnapshot) -> int:
        """Worker-session slots in use: the larger of snapshot and task registry."""
        by_snapshot = sum(
            1
            for node_id, current in snapshot.nodes.items()
            if current.get("status") not in {"stopped", "failed"}
            and not current.get("pair_archived")
            and (
                int(current.get("developer_generation", 0)) > 0
                or current.get("retryable_action") is not None
                or isinstance(current.get("active_task"), dict)
                or node_id in snapshot.handles
            )
        )
        by_registry = len([
            n for n in self._occupied_nodes(snapshot)[0]
            if not snapshot.nodes.get(n, {}).get("pair_archived")
            and snapshot.nodes.get(n, {}).get("status") not in {"stopped", "failed"}
        ])
        return max(by_snapshot, by_registry)

    def _schedule_ready(
        self,
        snapshot: RunSnapshot,
        runner: Runner,
        recover_missing_reviewer: bool = False,
    ) -> None:
        record = self._require_snapshot_authorization(snapshot)
        self._validate_execution_topology(snapshot)
        # V4.6: capacity counts active *worker sessions* (one visible-sdd
        # session or one dev/reviewer pair slot per occupied node), not
        # logical pairs.  Unknown states keep occupying their slot: when the
        # evidence is ambiguous the monitor dispatches less, never more.
        session_limit = self._effective_worker_session_limit(record)
        # The V4.6 parallel-group audit (ISSUE-06, wired into
        # ``node_scoped_ready`` in dag.py) refuses nodes whose group
        # membership is conflicting or unverifiable.  Consume the same audit
        # signal on the same readiness projection before any writer lease is
        # acquired; on a dag.py without the gate the audit set is empty and
        # dispatch behavior is unchanged.
        group_blocked = self._parallel_group_blocked(snapshot)
        # V4.7 ISSUE-01: explicit writer ownership is checked over every
        # candidate of this round (same group, cross group or ungrouped)
        # before any writer lease is acquired.  A refused round leaves its
        # candidates blocked_unknown, so the planned branch below never sees
        # them.
        self._refuse_path_ownership_conflicts(snapshot)
        active_sessions = self._count_active_sessions(snapshot)
        # The task registry outranks the snapshot counters; an unreadable or
        # contradictory registry holds new dispatch (blocked_unknown) rather
        # than guessing how many slots are free.
        self._reconcile_capacity(snapshot)
        capacity_held = snapshot.run_id in self.__dict__.get("_capacity_hold", {})
        for node_id, node in self.nodes.items():
            current = snapshot.nodes[node_id]
            was_active_session = not current.get("pair_archived") and (
                int(current.get("developer_generation", 0)) > 0
                or current.get("retryable_action") is not None
            )
            # ISSUE-249 runs before every other recovery gate: the legacy
            # acceptance ordering left blocked_unknown + a written acceptance
            # + an accepted reviewer binding at once, and each legacy gate
            # below is mutually exclusive with that state.
            if self._recover_reverse_blocked_acceptance(snapshot, node_id):
                if was_active_session:
                    active_sessions -= 1
                continue
            if self._recover_quarantined_delivered_developer(snapshot, node_id):
                if was_active_session:
                    active_sessions -= 1
            if recover_missing_reviewer:
                self._recover_missing_reviewer_successor(snapshot, node_id)
            retry = current.get("retryable_action")
            if isinstance(retry, dict) and retry.get("same_task") is True:
                # Unknown side effects are never eligible for successor
                # creation, even if an older persisted marker said so.
                if retry.get("successor") is not False or retry.get("successor_candidate") is True:
                    retry = dict(retry)
                    retry["successor"] = False
                    retry["successor_candidate"] = False
                    current["retryable_action"] = retry
            if (
                current.get("status") in {"blocked_unknown", "retry_pending", "running", "rework"}
                and isinstance(retry, dict)
                and node_id not in snapshot.handles
            ):
                # A delivered developer handoff is a verified continuation
                # even when an interrupted reauthorization left a stale
                # quarantine marker behind.  Normalize the legacy marker
                # before applying the fail-closed quarantine branch so the
                # original visible task can be resumed in place.
                resumable_developer = (
                    retry.get("continuation")
                    and retry.get("role") == "developer"
                    and not self._continuation_requires_successor(
                        snapshot, node_id, "developer"
                    )
                )
                if resumable_developer:
                    retry = dict(retry)
                    retry["successor_candidate"] = False
                    current["retryable_action"] = retry
                    if (
                        isinstance(current.get("quarantine"), dict)
                        and current.get("active_task") is None
                    ):
                        current["quarantine"] = None
                pending_successor = (
                    retry.get("successor") is True
                    and self._proven_successor_action(
                        snapshot, node_id, str(retry.get("role")),
                        int(current.get("review_generation" if retry.get("role") == "reviewer" else "developer_generation", 0)) + 1,
                        getattr(runner, "provider", None), retry.get("pending_provider_action"),
                        retry.get("request_digest"), retry.get("predecessor_task_id"),
                        getattr(record, "digest", snapshot.authorization_digest),
                    )
                )
                if retry.get("pending_provider_action") is not None and not pending_successor:
                    current["reason"] = "pending successor transaction proof is invalid"
                    current["status"] = "blocked_unknown"
                    continue
                if (
                    current.get("status") == "blocked_unknown"
                    and isinstance(current.get("quarantine"), dict)
                    and retry.get("successor_candidate") is not True
                    and retry.get("same_task") is not True
                    and current.get("active_task") is None
                    and not resumable_developer
                    and not pending_successor
                ):
                    current["retryable_action"] = {
                        "action": "reconcile_binding",
                        "manual_repair_required": True,
                        "same_task": False,
                        "same_task_required": True,
                        "successor": False,
                        "role": retry.get("role"),
                        "phase": retry.get("phase"),
                        "reason": "quarantined task has no proven active binding to resume",
                    }
                    current["recovery_action"] = "reconcile_binding"
                    current["reason"] = (
                        "quarantined task has no proven active binding to resume"
                    )
                    continue
                if retry.get("successor_candidate") is True and retry.get(
                    "continuation"
                ):
                    predecessor = self._prove_old_task_stopped_or_absent(
                        snapshot, node_id, retry
                    )
                    if predecessor is None:
                        current["status"] = "blocked_unknown"
                        current["reason"] = (
                            "old task stop or absence cannot be proven for successor"
                        )
                        continue
                    retry = dict(retry)
                    retry["continuation"] = False
                    retry["successor"] = True
                    retry["predecessor_task_id"] = predecessor or None
                    retry["old_task_reconciled"] = True
                    current["retryable_action"] = retry
                    current["old_task_reconciled"] = True
                    self._record(
                        snapshot,
                        "old_task_reconciled",
                        {
                            "run_id": snapshot.run_id,
                            "node_id": node_id,
                            "role": retry["role"],
                            "predecessor_task_id": predecessor or None,
                            "proof": "absent" if not predecessor else "stopped",
                        },
                    )
                if retry.get("manual_repair_required") is True:
                    continue
                pending_schedule = retry.get("pending_schedule") is True
                reviewer_recovery = retry.get("missing_binding_recovery") is True
                # Respect the durable bounded-backoff deadline for V4.2
                # engineering retries.  The marker is consumed by the
                # scheduler; polling/starting again before this point would
                # turn an intended retry budget into a hot loop.
                next_retry_at = retry.get("next_retry_at")
                if (
                    self._v42_retry_status() == "retry_pending"
                    and retry.get("same_task") is True
                    and isinstance(next_retry_at, (int, float))
                    and time.time() < float(next_retry_at)
                ):
                    continue
                # Legacy V3.x replay defers one scheduling pass after an
                # interrupted timeout. The next heartbeat can then resume
                # the same handle/task; this preserves historical recovery
                # ordering while V4.2 uses the explicit retry_pending phase.
                if (
                    self._v42_retry_status() == "blocked_unknown"
                    and retry.get("same_task") is True
                    and retry.get("defer_once") is True
                    and not pending_schedule
                    and isinstance(current.get("quarantine"), dict)
                ):
                    retry = dict(retry)
                    retry["defer_once"] = False
                    current["retryable_action"] = retry
                    continue
                if pending_schedule and capacity_held:
                    # Unknown registry: no new session, not even a reviewer
                    # replacement.
                    continue
                if pending_schedule and (
                    active_sessions >= session_limit
                    or not all(
                        snapshot.nodes[dependency].get("status") == "accepted"
                        for dependency in node.depends_on
                    )
                ) and not reviewer_recovery:
                    continue
                retry_role = str(retry["role"])
                retry_generation = int(current.get("review_generation" if retry_role == "reviewer" else "developer_generation", 0)) + 1
                retry_digest = str((current.get("start_intent") or {}).get("intent_digest") or self._prospective_intent_digest(snapshot, node_id, retry_role, retry_generation, current))
                if not acquire_writer_lease(self.paths, node_id, str(current["worktree"]), snapshot.run_id, retry_digest, retry_role, retry_generation) and not self._adopt_unserved_handoff_lease(snapshot, node_id, retry_role, retry_generation, retry_digest, runner):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "writer lease is already owned by another run",
                    )
                    continue
                if pending_schedule:
                    current["pair_archived"] = False
                    active_sessions += 1
                if self._start_task(
                    snapshot,
                    node_id,
                    str(retry["role"]),
                    str(retry["phase"]),
                    runner,
                    bool(retry.get("continuation")),
                    bool(retry.get("successor") or reviewer_recovery),
                ):
                    if current.get("status") not in {"blocked_unknown", "retry_pending"}:
                        current["retryable_action"] = None
                    if (
                        str(retry.get("role")) == "reviewer"
                        and current.get("reviewer_replacement_required")
                        and current.get("status") == "review"
                    ):
                        # A scheduled successor review is now live: close the
                        # replacement gate exactly like the delivery-time
                        # successor dispatch does above.  Leaving it open
                        # would force yet another fresh identity on the next
                        # review round instead of continuing this reviewer.
                        current["reviewer_replacement_required"] = False
                continue
            if current.get("status") == "delivered" and not current.get("reviewer_started"):
                if self._node_dispatch_topology(node) == TOPOLOGY_VISIBLE_SDD:
                    # visible-sdd review is in-session evidence consumed at
                    # delivery time; no reviewer task is ever dispatched.  A
                    # delivered state reaching the scheduler means the
                    # evidence was lost (interrupted recovery), never that it
                    # was accepted: fail closed instead of stranding the node
                    # with its session slot held forever.
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "visible-sdd delivery has no consumable in-session review evidence after recovery",
                        quarantine_lease=False,
                    )
                    continue
                retry_role = "reviewer"
                retry_generation = int(current.get("review_generation", 0)) + 1
                retry_digest = str((current.get("start_intent") or {}).get("intent_digest") or self._prospective_intent_digest(snapshot, node_id, retry_role, retry_generation, current))
                if not acquire_writer_lease(self.paths, node_id, str(current["worktree"]), snapshot.run_id, retry_digest, retry_role, retry_generation) and not self._adopt_unserved_handoff_lease(snapshot, node_id, retry_role, retry_generation, retry_digest, runner):
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "writer lease is already owned by another run",
                    )
                    continue
                self._start_task(
                    snapshot,
                    node_id,
                    "reviewer",
                    "review",
                    runner,
                    False,
                    bool(current.get("reviewer_replacement_required")),
                )
                continue
            if current.get("status") != "planned":
                continue
            if node_id in group_blocked:
                # Refused by the parallel-group audit: never acquire a
                # writer lease for a conflicting or unverifiable group
                # member.
                continue
            supervisor_rejection = self._supervisor_writer_rejection(node, current)
            if supervisor_rejection is not None:
                self._mark_blocked_unknown(snapshot, node_id, supervisor_rejection)
                self._record(
                    snapshot,
                    "supervisor_writer_rejected",
                    {
                        "run_id": snapshot.run_id,
                        "node_id": node_id,
                        "reason": supervisor_rejection,
                    },
                )
                continue
            if not self._brief_allows_first_write(snapshot, node_id, node):
                continue
            if capacity_held or active_sessions >= session_limit:
                continue
            if not all(
                snapshot.nodes[dependency].get("status") == "accepted"
                for dependency in node.depends_on
            ):
                continue
            # Review-only closeout waits for the same dependency/capacity gates,
            # but never creates a developer merely to unlock a reviewer.
            first_role = "reviewer" if is_integration_review_node(node) else "developer"
            generation_key = "review_generation" if first_role == "reviewer" else "developer_generation"
            first_generation = int(current.get(generation_key, 0)) + 1
            first_digest = str((current.get("start_intent") or {}).get("intent_digest") or self._prospective_intent_digest(snapshot, node_id, first_role, first_generation, current))
            if not acquire_writer_lease(self.paths, node_id, str(current["worktree"]), snapshot.run_id, first_digest, first_role, first_generation) and not self._adopt_unserved_handoff_lease(snapshot, node_id, first_role, first_generation, first_digest, runner):
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "writer lease is already owned by another run",
                )
                continue
            self._start_task(
                snapshot, node_id, first_role,
                "review" if first_role == "reviewer" else "develop", runner, False,
            )
            # A create left pending (retry marker, no handle yet) still holds
            # its slot, so recount instead of trusting the start result.
            active_sessions = self._count_active_sessions(snapshot)

    def _handoff_replaced_authorization(self, snapshot: RunSnapshot) -> Optional[str]:
        """The authorization digest the latest reauthorization replaced, when
        that reauthorization was a platform_handoff into the current one.

        Only the most recent reauthorization counts: a later ordinary
        reauthorization moves the digest again, and then nothing recorded
        proves which requests predate the handoff, so this returns None.
        """
        latest = None
        for event in load_events(self.paths, snapshot.run_id)[: snapshot.event_sequence]:
            if event.get("event") == "authorization_reauthorized":
                latest = event.get("data", {})
        if (
            not isinstance(latest, dict)
            or latest.get("change_reason") != "platform_handoff"
            or latest.get("authorization_digest") != snapshot.authorization_digest
        ):
            return None
        previous = latest.get("previous_authorization_digest")
        if not isinstance(previous, str) or len(previous) != 64:
            return None
        return previous

    def _node_never_claimed(
        self, snapshot: RunSnapshot, node_id: str, store: ProviderActionStore
    ) -> bool:
        """Whether nothing durable shows anyone started this node: no task
        identity, active task, handle, binding, ``start_confirmed`` or mailbox
        claim mark.  Anything unprovable is False."""
        current = snapshot.nodes[node_id]
        if (
            current.get("developer_identity") or current.get("reviewer_identity")
            or current.get("active_task") or node_id in snapshot.handles
        ):
            return False
        for other in ("developer", "reviewer"):
            try:
                self._load_task_binding(snapshot, node_id, other)
            except FileNotFoundError:
                continue
            return False
        if any(
            event.get("event") == "start_confirmed"
            and event.get("data", {}).get("node_id") == node_id
            for event in load_events(self.paths, snapshot.run_id)[: snapshot.event_sequence]
        ):
            return False
        return store.claim_evidence(snapshot.run_id, node_id) is None

    def _handoff_replaced_authorizations(self, snapshot: RunSnapshot) -> List[str]:
        """Every authorization digest an unbroken chain of platform_handoff
        reauthorizations ending at the current one replaced, newest first.

        Re-running a finished handoff reauthorizes again, so the latest event
        alone names the digest the *new* platform's create carries; the old
        platform's create sits one link further back.  The walk stops at the
        first event that is not a handoff or does not link to the digest
        after it.
        """
        expected = snapshot.authorization_digest
        chain: List[str] = []
        events = [
            event for event in load_events(self.paths, snapshot.run_id)[: snapshot.event_sequence]
            if event.get("event") == "authorization_reauthorized"
        ]
        for event in reversed(events):
            data = event.get("data", {})
            previous = data.get("previous_authorization_digest")
            if (
                data.get("change_reason") != "platform_handoff"
                or data.get("authorization_digest") != expected
                or not isinstance(previous, str) or len(previous) != 64
            ):
                break
            chain.append(previous)
            expected = previous
        return chain

    def _hide_stale_handoff_creates(self, snapshot: RunSnapshot, runner: Runner) -> None:
        return monitor_handoff._hide_stale_handoff_creates(self, snapshot, runner)

    def _adopt_unserved_handoff_lease(
        self, snapshot: RunSnapshot, node_id: str, role: str, generation: int, digest: str,
        runner: Optional[Runner] = None,
    ) -> bool:
        """Re-key this run's own unclaimed lease after a platform_handoff.

        The intent digest binds the authorization, so the handoff's new
        authorization makes the lease registered before it look foreign.  The
        lease may move to the new digest only when the run's latest
        reauthorization is a platform_handoff and nothing ever claimed the
        node: no binding, no task identity, no handle, no ``start_confirmed``,
        no mailbox claim mark (``ProviderActionStore.claim_evidence``).
        Anything unprovable returns False and the caller keeps blocked_unknown.

        Order matters for crash recovery: hide the old create first (an
        idempotent step), then move the lease, then record the event.  A crash
        before the move leaves the old lease, so the next attempt re-enters
        here and converges; a crash after it leaves nothing to clean up.
        """
        current = snapshot.nodes[node_id]
        try:
            replaced = self._handoff_replaced_authorization(snapshot)
            if replaced is None:
                return False
            store = ProviderActionStore(self.paths)
            if not self._node_never_claimed(snapshot, node_id, store):
                return False
            # A rerun of the handoff re-keys again, and then the digest the
            # latest handoff replaced is the one the new platform's own create
            # carries: never hide a request of the provider now dispatching
            # (a runner without a provider keeps the digest-only rule).
            provider = getattr(runner, "provider", None)
            superseded = store.supersede_unclaimed_creates(
                snapshot.run_id, node_id, role, generation, replaced,
                provider if isinstance(provider, str) and provider else None,
            )
            if not rebind_writer_lease(
                self.paths, node_id, str(current["worktree"]), snapshot.run_id,
                role, generation, None, digest, previous_status="active",
            ):
                return False
        except (OSError, TypeError, ValueError):
            return False
        self._record(
            snapshot,
            "handoff_lease_rebound",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": role,
                "generation": generation,
                "intent_digest": digest,
                "superseded_creates": superseded,
            },
        )
        return True

    def _brief_allows_first_write(
        self, snapshot: RunSnapshot, node_id: str, node: DAGNode
    ) -> bool:
        """Require a valid V3.8 implementation brief before a first write.

        The gate is deliberately checked before acquiring a writer lease or
        invoking the provider.  Nodes without the opt-in contract retain the
        legacy scheduling path.
        """
        if node.contract.get("brief_required") is not True:
            return True
        current = snapshot.nodes[node_id]
        raw = node.contract.get("implementation_brief")
        try:
            brief = ImplementationBrief.from_mapping(raw)
            validation = validate_implementation_brief(
                brief, RunManifest.from_mapping({
                    "plan_id": snapshot.plan_id,
                    "plan_revision": snapshot.plan_version,
                    "run_id": snapshot.run_id,
                    # A missing contract base is itself a mismatch.  Use a
                    # non-zero sentinel only to let the validator report the
                    # concrete ``base_sha`` difference without accepting it.
                    "base_sha": node.contract.get("base_sha", "f" * 40),
                    "target_branch": str(current.get("branch", "")),
                    "execution_epoch": int(node.contract.get("execution_epoch", 0)),
                    "authorization_digest": snapshot.authorization_digest,
                    "evidence_ref": "brief-gate",
                }),
                node,
                project_root=self.paths.root,
            )
        except (TypeError, ValueError, OSError) as error:
            validation = None
            missing = ["schema:" + type(error).__name__]
            evidence = {"status": "brief_pending", "checks": {}}
        else:
            missing = validation.missing
            evidence = validation.evidence
        if missing:
            current["status"] = "brief_pending"
            current["brief_evidence"] = {
                "missing": sorted(set(missing)),
                "checks": evidence.get("checks", {}),
            }
            current["reason"] = "implementation brief is not valid"
            return False
        return True

    def _recover_missing_reviewer_successor(
        self, snapshot: RunSnapshot, node_id: str
    ) -> None:
        """Queue a reviewer successor only from a fully proven recovery state.

        A historical ``reviewer_started`` flag is not proof that this run has
        a reviewer task to continue. Recovery may create a successor only when
        the delivered developer binding is current, the reviewer binding is
        explicitly absent, and neither a provider request nor an active
        task/handle/start intent could represent an unresolved side effect.
        """
        current = snapshot.nodes[node_id]
        if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
            # visible-sdd binds a single worker session; there is never a
            # reviewer binding to recover.
            return
        if current.get("status") != "blocked_unknown":
            return
        if current.get("reviewer_started") is not True:
            return
        if current.get("retryable_action") is not None:
            return
        if (
            current.get("active_task") is not None
            or current.get("active_role") is not None
            or current.get("start_intent") is not None
            or node_id in snapshot.handles
        ):
            return

        try:
            developer = self._load_task_binding(snapshot, node_id, "developer")
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return

        try:
            self._load_task_binding(snapshot, node_id, "reviewer")
        except FileNotFoundError:
            pass
        except (OSError, TypeError, ValueError):
            return
        else:
            return

        try:
            requested = ProviderActionStore(self.paths).has_request(
                snapshot.run_id, node_id, "reviewer"
            )
        except (OSError, TypeError, ValueError):
            return
        if requested:
            return

        try:
            developer_generation = int(current.get("developer_generation", 0))
        except (TypeError, ValueError):
            return
        if (
            developer.status != "delivered"
            or developer.task_id != current.get("developer_identity")
            or developer.generation != developer_generation
        ):
            return

        predecessor = current.get("reviewer_identity")
        if predecessor is not None and (
            not isinstance(predecessor, str) or not predecessor
        ):
            return
        current["retryable_action"] = {
            "role": "reviewer",
            "phase": "review",
            "continuation": False,
            "successor": True,
            "successor_candidate": True,
            "pending_schedule": True,
            "missing_binding_recovery": True,
            "predecessor_task_id": predecessor,
        }
        current["reason"] = "reviewer successor queued after missing binding recovery"

    def _recover_reverse_blocked_acceptance(
        self, snapshot: RunSnapshot, node_id: str
    ) -> bool:
        """Complete an acceptance the legacy ordering reverse-blocked (#249).

        The pre-5.1.1 acceptance handler recorded the ``accepted`` event and
        flipped the reviewer binding *before* checking the payload's
        top-level P0-P2 clearance evidence, so an evidence-less acceptance
        left the node simultaneously accepted (reviewer binding + written
        ``acceptance`` fields) and ``blocked_unknown`` (snapshot status).
        Every legacy recovery gate is mutually exclusive with that state:
        the successor recoverer demands a *missing* reviewer binding,
        ``repair`` only re-stamps ``binding_unknown``, and ``reconcile``
        demands a package covering every unaccepted node.  This pass
        recognizes the exact durable signature and completes the acceptance
        the event log already asserts, mirroring the live path's
        zero-clearance closeout for non-integration nodes.

        The signature is structural, not textual: node reasons are persisted
        redacted, so the reverse-block text cannot be matched on disk.  A
        written acceptance with the current contract digest and
        authorization epoch can only have been produced by the accepted
        path itself, and the accepted reviewer binding of the recorded
        generation proves the same reviewer round the acceptance belongs to.

        Every fact the pass decides on is captured into ``RecoveryFacts`` /
        ``BindingFacts`` *before* the first mutation (#256 ①), and a signature
        fact that reads back as a redaction placeholder aborts the pass
        instead of being compared (#256 ②).  The defect-adjacent markers are
        judged by ``defect_adjacent_marker_allows_rescue`` (#256 ③).

        Fail-closed: anything short of the full signature -- a different
        block cause, no written acceptance, epoch or digest drift, a live
        task/handle/intent/retry, a recovery action other than the
        reverse-block's own ``reconcile_binding`` stamp, a quarantine still
        bound to a live handle, an unreadable or stale reviewer binding, or
        an unreadable live node contract -- leaves the node blocked for the
        human.  The closeout itself orders durable state before audit: a
        failure anywhere before the pair is fully archived restores every
        inspected field from the captured facts (bindings rolled back
        best-effort) for a clean retry; a failure of the final audit append
        alone keeps the durable acceptance, because re-blocking an archived
        pair is the very mutually-exclusive deadlock this pass exists to
        clear.
        Integration-review is excluded: its closeout needs the derived
        run-level package, which an evidence-less record cannot rebuild.
        """
        current = snapshot.nodes[node_id]
        if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
            # A visible-sdd node binds a single worker session; its
            # acceptance never passed through the reviewer binding path.
            return False
        if node_id == "integration-review":
            return False
        # #256 ①: every decision below reads facts captured *now*.  The node
        # mapping and the shared binding objects are mutated in place further
        # down, so re-reading them (especially from the rollback) can only see
        # post-mutation values -- the #249 no-op rollback.
        facts = RecoveryFacts.capture(
            node_id, current, handle_registered=node_id in snapshot.handles
        )
        if facts.unreadable:
            # A signature fact persisted as a redaction placeholder cannot be
            # compared; fail closed instead of silently missing (#256 ②).
            return False
        if facts.status != "blocked_unknown":
            return False
        if not isinstance(facts.acceptance, dict):
            return False
        contract_digest = facts.acceptance_contract_digest
        if (
            not isinstance(contract_digest, str)
            or len(contract_digest) != 64
            or any(character not in "0123456789abcdef" for character in contract_digest)
            or contract_digest != facts.contract_digest
            or facts.authorization_epoch != snapshot.authorization_digest
        ):
            return False
        if facts.review_clearance:
            # The closeout already ran; nothing left to rescue.
            return False
        if (
            facts.active_task is not None
            or facts.active_role is not None
            or facts.start_intent is not None
            or facts.retryable_action is not None
            or facts.handle_registered
        ):
            return False
        # The reverse-block residue itself (#256 ③): ``_mark_blocked_unknown``
        # stamps ``reconcile_binding`` and a quarantine whose handle was
        # already retired (handle_id null) -- that is the bug's own
        # fingerprint, not live work.  Any other recovery action, or a
        # quarantine still bound to a live handle, vetoes the rescue
        # fail-closed.
        if not defect_adjacent_marker_allows_rescue(
            facts.recovery_action, facts.quarantine
        ):
            return False
        try:
            registered = BindingFacts.capture(
                "reviewer", self._load_task_binding(snapshot, node_id, "reviewer")
            )
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return False
        if (
            registered.status != "accepted"
            or registered.task_id != facts.reviewer_identity
            or registered.generation != facts.review_generation
        ):
            return False
        try:
            developer_binding = BindingFacts.capture(
                "developer", self._load_task_binding(snapshot, node_id, "developer")
            )
        except (FileNotFoundError, OSError, TypeError, ValueError):
            # ``_archive_pair`` archives both bindings; without the
            # developer binding the closeout cannot be written durably.
            return False
        try:
            live_digest_matches = self._live_node_contract_digest_matches(
                node_id, facts.contract_digest
            )
        except ValueError:
            return False
        if not live_digest_matches:
            return False
        # Release the lease *before* any lifecycle mutation: the unlink is
        # the one step that routinely raises OSError, and an exception after
        # the bindings were archived could never re-flip them to accepted,
        # which would mint a fresh mutually-exclusive deadlock.  Ordering it
        # first keeps every failure below a pure no-op.
        #
        # The caught tuple is deliberately narrow and is a plain tuple of
        # exception *classes* (a set literal would raise TypeError at runtime,
        # and a frozenset constant could not be used in an except clause at
        # all): OSError is the unlink failure this ordering exists for,
        # TypeError/ValueError are what the registry validators raise.  Every
        # other exception -- including RuntimeError and BaseException
        # subclasses such as KeyboardInterrupt -- must propagate so the run
        # fails loudly instead of parking the node; widening this tuple would
        # turn a programming error into a silent "rescue declined".
        try:
            self._release_node_lease(snapshot, node_id)
        except (OSError, TypeError, ValueError):
            return False
        audit_payload = {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "proof": {
                "reverse_block_reason": REVERSE_BLOCK_REASON,
                "reviewer_task_id": registered.task_id,
                "reviewer_generation": registered.generation,
                "contract_digest": contract_digest,
            },
        }
        archived = False
        try:
            current["status"] = "accepted"
            current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
            current["reason"] = None
            current["recovery_action"] = None
            current["quarantine"] = None
            # Durable closeout first, audit trail second: once the pair is
            # archived the acceptance is a fact on disk, and a failing audit
            # append must never re-block a node whose lifecycle already
            # closed.  ``archived`` therefore only flips after _archive_pair
            # -- including its own event -- fully succeeded.
            self._archive_pair(snapshot, node_id)
            archived = True
            self._record(snapshot, "acceptance_recovered", audit_payload)
        except (OSError, TypeError, ValueError):
            if archived:
                # Only the audit append can fail here.  Retry it once
                # best-effort and keep the durable closeout: resurrecting
                # blocked_unknown over an archived pair is exactly the
                # mutually-exclusive deadlock this pass exists to clear.
                try:
                    self._record(snapshot, "acceptance_recovered", audit_payload)
                except (OSError, TypeError, ValueError):
                    pass
                return True
            # Fail closed with zero half-done residue: restore every field
            # the signature inspected -- status, clearance, reason,
            # recovery_action, quarantine and pair_archived -- from the
            # captured ``facts`` copy, and roll both bindings back to the
            # statuses captured in their ``BindingFacts`` copies
            # best-effort (``_archive_pair`` may have archived one or both
            # before failing).  Reading either back from the live objects
            # here would only ever see the post-mutation value and turn the
            # rollback into a no-op (#249 ``019a619``), leaving an archived
            # binding that never matches the signature again.  The next
            # scheduling pass then retries the rescue from a state no other
            # gate can ever see as half-done.
            current["status"] = facts.status
            current["review_clearance"] = facts.review_clearance
            current["reason"] = facts.reason
            current["recovery_action"] = facts.recovery_action
            current["quarantine"] = facts.quarantine
            current["pair_archived"] = facts.pair_archived
            for binding in (developer_binding, registered):
                try:
                    self._set_binding_status(
                        snapshot, node_id, binding.role, binding.status
                    )
                except (FileNotFoundError, OSError, TypeError, ValueError):
                    pass
            return False
        return True

    def _effective_model_tier(self, node: DAGNode, current: Dict[str, Any], role: str) -> str:
        """Derived tier plus the run's recorded ±1 adjustments (never below ``light``)."""
        tier = model_catalog.node_tier(node, role=role)
        try:
            delta = int(current.get("model_tier_adjustment", 0))
        except (TypeError, ValueError):
            delta = 0
        index = max(0, min(len(model_catalog.TIERS) - 1, model_catalog.TIERS.index(tier) + delta))
        tier = model_catalog.TIERS[index]
        if role == "reviewer":
            tier = model_catalog.max_tier(tier, "standard")
        return tier

    #: Rounds a ``model_unavailable`` node may re-walk its chain before the
    #: supervisor must act (move the tier or fix the configured map). Bounds
    #: the retry: every model still gets exactly one attempt per round.
    _MODEL_UNAVAILABLE_MAX_ROUNDS = 2

    #: Node-state keys written by the model fallback machinery.  Every
    #: recovery entry point (repair / requeue / tier move) clears them so the
    #: next dispatch re-resolves the model from scratch.
    _MODEL_FALLBACK_BOOKKEEPING_KEYS = (
        "model_dispatch_attempts",
        "model_dispatch_chain",
        "model_dispatch_chain_profile",
        "model_unavailable_rounds",
        "model_unavailable",
    )

    def _clear_model_fallback_state(self, current: Dict[str, Any]) -> bool:
        """Drop monitor-authored model fallback state so recovery re-resolves the model.

        Also removes the switched ``contract_overrides.worker_profile``: the
        stale override would otherwise shadow :meth:`_apply_model_tier`
        re-resolution (a fixed ``model_map`` or a tier move must take effect
        on the next dispatch) and, for routed nodes, would keep replaying the
        last chain model instead of the router-selected primary.  Returns
        ``True`` when fallback state existed.
        """
        had_state = any(
            key in current for key in self._MODEL_FALLBACK_BOOKKEEPING_KEYS
        )
        for key in self._MODEL_FALLBACK_BOOKKEEPING_KEYS:
            current.pop(key, None)
        overrides = current.get("contract_overrides")
        if isinstance(overrides, dict) and "worker_profile" in overrides:
            overrides.pop("worker_profile", None)
            had_state = True
        return had_state

    def _profile_model_chain(self, contract: Dict[str, Any], current: Dict[str, Any]) -> List[str]:
        """The dispatch model plus its configured fallbacks, in dispatch order."""
        recorded = current.get("model_dispatch_chain")
        if (
            isinstance(recorded, list)
            and recorded
            and all(isinstance(name, str) and name.strip() for name in recorded)
        ):
            return list(recorded)
        raw = contract.get("worker_profile")
        if not isinstance(raw, dict):
            overrides = current.get("contract_overrides")
            raw = overrides.get("worker_profile") if isinstance(overrides, dict) else None
        if not isinstance(raw, dict):
            return []
        names: List[str] = []
        primary = raw.get("model")
        if isinstance(primary, str) and primary.strip():
            names.append(primary)
        for item in raw.get("fallbacks") or []:
            name = item.get("model") if isinstance(item, dict) else item
            if isinstance(name, str) and name.strip() and name not in names:
                names.append(name)
        return names

    def _plan_model_fallback(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        contract: Dict[str, Any],
        current: Dict[str, Any],
        role: str,
        phase: str,
        continuation: bool,
        error_text: Any,
        reason_class: Optional[str] = None,
    ) -> str:
        """Advance the dispatch model along the profile chain after a model-class failure.

        Returns ``"switch"`` when the scheduled retry must replay on the next
        chain model (the ``model_fallback_used`` event is recorded and the
        node stays retryable, never blocked), ``"exhausted"`` when every chain
        model burned its single attempt (the caller then fails closed as a
        retryable ``model_unavailable``), and ``"none"`` when the failure is
        not a model-chain condition at all.

        ``reason_class`` may carry the classification computed from the raw
        provider error: durable ``reason`` text is redacted before this point,
        which would erase the markers.
        """
        if reason_class is None:
            reason_class = model_failure_reason_class(error_text)
        if reason_class is None:
            return "none"
        chain = self._profile_model_chain(contract, current)
        if not chain:
            return "none"
        if len(chain) < 2:
            # No alternate configured: the model failure keeps the legacy
            # retryable behavior (omit_thinking recovery, per-tick requeue).
            # The exhausted/park path is only for chains with real alternates.
            return "none"
        raw_profile = contract.get("worker_profile")
        if not isinstance(raw_profile, dict):
            return "none"
        failed_model = raw_profile.get("model")
        if not isinstance(failed_model, str) or not failed_model.strip():
            return "none"
        attempts = current.get("model_dispatch_attempts")
        if not isinstance(attempts, list):
            attempts = []
            current["model_dispatch_attempts"] = attempts
        if not current.get("model_dispatch_chain_profile"):
            # Keep the original profile so an exhausted round can rewind the
            # chain to its head instead of retrying only the last model.
            current["model_dispatch_chain_profile"] = dict(raw_profile)
        if any(isinstance(item, dict) and item.get("model") == failed_model for item in attempts):
            # Each model gets exactly one attempt per round.
            return "none"
        attempts.append({
            "model": failed_model,
            "reason_class": reason_class,
            "reason": redact_provider_text(str(error_text))[:400],
        })
        current["model_dispatch_chain"] = chain
        tried = {item.get("model") for item in attempts if isinstance(item, dict)}
        for name in chain:
            if name not in tried:
                self._switch_model_fallback(
                    snapshot, node_id, contract, current, role,
                    failed_model, name, reason_class,
                )
                return "switch"
        self._mark_model_unavailable(snapshot, node_id, current, role, phase, continuation)
        return "exhausted"

    def _switch_model_fallback(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        contract: Dict[str, Any],
        current: Dict[str, Any],
        role: str,
        from_model: str,
        to_model: str,
        reason_class: str,
    ) -> None:
        """Point the dispatch copy (and its durable override) at the next chain model."""
        raw_profile = contract.get("worker_profile")
        profile = dict(raw_profile) if isinstance(raw_profile, dict) else {}
        remaining = []
        for item in profile.get("fallbacks") or []:
            name = item.get("model") if isinstance(item, dict) else item
            if name != to_model:
                remaining.append(item)
        profile["model"] = to_model
        profile["fallbacks"] = remaining
        contract["worker_profile"] = profile
        current.setdefault("contract_overrides", {})["worker_profile"] = dict(profile)
        self._record(
            snapshot,
            "model_fallback_used",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": role,
                "proof": {
                    "from_model": from_model,
                    "to_model": to_model,
                    "reason_class": reason_class,
                },
            },
        )

    def _mark_model_unavailable(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        current: Dict[str, Any],
        role: str,
        phase: str,
        continuation: bool,
    ) -> None:
        """Every chain model failed: fail closed, retryable, never success/blocked_unknown.

        The node lands on the existing retryable state machine
        (``_v42_retry_status`` -- ``retry_pending`` on V4.2) with an explicit
        ``model_unavailable`` marker carrying each model's own failure, and a
        ``retryable_action`` whose ``reason_class`` is ``model_unavailable``.
        A bounded number of full-chain re-walks stays schedulable so a
        transient platform outage can recover on its own; past that ceiling
        the retry marker is dropped and the node parks until the supervisor
        acts -- ``repair_node``, ``schedule_retry`` and ``adjust_model_tier``
        each clear the fallback state (and the switched override profile, so
        a fixed ``model_map`` or a tier move takes effect) and requeue the
        node.  The durable status vocabulary is unchanged: ``state.py``
        knows no new node status.
        """
        attempts = [
            item for item in (current.get("model_dispatch_attempts") or [])
            if isinstance(item, dict)
        ]
        detail = "; ".join(
            "%s: %s (%s)" % (item.get("model"), item.get("reason_class"), item.get("reason"))
            for item in attempts
        )
        rounds = 0
        try:
            rounds = int(current.get("model_unavailable_rounds", 0) or 0)
        except (TypeError, ValueError):
            rounds = 0
        rounds += 1
        current["model_unavailable_rounds"] = rounds
        current["model_unavailable"] = {"rounds": rounds, "attempts": list(attempts)}
        current["status"] = self._v42_retry_status()
        current["reason"] = "model fallback chain exhausted (%d models): %s" % (
            len(attempts), detail or "no attempt recorded"
        )
        current["active_task"] = None
        current["active_role"] = None
        current["start_intent"] = None
        if rounds >= self._MODEL_UNAVAILABLE_MAX_ROUNDS:
            current["retryable_action"] = None
            self._record(
                snapshot,
                "model_unavailable_parked",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": role,
                    "proof": {
                        "rounds": rounds,
                        "attempts": list(attempts),
                    },
                },
            )
            return
        saved = current.get("model_dispatch_chain_profile")
        if isinstance(saved, dict):
            # Rewind to the chain head so the retry re-walks every model.
            current.setdefault("contract_overrides", {})["worker_profile"] = dict(saved)
        current["model_dispatch_attempts"] = []
        current["retryable_action"] = {
            "role": role,
            "phase": phase,
            "continuation": continuation,
            "successor": False,
            "same_task": True,
            "same_task_required": True,
            "attempt": rounds,
            "reason_class": "model_unavailable",
            "next_retry_at": time.time(),
            "binding_digest": current.get("contract_digest", ""),
            "last_observation_ref": "model-chain-exhausted",
        }

    def _apply_model_tier(
        self, snapshot: RunSnapshot, node: DAGNode, contract: Dict[str, Any],
        current: Dict[str, Any], role: str,
    ) -> None:
        """Turn the node's tier into this platform's model on the dispatch copy.

        Only a profile that still carries the planner's placeholder model is
        touched: an explicitly routed node keeps the route it was given.  The
        concrete model lives on the dispatch contract only, never in the signed
        node contract, so it cannot affect the authorization digest.
        """
        raw = contract.get("worker_profile")
        if not isinstance(raw, dict) and is_integration_review_node(node):
            # The integration reviewer may reach dispatch with no profile dict;
            # the v2 fallback would then pin the "default" model and skip the
            # tier entirely.  Seed the same placeholder so it resolves as deep.
            worker = str(contract.get("worker") or node.contract.get("worker") or "worker")
            raw = {
                "worker": worker, "model": "default", "reasoning": "normal",
                "fallbacks": [],
                "selection_basis": {
                    "issue_complexity_ref": node.id, "complexity_band": "standard",
                    "risk_tags": ["integration", "read-only"],
                    "availability_evidence": "runtime",
                },
                "writer": worker,
                "worktree": str(contract.get("worktree") or current.get("worktree") or "."),
                "branch": str(contract.get("branch") or current.get("branch") or "branch-" + node.id),
                "allowlist": list(contract.get("files") or [node.id + ".py"]),
            }
        if not isinstance(raw, dict) or raw.get("model") not in (None, "", "default"):
            return
        if contract.get("routing_required") is True:
            return
        try:
            tier = self._effective_model_tier(node, current, role)
            choice = model_catalog.resolve(
                str(contract.get("adapter_id") or node.contract.get("adapter_id") or ""),
                tier,
                vibe_home=model_catalog.vibe_home_path(),
            )
        except (model_catalog.TierError, ValueError, TypeError):
            return
        if choice.status != "ok":
            # Unknown probe: keep the platform default, but say so once.
            if not current.get("model_tier_unknown_recorded"):
                current["model_tier_unknown_recorded"] = True
                self._record(
                    snapshot,
                    "model_tier_unresolved",
                    {
                        "run_id": snapshot.run_id,
                        "node_id": node.id,
                        "role": role,
                        "reason_code": "model_probe_unknown",
                        "proof": {"tier": tier, "platform": choice.platform, "note": choice.note},
                    },
                )
            return
        basis = dict(raw.get("selection_basis") or {})
        basis.update({"model_tier": tier, "model_tier_source": choice.source})
        try:
            profile = WorkerProfile(**dict(
                raw, model=choice.model, reasoning=choice.reasoning,
                fallbacks=[{"model": name} for name in choice.fallbacks],
                selection_basis=basis, route_digest="",
            ))
        except (TypeError, ValueError):
            return
        contract["worker_profile"] = profile.to_dict()

    def adjust_model_tier(
        self, run_id: str, node_id: str, delta: int, reason: str
    ) -> RunSnapshot:
        """Supervisor/repair session moves a node's tier by exactly one level.

        Refused (with the allowed range) when the move leaves light..deep, and
        when no reason is given.
        """
        snapshot = load_snapshot(self.paths, run_id)
        if node_id not in snapshot.nodes:
            raise ValueError("model tier adjustment targets an unknown node")
        current = snapshot.nodes[node_id]
        before = self._effective_model_tier(self.nodes[node_id], current, "developer")
        after = model_catalog.adjust_tier(before, delta, reason)
        self._commit_model_tier_adjustment(snapshot, node_id, current, delta, before, after, reason, "supervisor")
        if self._clear_model_fallback_state(current):
            # A tier move supersedes the exhausted fallback chain: drop the
            # switched override profile (so the new tier re-resolves the
            # model) and requeue a parked node -- without a retryable_action
            # the sweep never replays it.
            if (
                not isinstance(current.get("retryable_action"), dict)
                and current.get("active_task") is None
                and node_id not in snapshot.handles
            ):
                current["status"] = self._v42_retry_status()
                current["retryable_action"] = {
                    "role": "developer",
                    "phase": "develop",
                    "continuation": False,
                    "successor": False,
                    "same_task": True,
                    "same_task_required": True,
                    "attempt": 1,
                    "reason_class": "model_unavailable",
                    "next_retry_at": time.time(),
                    "binding_digest": current.get("contract_digest", ""),
                    "last_observation_ref": "model-tier-adjusted",
                }
        self._save_snapshot(snapshot)
        return snapshot

    def _commit_model_tier_adjustment(
        self, snapshot: RunSnapshot, node_id: str, current: Dict[str, Any],
        delta: int, before: str, after: str, reason: str, actor: str,
    ) -> None:
        current["model_tier_adjustment"] = int(current.get("model_tier_adjustment", 0)) + delta
        self._record(
            snapshot,
            "model_tier_adjusted",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "proof": {"from": before, "to": after, "reason": reason, "by": actor},
            },
        )

    def _prepare_worker_profile(
        self, node: DAGNode, contract: Dict[str, Any], current: Dict[str, Any], role: str
    ) -> Optional[WorkerProfile]:
        """Resolve an evidence-bound profile for the real dispatch path.

        Legacy nodes keep their configured profile/default behavior.  A node
        that declares routing inputs must provide a complete IssueComplexity
        and structured LocalModel probes; no default model is synthesized.
        """
        routing_keys = {"routing_required", "issue_complexity", "model_probes", "models", "required_capabilities"}
        routing_requested = any(key in contract for key in routing_keys)
        raw = contract.get("worker_profile")
        if raw is None and isinstance(contract.get("child_binding"), dict):
            raw = contract["child_binding"].get("worker_profile")
        if role != "developer" and raw is None:
            raw = current.get("contract_overrides", {}).get("worker_profile")
        if raw is not None:
            try:
                profile = raw if isinstance(raw, WorkerProfile) else WorkerProfile.from_dict(raw)
            except (TypeError, ValueError) as error:
                raise ValueError("worker model profile is invalid") from error
            issue_data = contract.get("issue_complexity")
            probes = contract.get("model_probes", contract.get("models"))
            if routing_requested and contract.get("routing_required") is True:
                # An explicitly routed node cannot silently accept an
                # unrelated preconfigured/default profile.
                if issue_data is None or probes is None:
                    raise ValueError("worker model routing evidence is incomplete")
            if routing_requested and issue_data is not None and probes is not None:
                issue = issue_data if isinstance(issue_data, IssueComplexity) else IssueComplexity.from_dict(issue_data)
                if not isinstance(probes, (list, tuple)):
                    raise TypeError("model probes must be a list")
                models = [item if isinstance(item, LocalModel) else LocalModel.from_dict(item) for item in probes]
                expected = ModelRouter(worker=profile.worker).select(
                    issue, contract.get("required_capabilities", []), models
                )
                if (
                    profile.model != expected.model
                    or profile.reasoning != expected.reasoning
                    or profile.route_digest != expected.route_digest
                ):
                    chain = current.get("model_dispatch_chain")
                    if (
                        isinstance(chain, list)
                        and profile.model in chain
                        and profile.reasoning == expected.reasoning
                        and profile.route_digest == expected.route_digest
                    ):
                        # Monitor-authored model fallback: the chain was
                        # derived from this router-selected profile, so a
                        # model inside it is a dispatch-time fallback switch,
                        # not a writer conflict.  A profile outside the chain
                        # (or with drifted reasoning/digest) still fails
                        # closed below.
                        pass
                    else:
                        raise ValueError("worker model profile conflicts with routing evidence")
            expanded = list(profile.allowlist)
            for item in current.get("scope_expansions", []):
                if item not in expanded:
                    expanded.append(item)
            if expanded != list(profile.allowlist):
                profile = replace(profile, allowlist=expanded)
            contract["worker_profile"] = profile.to_dict()
            return profile
        if not routing_requested:
            return None
        issue_data = contract.get("issue_complexity")
        probes = contract.get("model_probes", contract.get("models"))
        if issue_data is None or probes is None:
            raise ValueError("worker model routing evidence is incomplete")
        issue = issue_data if isinstance(issue_data, IssueComplexity) else IssueComplexity.from_dict(issue_data)
        if not isinstance(probes, (list, tuple)):
            raise TypeError("model probes must be a list")
        models = [item if isinstance(item, LocalModel) else LocalModel.from_dict(item) for item in probes]
        profile = ModelRouter(worker=str(contract.get("worker") or node.contract.get("worker") or "developer")).select(
            issue, contract.get("required_capabilities", []), models
        )
        profile = WorkerProfile(
            profile.worker,
            profile.model,
            profile.reasoning,
            profile.fallbacks,
            profile.selection_basis,
            worktree=str(contract.get("worktree") or current.get("worktree", "")),
            branch=str(contract.get("branch") or current.get("branch", "")),
            allowlist=list(contract.get("files", [])),
            writer=str(contract.get("writer") or profile.worker),
            route_digest=profile.route_digest,
        )
        current.setdefault("contract_overrides", {})["worker_profile"] = profile.to_dict()
        expanded = list(profile.allowlist)
        for item in current.get("scope_expansions", []):
            if item not in expanded:
                expanded.append(item)
        if expanded != list(profile.allowlist):
            profile = replace(profile, allowlist=expanded)
        contract["worker_profile"] = profile.to_dict()
        return profile

    def _preflight_contract_failure(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        phase: str,
        continuation: bool,
        generation: int,
        generation_key: str,
        error: Exception,
        failure_phase: str,
        successor: bool = False,
    ) -> bool:
        """Close a dispatch lease when local validation fails before intent.

        Profile/routing/child-binding checks run after the scheduler acquires
        the writer lease.  They are deterministic contract failures, so keep
        the same task identity retryable, rewind the speculative generation,
        and release that lease before the next heartbeat.
        """
        current = snapshot.nodes[node_id]
        current[generation_key] = max(0, generation - 1)
        current["active_task"] = None
        current["active_role"] = None
        current["start_intent"] = None
        action_ref = "dispatch-" + digests.sha256_bytes(
            (snapshot.run_id + "\0" + node_id + "\0" + role + "\0" + str(generation) + "\0" + failure_phase + "\0" + type(error).__name__).encode("utf-8")
        )[:24]
        current["status"] = "blocked_unknown"
        current["reason"] = "task binding rejected before start ({})".format(type(error).__name__)
        if (
            is_integration_review_node(self.nodes[node_id])
            and role == "reviewer" and generation == 1
            and not continuation and not current.get("reviewer_identity")
        ):
            # No start intent or provider action exists at this local preflight
            # boundary. Retry normal readiness, not legacy developer recovery.
            current["status"] = "planned"
            current["reviewer_started"] = False
            current["retryable_action"] = None
        elif successor:
            # A missing historical reviewer binding is a successor recovery
            # path. Preserve its lineage marker so the scheduler can create
            # the successor on the next cycle; it is not a same-task retry.
            current["retryable_action"] = None
            if role == "reviewer":
                current["reviewer_started"] = True
        else:
            current["retryable_action"] = {
                "role": role,
                "phase": phase,
                "continuation": continuation,
                "successor": False,
                "same_task": True,
                "same_task_required": True,
                "attempt": 1,
                "reason_class": "contract_error",
                "reason_code": "dispatch_contract_invalid",
                "action_ref": action_ref,
                "next_retry_at": time.time(),
                "binding_digest": current.get("contract_digest", ""),
                "last_observation_ref": action_ref,
            }
        try:
            self._release_node_lease(snapshot, node_id)
        except (OSError, TypeError, ValueError):
            # Failure to verify/release ownership remains fail-closed; the
            # retry marker prevents a successor writer from being created.
            current["reason"] = current["reason"] + "; lease release unknown"
        self._record(
            snapshot,
            "contract_error",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": role,
                "phase": failure_phase,
                "reason_code": (
                    "integration_initial_preflight_retry"
                    if current["status"] == "planned" else "dispatch_contract_invalid"
                ),
                "action_ref": action_ref,
            },
        )
        self._save_snapshot(snapshot)
        return False

    def _start_task(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        phase: str,
        runner: Runner,
        continuation: bool,
        successor: bool = False,
    ) -> bool:
        record = self._require_snapshot_authorization(snapshot)
        node = self.nodes[node_id]
        current = snapshot.nodes[node_id]
        if current.get("workspace_reconciliation_required") is True:
            self._mark_blocked_unknown(snapshot, node_id, "workspace reconciliation required before dispatch")
            return False
        topology = self._node_dispatch_topology(node)
        supervisor_rejection = self._supervisor_writer_rejection(node, current)
        if supervisor_rejection is not None:
            self._mark_blocked_unknown(snapshot, node_id, supervisor_rejection)
            self._record(
                snapshot,
                "supervisor_writer_rejected",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": role,
                    "reason": supervisor_rejection,
                },
            )
            return False
        if topology == TOPOLOGY_VISIBLE_SDD and role == "reviewer":
            # Structural rule (not authorization-card text): a visible-sdd
            # node binds exactly one worker session; review is in-session
            # evidence and is never dispatched as a reviewer task.
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "visible-sdd topology never dispatches a reviewer task",
            )
            return False
        identity_key = role + "_identity"
        predecessor_identity = current.get(identity_key) or "{}:{}".format(
            role, node_id
        )
        identity = predecessor_identity
        generation_key = "review_generation" if role == "reviewer" else "developer_generation"
        prior_intent = current.get("start_intent")
        if isinstance(prior_intent, dict) and prior_intent.get("role") == role:
            generation = int(prior_intent.get("generation", current.get(generation_key, 0)))
            identity = str(prior_intent.get("task_id") or identity)
        else:
            generation = int(current.get(generation_key, 0)) + 1
        # #289 section 3 (PM ruling): only after a recorded platform_handoff
        # does rework/re-review of a pre-handoff task go to a successor on
        # the new platform; the old task must first land terminal.  Every
        # other resume keeps returning to the same worker.  A pending start
        # intent of this role is the in-flight successor itself.
        handoff_predecessor = None
        if (
            not successor
            and self._handoff_superseded_identity(current, role) is not None
            and not (isinstance(prior_intent, dict) and prior_intent.get("role") == role)
        ):
            handoff_predecessor = self._prove_handoff_predecessor_terminal(
                snapshot, node_id, role
            )
            if handoff_predecessor is None:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "platform handoff predecessor cannot be proven terminal for successor",
                    reason_code="platform_handoff_predecessor_unproven",
                )
                return False
            continuation = False
            successor = True
            current["old_task_reconciled"] = True
        # A first reviewer binding has no prior reviewer identity. If local
        # validation fails, preserve the historical recovery marker so the
        # next cycle can create the successor task.
        successor_recovery = bool(successor) or (
            role == "reviewer"
            and generation == 1
            and not current.get("reviewer_identity")
            and not continuation
        )
        if successor:
            identity = "{}:successor:{}".format(predecessor_identity, generation)
        current[generation_key] = generation
        if role == "reviewer":
            current["reviewer_started"] = True
            if successor:
                # A fresh reviewer identity starts a fresh unconverged-loop
                # budget: the three-finding limit exists to stop ping-pong
                # under one reviewer, so replacing the reviewer must actually
                # break the loop instead of parking on the successor's very
                # first finding (#153).
                current["review_loop_rounds"] = 0

        contract = dict(node.contract)
        contract.update(current.get("contract_overrides", {}))
        scope_expansions = list(current.get("scope_expansions", []))
        if scope_expansions:
            contract["files"] = self._node_scope_files(node_id, current)
        contract.update(
            {
                "node_id": node_id,
                "title": getattr(node, "title", contract.get("title", "")),
                "spec_path": getattr(self.plan, "spec_path", ""),
                "role": role,
                "worker": (
                    node.contract.get("reviewer_worker")
                    if role == "reviewer"
                    else current.get("worker")
                ),
                "phase": phase,
                "task_id": identity,
                "generation": generation,
                "continuation": continuation,
                "successor": successor,
                "action": phase,
                "run_id": snapshot.run_id,
                # Explicit provenance consumed by the shared complex-dispatch
                # guard in runners and adapters.  Only Monitor creates this
                # proof-bearing contract.
                "dispatcher": "monitor",
                "project_root": str(self.paths.root.resolve()),
                "plan_id": getattr(self.plan, "plan_id", ""),
                "plan_version": getattr(self.plan, "version", 0),
                "complexity_band": getattr(self.plan, "complexity_band", ""),
                "execution_engine": getattr(record, "execution_engine", "monitor"),
                "engine_mode": getattr(record, "engine_mode", "parallel"),
                "engine_evidence_ref": getattr(record, "engine_evidence_ref", ""),
                "authorization_digest": getattr(record, "digest", snapshot.authorization_digest),
                "authorized_actions": list(getattr(record, "allowed_actions", [])),
                "consistency_binding": self._consistency_binding(record, node),
            }
        )
        # The create request carries the dispatch topology with the node
        # contract, file allowlist and worktree/branch; visible-sdd sessions
        # additionally receive the SDD protocol pointer they must follow.
        contract["topology"] = topology
        if topology == TOPOLOGY_VISIBLE_SDD:
            contract["sdd_protocol"] = VISIBLE_SDD_PROTOCOL_REF
        elif topology == TOPOLOGY_BACKGROUND:
            limitations = contract.get("dispatch_limitations")
            if not (
                isinstance(limitations, list)
                and any(isinstance(item, str) and item.strip() for item in limitations)
            ):
                limitations = [BACKGROUND_TOPOLOGY_DISCLOSURE]
            contract["dispatch_limitations"] = list(limitations)
        retry = current.get("retryable_action")
        if successor:
            predecessor_task_id = None
            if isinstance(retry, dict):
                predecessor_task_id = retry.get("predecessor_task_id")
            if not predecessor_task_id:
                predecessor_task_id = current.get(identity_key)
            if handoff_predecessor is not None:
                predecessor_task_id, contract["handoff_from_provider"] = handoff_predecessor
                # Lets the provider runner and the task registry accept a
                # predecessor bound by another provider (lineage recorded as
                # ``handoff_from_provider``).  Only set after the terminal
                # proof above.
                contract["platform_handoff_successor"] = True
            contract["predecessor_task_id"] = predecessor_task_id
        contract.setdefault("worktree", str(current["worktree"]))
        contract.setdefault("branch", str(current["branch"]))
        contract["upstream_branches"] = upstream_branches(self.nodes, snapshot.nodes, node_id)
        if role == "reviewer" and continuation:
            # Resume prompts must carry the real developer delivery facts
            # observed by the monitor, bound to this snapshot generation.
            delivery = current.get("delivery_evidence")
            if isinstance(delivery, dict):
                for key in ("completion_marker", "delivery_path", "thread_status"):
                    value = delivery.get(key)
                    if isinstance(value, str) and value.strip():
                        contract[key] = value
            contract.setdefault(
                "status_file",
                str(current.get("reviewer_status_file") or current.get("status_file") or node.contract.get("status_file", "")),
            )
        contract.setdefault(
            "files", list((contract.get("worker_profile") or {}).get("allowlist", []))
        )
        routing_requested = any(
            key in contract
            for key in ("routing_required", "issue_complexity", "model_probes", "models", "required_capabilities")
        )
        try:
            profile = self._prepare_worker_profile(node, contract, current, role)
        except (OSError, TypeError, ValueError) as error:
            return self._preflight_contract_failure(
                snapshot, node_id, role, phase, continuation, generation,
                generation_key, error, "profile", successor_recovery,
            )
        if profile is not None:
            contract["worker_profile"] = profile.to_dict()
            # The profile is the canonical scope after auto-expansion. Keep
            # every envelope projection aligned before adapter validation.
            contract["files"] = list(profile.allowlist)
            if routing_requested:
                contract["routing_required"] = True
        self._apply_model_tier(snapshot, node, contract, current, role)
        if binding_contract_enabled(contract):
            has_live_evidence = isinstance(contract.get("binding_intent"), BindingIntent) and isinstance(
                contract.get("binding_observation"), BindingObservation
            )
            if not has_live_evidence:
                if continuation:
                    contract.setdefault("binding_bootstrap", True)
                    current["binding_phase"] = "binding_repair_pending"
                else:
                    contract.setdefault("binding_probe", True)
                    current["binding_phase"] = "binding_probe_pending"
            else:
                current["binding_phase"] = "binding_verified"
        if snapshot.capability_contract_digest:
            contract["capability_contract_digest"] = snapshot.capability_contract_digest
        if (self.paths.vibe / "state.json").is_file():
            try:
                workflow_version = json.loads(
                    (self.paths.vibe / "state.json").read_text(encoding="utf-8")
                ).get("workflow_version")
                v2 = workflow_version in (2, 4)
            except (OSError, ValueError, json.JSONDecodeError):
                v2 = True
            if v2:
                profile_data = contract.get("worker_profile") or {}
                if not profile_data:
                    profile_data = {"worker": str(contract.get("worker", "worker")), "model": "default", "reasoning": "normal", "fallbacks": [], "selection_basis": {"issue_complexity_ref": node_id, "complexity_band": "standard", "risk_tags": [], "availability_evidence": "runtime"}, "writer": str(contract.get("worker", "writer")), "worktree": str(contract.get("worktree", ".")), "branch": str(contract.get("branch", "branch-" + node_id)), "allowlist": list(contract.get("files", [node_id + ".py"]))}
                profile = WorkerProfile(**profile_data)
                try:
                    validate_child_session_binding(snapshot.run_id, str(self.plan.version), record.digest, node_id, role, profile)
                except (OSError, TypeError, ValueError) as error:
                    return self._preflight_contract_failure(
                        snapshot, node_id, role, phase, continuation, generation,
                        generation_key, error, "child_binding", successor_recovery,
                    )
                contract["child_origin"] = "worker_dispatch"
                contract["child_binding"] = {"parent_run_id": snapshot.run_id, "plan_revision": str(self.plan.version), "authorization_digest": record.digest, "node_id": node_id, "role": role, "writer": profile.writer, "worktree": profile.worktree, "branch": profile.branch, "allowlist": profile.allowlist, "worker_profile": profile.to_dict(), "model": profile.model, "reasoning": profile.reasoning, "route_digest": profile.route_digest, "capability_contract_digest": snapshot.capability_contract_digest}
        try:
            try:
                contract = validate_runtime_contract(
                    contract,
                    authorized_actions=record.allowed_actions,
                    authorized_files=tuple(record.file_scope) + tuple(scope_expansions),
                )
            except (OSError, TypeError, ValueError) as error:
                return self._preflight_contract_failure(
                    snapshot, node_id, role, phase, continuation, generation,
                    generation_key, error, "runtime_contract", successor_recovery,
                )
            if self._binding_recovery_requested(contract):
                self._preflight_binding_recovery(
                    snapshot, node_id, role, contract, current
                )
            else:
                self._preflight_binding_before_provider(contract)
            self._run_binding_bootstrap(snapshot, node_id, role, contract, runner, current)
            binding = self._binding_for(
                snapshot,
                node_id,
                role,
                identity,
                generation,
                "start_pending",
                runner,
                contract,
                continuation,
                successor,
            )
            self._require_binding_gate(contract, binding, runner)
            if binding_contract_enabled(contract):
                # Registry persistence intentionally strips private provenance
                # tokens.  Carry the live, verified objects through this
                # in-process call so ProviderActionRunner.start() rechecks the
                # same evidence instead of trusting a downgraded JSON row.
                contract["binding_intent"] = binding.binding_intent
                contract["binding_observation"] = binding.binding_observation
            identity = str(binding.task_id)
            current[identity_key] = identity
            contract["task_id"] = identity
            self._save_task_binding(binding)
        except ProviderPending as error:
            # Only a durable, bound successor action permits retrying the
            # same create transaction. An unbound exception still fails
            # closed; never turn it into permission for another writer.
            action_id = getattr(error, "action_id", None)
            pending_successor = successor and self._proven_successor_action(
                snapshot, node_id, role, generation, getattr(runner, "provider", None),
                action_id, getattr(error, "request_digest", None),
                contract.get("predecessor_task_id"), contract["authorization_digest"],
            )
            current[generation_key] = generation - 1
            if role == "reviewer" and generation == 1:
                current["reviewer_started"] = False
            # Binding/setup pending is not a provider-confirmed task.  Clear
            # any write-ahead active projection created before the probe so a
            # setup token can never be exposed as a formal task identity.
            current["active_task"] = None
            current["active_role"] = None
            current["start_intent"] = None
            setup_identity = contract.get("setup_identity") or contract.get("clientThreadId") or contract.get("client_thread_id")
            current["retryable_action"] = {
                "role": role,
                "phase": phase,
                "continuation": continuation,
                "successor": pending_successor,
                "same_task": not pending_successor,
                "same_task_required": not pending_successor,
                "successor_candidate": False,
                "attempt": 1,
                "reason_class": "engineering",
                "next_retry_at": time.time(),
                "binding_digest": current.get("contract_digest", ""),
                "last_observation_ref": "provider-pending",
            }
            if pending_successor:
                current["retryable_action"].update({
                    "pending_provider_action": action_id,
                    "request_digest": getattr(error, "request_digest"),
                    "predecessor_task_id": contract.get("predecessor_task_id"),
                })
            if setup_identity:
                current["retryable_action"]["setup_identity"] = str(setup_identity)
            # An asynchronous bridge response is a retry condition, not
            # evidence that the provider capability is unavailable.
            current["status"] = "blocked_unknown" if successor else "running"
            if binding_contract_enabled(contract):
                current["binding_phase"] = "retry_pending"
            current["reason"] = str(error)
            # A pending client handle is only a setup observation.  Do not
            # expose it as an active/formal task; replay the durable action
            # with the same contract and writer once the provider resolves it.
            current["active_task"] = None
            current["active_role"] = None
            # Preserve the write-ahead intent for same-task retry.
            if successor:
                self._mark_blocked_unknown(snapshot, node_id, error, quarantine_lease=False)
                if pending_successor:
                    self._record(snapshot, "successor_binding_pending", {
                        "run_id": snapshot.run_id, "node_id": node_id,
                        "proof": {"generation": generation, "provider": getattr(runner, "provider", None),
                                  "retry": dict(current["retryable_action"])},
                    })
                self._save_snapshot(snapshot)
            return False
        except ProviderUnavailable as error:
            current[generation_key] = generation - 1
            if role == "reviewer" and generation == 1:
                current["reviewer_started"] = False
            failure = classify_provider_failure(error)
            if failure["kind"] == "external_decision":
                current["retryable_action"] = None
            elif failure["kind"] in {"retryable", "capacity_wait", "repairable"}:
                fallback_plan = self._plan_model_fallback(
                    snapshot, node_id, contract, current, role, phase, continuation, error
                )
                if fallback_plan == "exhausted":
                    current["active_task"] = None
                    current["active_role"] = None
                    self._save_snapshot(snapshot)
                    return False
                current["retryable_action"] = {
                    "role": role,
                    "phase": phase,
                    "continuation": continuation,
                    "successor": False,
                    "same_task": True,
                    "same_task_required": True,
                    "attempt": 1,
                    "reason_class": failure["kind"],
                    "next_retry_at": time.time(),
                    "binding_digest": current.get("contract_digest", ""),
                    "last_observation_ref": digests.sha256_bytes(str(error).encode("utf-8")),
                }
                current["status"] = self._v42_retry_status()
                current["reason"] = redact_provider_text(str(error))
                if fallback_plan != "switch" and (
                    "reasoning" in str(error).casefold() or "model" in str(error).casefold()
                ):
                    options = dict(current.get("contract_overrides", {}).get("provider_options", {}))
                    options["omit_thinking"] = True
                    current.setdefault("contract_overrides", {})["provider_options"] = options
                    current["retryable_action"]["parameter_recovery"] = "omit_thinking"
            else:
                current["retryable_action"] = None
            current["active_task"] = None
            current["active_role"] = None
            # Preserve the write-ahead intent for same-task retry.  Unknown
            # binding/authorization evidence still fails closed; known
            # engineering failures remain schedulable.
            if failure["kind"] == "external_decision":
                self._mark_external_authorization_required(snapshot, node_id, str(error))
            elif failure["kind"] not in {"retryable", "capacity_wait", "repairable"}:
                self._mark_blocked_unknown(snapshot, node_id, error)
                # A pre-write binding observation has no provider handle yet,
                # but it is still a recoverable same-generation action. Keep
                # the retry marker after the fail-closed user status so the
                # next heartbeat re-enters the original binding probe instead
                # of merely repeating ``blocked_unknown``.
                if failure["kind"] == "binding_unknown":
                    current["retryable_action"] = {
                        "role": role,
                        "phase": phase,
                        "continuation": continuation,
                        "successor": False,
                        "same_task": True,
                        "same_task_required": True,
                        "attempt": 1,
                        "reason_class": "binding_unknown",
                        "next_retry_at": time.time(),
                        "binding_digest": current.get("contract_digest", ""),
                        "last_observation_ref": digests.sha256_bytes(str(error).encode("utf-8")),
                    }
            if binding_contract_enabled(contract) and failure["kind"] != "external_decision":
                current["binding_phase"] = "binding_repair_pending"
            self._save_snapshot(snapshot)
            return False
        except (OSError, TypeError, ValueError) as error:
            if successor_recovery:
                current[generation_key] = max(0, generation - 1)
                current["active_task"] = None
                current["active_role"] = None
                current["start_intent"] = None
                current["retryable_action"] = None
                try:
                    self._release_node_lease(snapshot, node_id)
                except (OSError, TypeError, ValueError):
                    pass
                self._mark_blocked_unknown(
                    snapshot, node_id,
                    "task binding rejected before start ({})".format(type(error).__name__),
                    quarantine_lease=False,
                    cause=error,
                )
                return False
            return self._preflight_contract_failure(
                snapshot, node_id, role, phase, continuation, generation,
                generation_key, error, "binding_validation", successor,
            )

        snapshot.tasks["{}:{}".format(node_id, role)] = binding.to_dict()
        # Write-ahead intent is immutable across provider retries.  Keep the
        # legacy fields while adding the V4.4 identity digest.
        intent = dict(current.get("start_intent") or {})
        if intent.get("role") != role or intent.get("task_id") != identity or int(intent.get("generation", generation)) != generation:
            intent = {"intent_id": uuid.uuid4().hex, "role": role, "phase": phase, "task_id": identity, "generation": generation}
        intent["intent_digest"] = self._start_intent_digest(snapshot, node_id, role, identity, generation, contract, current)
        contract["intent_digest"] = intent["intent_digest"]
        contract["start_intent"] = dict(intent)
        current["status"] = "start_pending"
        if binding_contract_enabled(contract) and current.get("binding_phase") is None:
            current["binding_phase"] = "binding_verified"
        current["active_role"] = role
        current["active_task"] = {
            "role": role,
            "task_id": identity,
            "generation": generation,
            "handle_id": None,
        }
        current["start_intent"] = intent
        self._record(
            snapshot,
            "start_intent",
            {"run_id": snapshot.run_id, "node_id": node_id, **intent},
            current["active_task"],
        )
        # The intent and lease are durable before the external side effect.
        self._save_snapshot(snapshot)
        # ISSUE-140 (expected_red): a TDD node must observe its invariant
        # commands failing before the first developer dispatch.  The flag is
        # node state, so rework/continuation never re-runs the observation.
        if (
            role == "developer"
            and isinstance(node.contract, dict)
            and isinstance(node.contract.get("expected_red"), str)
            and node.contract["expected_red"].strip()
            and not current.get("expected_red_observed")
        ):
            expected_red_failure = self._execute_contract_test_commands(
                snapshot, node_id, "expected_red", current.get("active_task")
            )
            if expected_red_failure is not None:
                # Same cleanup as the other pre-dispatch failure branches:
                # reset the active/intent fields and do not fabricate a
                # retryable action for what is a contract violation.
                current["active_task"] = None
                current["active_role"] = None
                current["start_intent"] = None
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "contract test execution failed: {}".format(expected_red_failure),
                )
                self._save_snapshot(snapshot)
                return False
            current["expected_red_observed"] = True
            self._save_snapshot(snapshot)
        self._require_snapshot_authorization(snapshot)
        try:
            # All starts pass through the intent transaction.  V4.4 providers
            # must return a structured proof; legacy runners retain RunHandle
            # compatibility until their contract opts into 4.4.
            handle = self._dispatch_with_intent(runner, contract, intent)
            if isinstance(handle, dict) and handle.get("kind") in {"binding_unknown", "provider_error", "retryable", "capacity_wait", "repairable", "contract_error"}:
                reason = handle.get("reason", handle.get("kind"))
                failure_kind = handle.get("reason_class")
                if failure_kind is None and handle.get("kind") in {"retryable", "capacity_wait", "repairable"}:
                    # Explicit runner phases are already engineering outcomes;
                    # preserve them instead of reclassifying a short/redacted
                    # reason such as ``ValueError``.
                    failure_kind = handle.get("kind")
                if handle.get("kind") == "contract_error":
                    # Local profile/child contract drift is repairable and must
                    # not become a product block or a new writer generation.
                    failure_kind = "repairable"
                failure = (
                    {"kind": failure_kind}
                    if failure_kind in {"retryable", "capacity_wait", "repairable", "binding_unknown", "external_decision"}
                    else classify_provider_failure({"kind": handle.get("kind"), "reason": reason})
                )
                if failure["kind"] in {"retryable", "capacity_wait", "repairable"}:
                    fallback_plan = self._plan_model_fallback(
                        snapshot, node_id, contract, current, role, phase, continuation, reason,
                        reason_class=handle.get("model_failure_class"),
                    )
                    if fallback_plan == "exhausted":
                        current["active_task"] = None
                        current["active_role"] = None
                        current["start_intent"] = None
                        self._save_snapshot(snapshot)
                        return False
                    previous_retry = current.get("retryable_action")
                    previous_attempt = int(previous_retry.get("attempt", 0)) if isinstance(previous_retry, dict) else 0
                    retry = {
                        "role": role, "phase": phase,
                        "continuation": continuation, "successor": False,
                        "same_task": True, "same_task_required": True,
                        "attempt": max(1, previous_attempt + 1), "reason_class": failure["kind"],
                        "next_retry_at": time.time(),
                        "binding_digest": current.get("contract_digest", ""),
                        "last_observation_ref": handle.get("action_ref") or digests.sha256_bytes(str(reason).encode("utf-8")),
                    }
                    current["status"] = self._v42_retry_status()
                    current["reason"] = redact_provider_text(str(reason))
                    current["binding_phase"] = "retry_pending"
                    current["retryable_action"] = retry
                    if fallback_plan != "switch" and (
                        "reasoning" in str(reason).casefold() or "model" in str(reason).casefold()
                    ):
                        options = dict(current.get("contract_overrides", {}).get("provider_options", {}))
                        options["omit_thinking"] = True
                        current.setdefault("contract_overrides", {})["provider_options"] = options
                        retry["parameter_recovery"] = "omit_thinking"
                    self._record(snapshot, "retry_scheduled", {"run_id": snapshot.run_id, "node_id": node_id, "role": role, "reason_class": failure["kind"], "same_task_required": True, "parameter_recovery": retry.get("parameter_recovery")})
                else:
                    current["status"] = "blocked_unknown"
                    current["reason"] = reason
                    if isinstance(handle.get("reason_code"), str):
                        current["reason_code"] = handle["reason_code"]
                        current["reason_class"] = str(failure["kind"])
                    current["binding_phase"] = handle.get("kind")
                    current["retryable_action"] = {
                        "role": role, "phase": phase,
                        "continuation": continuation, "successor": False,
                        "same_task": True, "same_task_required": True,
                        "attempt": 1, "reason_class": handle.get("kind"),
                        "next_retry_at": time.time(),
                        "binding_digest": current.get("contract_digest", ""),
                        "last_observation_ref": "provider-pending",
                    }
                    unknown_event = {"run_id": snapshot.run_id, "node_id": node_id, "intent_digest": intent.get("intent_digest"), "proof": handle.get("proof")}
                    if isinstance(handle.get("reason_code"), str):
                        unknown_event["reason_code"] = handle["reason_code"]
                        unknown_event["reason_class"] = str(failure["kind"])
                        if isinstance(handle.get("worktree"), str):
                            unknown_event["worktree"] = handle["worktree"]
                    self._record(snapshot, handle.get("kind", "binding_unknown"), unknown_event)
                current["active_task"] = None
                current["active_role"] = None
                current["start_intent"] = None
                self._save_snapshot(snapshot)
                return False
            if isinstance(handle, dict):
                provider_run_id = handle.get("run_id") or handle.get("provider_task_id") or handle.get("task_id")
                if not isinstance(provider_run_id, str) or not provider_run_id:
                    current["status"] = "blocked_unknown"; current["reason"] = "provider proof has no task handle"; self._save_snapshot(snapshot); return False
                handle = RunHandle(provider_run_id)
            # A confirmed start retires per-round model fallback bookkeeping;
            # a later dispatch failure rebuilds it from the live profile.
            for bookkeeping_key in (
                "model_dispatch_attempts", "model_dispatch_chain",
                "model_dispatch_chain_profile", "model_unavailable_rounds",
                "model_unavailable",
            ):
                current.pop(bookkeeping_key, None)
        except ProviderPending as error:
            # A bridge setup token (clientThreadId) is not a formal task. Keep
            # the writer lease and durable retry metadata, but clear the
            # active/start intent until a real provider identity is observed.
            setup_identity = contract.get("setup_identity") or contract.get("clientThreadId") or contract.get("client_thread_id")
            current["retryable_action"] = {
                "role": role,
                "phase": phase,
                "continuation": continuation,
                "successor": False,
                "same_task": True,
                "same_task_required": True,
                "attempt": 1,
                "reason_class": "engineering",
                "next_retry_at": time.time(),
                "binding_digest": current.get("contract_digest", ""),
                "last_observation_ref": "provider-pending",
                "setup_identity": str(setup_identity or "pending"),
            }
            current["status"] = self._v42_retry_status()
            current["active_task"] = None
            current["active_role"] = None
            current["start_intent"] = None
            # Preserve the write-ahead intent for same-task retry.
            current["reason"] = str(error)
            self._save_snapshot(snapshot)
            return False
        except ProviderUnavailable as error:
            # Provider request validation happens before a visible task has an
            # identity.  Treat it as an engineering retry and preserve the
            # original role/generation.  A parameter-validation failure also
            # records the smallest safe adaptation: omit optional reasoning on
            # the next native create request.
            failure = classify_provider_failure(error)
            if failure["kind"] in {"retryable", "capacity_wait", "repairable"}:
                fallback_plan = self._plan_model_fallback(
                    snapshot, node_id, contract, current, role, phase, continuation, error
                )
                if fallback_plan == "exhausted":
                    current["active_task"] = None
                    current["active_role"] = None
                    current["start_intent"] = None
                    self._save_snapshot(snapshot)
                    return False
                previous = current.get("retryable_action")
                attempt = int(previous.get("attempt", 0) or 0) + 1 if isinstance(previous, dict) else 1
                retry = {
                    "role": role,
                    "phase": phase,
                    "continuation": continuation,
                    "successor": False,
                    "same_task": True,
                    "same_task_required": True,
                    "attempt": attempt,
                    "reason_class": failure["kind"],
                    "next_retry_at": time.time() + min(60.0, float(2 ** min(attempt - 1, 5))),
                    "binding_digest": current.get("contract_digest", ""),
                    "last_observation_ref": digests.sha256_bytes(str(error).encode("utf-8")),
                }
                current["retryable_action"] = retry
                current["status"] = self._v42_retry_status()
                current["active_task"] = None
                current["active_role"] = None
                current["start_intent"] = None
                current["quarantine"] = None
                current["reason"] = redact_provider_text(str(error))
                if fallback_plan != "switch" and (
                    "reasoning" in str(error).casefold() or "model" in str(error).casefold()
                ):
                    options = dict(current.get("contract_overrides", {}).get("provider_options", {}))
                    options["omit_thinking"] = True
                    current.setdefault("contract_overrides", {})["provider_options"] = options
                    retry["parameter_recovery"] = "omit_thinking"
                self._record(
                    snapshot,
                    "retry_scheduled",
                    {
                        "run_id": snapshot.run_id,
                        "node_id": node_id,
                        "role": role,
                        "reason_class": failure["kind"],
                        "same_task_required": True,
                        "parameter_recovery": retry.get("parameter_recovery"),
                    },
                )
                self._save_snapshot(snapshot)
                return False
            raise
        except Exception as error:
            if successor:
                current["retryable_action"] = {
                    "role": role,
                    "phase": phase,
                    "continuation": continuation,
                    "successor": False,
                    "same_task": True,
                }
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "worker start outcome is unknown ({})".format(
                    type(error).__name__
                ),
                cause=error,
            )
            self._save_snapshot(snapshot)
            return False

        if (
            not isinstance(handle.run_id, str)
            or not handle.run_id
            or handle.run_id in snapshot.handles.values()
        ):
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "provider returned a duplicate or invalid active handle",
            )
            self._save_snapshot(snapshot)
            return False

        current["status"] = self._running_status(role, phase)
        if binding_contract_enabled(contract):
            current["binding_phase"] = "business_work_allowed"
        current["active_task"]["handle_id"] = handle.run_id
        current["start_intent"] = None
        current["quarantine"] = None
        previous_handle = snapshot.handles.get(node_id)
        if previous_handle and previous_handle != handle.run_id:
            # The replaced handle is dead weight: keep it out of polling and
            # out of the unregistered-handle fail-closed path.
            stale_handles = current.setdefault("stale_handles", [])
            if previous_handle not in stale_handles:
                stale_handles.append(previous_handle)
        snapshot.handles[node_id] = handle.run_id
        self._record(
            snapshot,
            "start_confirmed",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": role,
                "phase": phase,
                "task_id": identity,
                "generation": generation,
                "handle_id": handle.run_id,
            },
            current["active_task"],
        )
        try:
            self._set_binding_status(
                snapshot, node_id, role, self._running_status(role, phase)
            )
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "confirmed task binding cannot be updated ({})".format(
                    type(error).__name__
                ),
                cause=error,
            )
            self._save_snapshot(snapshot)
            return False
        self._save_snapshot(snapshot)
        pending = getattr(runner, "is_pending", None)
        if callable(pending) and pending(handle):
            # Keep the active lease, but expose the unresolved bridge action
            # as unknown until a provider-confirmed result is observed.  The
            # handle remains durable so the next tick can poll it; no second
            # writer is scheduled while this side effect is pending.
            # A queued continuation is an engineering retry condition. Keep
            # the quarantine and retry marker so acceptance remains gated on
            # a bound provider result, while allowing the scheduler to poll
            # the same action instead of presenting the run as blocked.
            current["status"] = self._v42_retry_status()
            current["quarantine"] = {
                "run_id": snapshot.run_id,
                "handle_id": handle.run_id,
                "reason": "provider action pending; result not yet confirmed",
            }
            current["reason"] = "provider action pending; result not yet confirmed"
            if continuation:
                current["retryable_action"] = {
                    "role": role,
                    "phase": phase,
                    "continuation": True,
                    "successor": False,
                    "same_task": True,
                    "same_task_required": True,
                    "attempt": 1,
                    "reason_class": "engineering",
                    "next_retry_at": time.time(),
                    "binding_digest": current.get("contract_digest", ""),
                    "last_observation_ref": "provider-pending",
                }
            self._save_snapshot(snapshot)
        return True

    @staticmethod
    def _running_status(role: str, phase: str) -> str:
        if role == "reviewer":
            return "review"
        if phase == "rework":
            return "rework"
        return "running"

    def _binding_for(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        identity: str,
        generation: int,
        status: str,
        runner: Runner,
        contract: Dict[str, Any],
        continuation: bool,
        successor: bool = False,
    ) -> TaskBinding:
        current = snapshot.nodes[node_id]
        previous_binding = None
        if continuation:
            try:
                previous_binding = self._load_task_binding(snapshot, node_id, role)
            except FileNotFoundError:
                previous_binding = None
            if previous_binding is None or previous_binding.task_id != identity:
                raise ValueError("continuation task identity is stale")
        observed_binding = getattr(runner, "task_binding", None)
        if callable(observed_binding):
            binding = observed_binding(
                contract,
                self._worktree_path(current),
                snapshot.run_id,
                status,
            )
            if not isinstance(binding, TaskBinding):
                raise ValueError("runner returned an invalid observed task binding")
            if (
                binding.issue_id != node_id
                or binding.role != role
                or binding.run_id != snapshot.run_id
                or binding.generation != generation
                or (continuation and binding.task_id != identity)
            ):
                raise ValueError("observed task binding does not match runtime scope")
        else:
            binding = TaskBinding(
                provider="runner",
                mode="background",
                issue_id=node_id,
                role=role,
                task_id=identity,
                host=None,
                worktree=str(current["worktree"]),
                branch=str(current["branch"]),
                status_file=str(current.get("status_file", "")),
                handoff_file=str(current.get("handoff_file", "")),
                run_id=snapshot.run_id,
                status=status,
                generation=generation,
                allowlist=list(contract.get("files", [])),
                capability_contract_digest=contract.get("capability_contract_digest"),
                successor_of=contract.get("predecessor_task_id") if successor else None,
                handoff_from_provider=(
                    contract.get("handoff_from_provider")
                    if successor and contract.get("platform_handoff_successor") is True
                    else None
                ),
                topology=str(contract.get("topology") or TOPOLOGY_DUAL_VISIBLE),
                limitations=list(contract.get("dispatch_limitations", [])),
            )
        # The registered binding must match the dispatch ruling.  A bridge
        # that cannot honor the ruled topology (e.g. no visible session for a
        # visible-sdd node) is fail-closed, never silently downgraded.
        if continuation and previous_binding is not None and previous_binding.successor_of and not binding.successor_of:
            # Immutable ancestry (task_registry.save_task_binding): resuming a
            # successor task must keep its predecessor link.  Dropping it makes
            # the registry refuse the continuation as "immutable successor
            # ancestry drift" and strands the node in blocked_unknown after a
            # replacement reviewer's next finding.
            binding.successor_of = previous_binding.successor_of
        if (
            continuation and previous_binding is not None
            and previous_binding.handoff_from_provider
            and not binding.handoff_from_provider
        ):
            # Same immutability for the #289 handoff lineage.
            binding.handoff_from_provider = previous_binding.handoff_from_provider
        expected_topology = str(contract.get("topology") or TOPOLOGY_DUAL_VISIBLE)
        if binding.topology != expected_topology:
            raise ValueError(
                "task binding topology does not match the dispatch ruling"
            )
        if successor:
            predecessor = contract.get("predecessor_task_id")
            if predecessor and binding.task_id == predecessor:
                raise ValueError("successor task identity reused the predecessor")
            if predecessor and binding.successor_of not in {None, predecessor}:
                raise ValueError("successor task predecessor does not match contract")
            if binding.successor_of is None:
                binding.successor_of = predecessor
            if contract.get("platform_handoff_successor") is True:
                lineage = contract.get("handoff_from_provider")
                if binding.handoff_from_provider not in {None, lineage}:
                    raise ValueError("successor handoff lineage does not match contract")
                binding.handoff_from_provider = lineage
        expected_capability_digest = contract.get("capability_contract_digest")
        if expected_capability_digest:
            if binding.capability_contract_digest not in {
                None,
                expected_capability_digest,
            } and not continuation:
                raise ValueError("task binding capability contract digest is stale")
            binding.capability_contract_digest = expected_capability_digest
        expected_allowlist = list(contract.get("files", []))
        if expected_allowlist:
            if binding.allowlist:
                if binding.allowlist != expected_allowlist:
                    if not continuation or not set(binding.allowlist).issubset(
                        set(expected_allowlist)
                    ):
                        raise ValueError("task binding allowlist does not match contract")
                    binding.allowlist = expected_allowlist
            else:
                binding.allowlist = expected_allowlist
        profile_data = contract.get("worker_profile")
        if profile_data is not None:
            profile = profile_data if isinstance(profile_data, WorkerProfile) else WorkerProfile.from_dict(profile_data)
            for field_name, expected in (
                ("route_digest", profile.route_digest),
                ("model", profile.model),
                ("reasoning", profile.reasoning),
            ):
                current_value = getattr(binding, field_name, None)
                if continuation and previous_binding is not None and current_value not in (None, ""):
                    # The persisted binding is the verified record of the
                    # model route that actually created this task.  The
                    # plan-side worker profile is a stale projection for an
                    # already-bound continuation; keep the registered route
                    # instead of rewriting or refusing the same task.
                    if current_value != getattr(previous_binding, field_name, None):
                        raise ValueError("task binding {} does not match registered route".format(field_name))
                    continue
                if current_value not in (None, "", expected):
                    raise ValueError("task binding {} does not match worker profile".format(field_name))
                setattr(binding, field_name, expected)
        expected_worktree = self._worktree_path(current)
        observed_worktree = Path(str(binding.worktree))
        if not observed_worktree.is_absolute():
            observed_worktree = self.paths.root / observed_worktree
        if (
            observed_worktree.resolve() != expected_worktree.resolve()
            or binding.branch != str(current["branch"])
        ):
            raise ValueError("task binding worktree or branch does not match contract")
        if continuation and previous_binding is not None:
            if (
                binding.cursor is not None
                and binding.cursor != previous_binding.cursor
            ):
                raise ValueError("continuation cursor is stale")
            binding.cursor = previous_binding.cursor
        return binding

    def _set_binding_status(
        self, snapshot: RunSnapshot, node_id: str, role: str, status: str
    ) -> TaskBinding:
        binding = self._load_task_binding(snapshot, node_id, role)
        current = snapshot.nodes[node_id]
        generation_key = (
            "review_generation" if role == "reviewer" else "developer_generation"
        )
        if (
            binding.task_id != current.get(role + "_identity")
            or binding.generation != current.get(generation_key)
        ):
            raise ValueError("task binding identity or generation is stale")
        binding.status = status
        self._save_task_binding(binding)
        snapshot.tasks["{}:{}".format(node_id, role)] = binding.to_dict()
        return binding

    def _registered_active_binding(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        provenance: Dict[str, Any],
    ) -> TaskBinding:
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict):
            raise ValueError("event has no active registered task")
        if any(
            provenance.get(key) != active.get(key)
            for key in ("role", "task_id", "handle_id", "generation")
        ):
            raise ValueError("event provenance is stale or unregistered")
        if snapshot.handles.get(node_id) != active.get("handle_id"):
            raise ValueError("event handle is not the active run handle")
        binding = self._load_task_binding(snapshot, node_id, str(active["role"]))
        if (
            binding.task_id != active.get("task_id")
            or binding.generation != active.get("generation")
        ):
            raise ValueError("event task binding is stale")
        return binding

    def _derive_integration_acceptance(
        self,
        snapshot: RunSnapshot,
        claim: Any,
        contract: Any = None,
        refs: Any = None,
        excluded: Any = None,
    ):
        """Derive the run-level package without writing anything.

        Returns ``(package, None, False, None)`` on success and
        ``(None, reason, recoverable, code)`` otherwise.  ``code`` (#254) is the
        machine-readable rejection class the free-text ``reason`` cannot
        survive persistence with: it is built only from program constants (the
        claim key names and expected types), so it stays readable after the
        provider-text redaction the ``reason`` goes through.  Splitting
        derivation from the write is what lets the live acceptance path
        validate first: a malformed claim (recoverable) is a format error the
        same reviewer can correct and re-report on its still-live handle, while
        a contract digest mismatch or open P0-P2 findings stay fail-closed.
        Only a schema/derivation failure is recoverable -- everything else is an
        evidence or scope problem a re-report cannot fix.
        """
        if contract is None or refs is None or excluded is None:
            contract = integration_contract_projection(self.plan, list(self.nodes.values()))
            record = self._snapshot_record(snapshot)
            if digest_integration_contract(contract) != record.integration_contract_digest:
                return (
                    None,
                    "integration contract no longer matches the authorized digest",
                    False,
                    "integration_derivation:contract_digest_drift",
                )
            if contract:
                refs = contract.get("agentsmd_acceptance_refs") or []
                excluded = contract.get("unverified_or_excluded") or []
            else:
                refs = getattr(self.plan, "agentsmd_acceptance_refs", [])
                excluded = getattr(self.plan, "unverified_or_excluded", [])
        try:
            package = build_integration_review_evidence(
                snapshot, claim, list(refs or []), list(excluded or [])
            )
        except (TypeError, ValueError) as error:
            # A typed claim error carries its stable code; anything else that
            # fails derivation (lineage, digest, scope) is run state rather
            # than claim shape and gets the shared fallback code.
            code = getattr(error, "code", None)
            if not isinstance(code, str) or not code:
                code = "integration_derivation:unclassified"
            return (
                None,
                "integration review evidence cannot be derived ({})".format(error),
                True,
                code,
            )
        if any(package["clearance"][severity] for severity in ("p0", "p1", "p2")):
            return (
                None,
                "integration review acceptance still reports open P0-P2 findings",
                False,
                "integration_derivation:open_p0_p2_findings",
            )
        return package, None, False, None

    def _self_report_receipt(self, snapshot, node_id, role, active, binding, delivery):
        """The consumed ``worker-deliver`` wait for the active task, or None."""
        if not isinstance(active, dict) or binding.task_id != active.get("task_id") \
                or binding.generation != active.get("generation"):
            return None
        return ProviderActionStore(self.paths).self_report_receipt(
            snapshot.run_id, node_id, role, active.get("generation"),
            binding.task_id, binding.host or binding.hostId, delivery,
        )

    def _background_receipt(self, snapshot, node_id, role, active, binding, delivery):
        """The archived self-report of the active background task, or None."""
        if not isinstance(active, dict) or binding.task_id != active.get("task_id") \
                or binding.generation != active.get("generation"):
            return None
        return ProviderActionStore(self.paths).background_delivery_receipt(
            snapshot.run_id, node_id, role, active.get("generation"),
            binding.task_id, delivery,
        )

    def _archived_blocked_engineering(self, snapshot, node_id, role, active):
        """The consumed archive for this task is a blocked_engineering report."""
        if not isinstance(active, dict):
            return False
        store_paths = getattr(self, "paths", None)
        if store_paths is None:
            return False
        payload = ProviderActionStore(store_paths).archived_payload(
            snapshot.run_id, node_id, role, active.get("generation")
        )
        return isinstance(payload, dict) and payload.get("event") == "blocked_engineering"

    def _park_blocked_engineering(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        event: Any,
        active: Optional[Dict[str, Any]],
        handle_id: Optional[str],
    ) -> None:
        """Park a worker's blocked self-report as supervisor disposition (#335).

        The node stays ``blocked_unknown`` with the fixed ``engineering_blocked``
        class; no reviewer is dispatched and no retryable action is scheduled,
        because a declared engineering block cannot self-heal.  Everything
        stamped here is a program constant: the worker's free text never
        reaches the record or the event.
        """
        data = event.data if isinstance(event.data, dict) else {}
        fields = blocked_self_report_fields(data) or ["event"]
        reason = "worker reported a blocked engineering state ({})".format(
            ", ".join(fields)
        )
        current = snapshot.nodes[node_id]
        current["status"] = "blocked_unknown"
        current["reason"] = reason
        current["reason_class"] = "engineering_blocked"
        current["reason_code"] = "blocked_self_report"
        current.pop("recovery_action", None)
        current.pop("error_type", None)
        current["retryable_action"] = None
        current["active_role"] = None
        current["active_task"] = None
        snapshot.handles.pop(node_id, None)
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": handle_id,
            "reason": reason,
        }
        self._release_node_lease(snapshot, node_id)
        if isinstance(active, dict) and active.get("role") in {"developer", "reviewer"}:
            try:
                self._set_binding_status(
                    snapshot, node_id, str(active["role"]), "blocked_unknown"
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
        self._record(
            snapshot,
            "blocked_engineering",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "role": role,
                "generation": active.get("generation") if isinstance(active, dict) else None,
                "handle_id": handle_id,
                "reason_class": "engineering_blocked",
                "reason_code": "blocked_self_report",
            },
            active,
        )

    def _reject_acceptance_format(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        active: Dict[str, Any],
        reason: str,
        rejection_code: Optional[str] = None,
        extra_event_data: Optional[Dict[str, Any]] = None,
        runner: Optional[Runner] = None,
        handle_id: Optional[str] = None,
    ) -> None:
        """Record a validate-first rejection as pure audit and keep the handle.

        A malformed acceptance/delivery payload is a format error in the same
        task's report, not an unknown side effect: the event is not recorded
        as a lifecycle transition, the binding is not flipped, and the handle
        stays in ``snapshot.handles`` so the next poll re-issues the provider
        wait on the same session -- the reviewer/worker can correct the
        payload and report again.  Replay treats ``acceptance_rejected`` as a
        no-op for the same reason: nothing here mutated lifecycle state.

        The node ``reason`` is a fixed, decision-marker-free text: the raw
        validator reason stays only in the redacted audit event, because
        free-form reasons (e.g. key names like ``out_of_scope``) otherwise
        trip the user-facing "needs your decision" mapping for an error the
        same session recovers from on its own.  Crash-window trade-off: if
        the process dies after this event is appended but before the
        snapshot is saved, the replayed no-op restores neither the evidence
        entry nor this reason -- the durable trail is the audit event alone.

        #254: besides that redacted free text the event also carries
        ``rejection_code`` -- a stable, assertable machine-readable class
        (error kind plus the involved key names / expected types, all program
        constants) under a non-provider-text event key, so an auditor can tell
        a wrong key set from a wrong type from an empty value without
        un-redacting anything.  #255: consecutive rejections of the same
        session (same handle + generation) are counted durably on the node
        (``acceptance_format_rejections``); when no live entity can re-report
        -- the incident case where the reviewer session exited right after
        delivering and every tick re-read the same payload -- the count hits
        ``ACCEPTANCE_FORMAT_REJECTION_LIMIT`` and the rejection escalates to a
        visible ``blocked_unknown`` with ``recovery_action`` instead of
        replaying forever.
        """
        current = snapshot.nodes[node_id]
        count = self._note_acceptance_format_rejection(
            snapshot, node_id, active, rejection_code
        )
        if count >= ACCEPTANCE_FORMAT_REJECTION_LIMIT:
            self._escalate_acceptance_format_rejection(
                snapshot, node_id, active, reason, rejection_code, count,
                runner, handle_id,
            )
            return
        current["reason"] = (
            "acceptance report format error; "
            "the same session can correct and re-report"
        )
        event_data = {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "reason": redact_provider_text(reason),
            "recoverable": True,
        }
        if isinstance(rejection_code, str) and rejection_code:
            event_data["rejection_code"] = rejection_code
        event_data["rejection_count"] = count
        if extra_event_data:
            event_data.update(extra_event_data)
        self._record(snapshot, "acceptance_rejected", event_data, active)
        self._notify_rejected_reviewer(snapshot, node_id, active, runner, handle_id)

    def _notify_rejected_reviewer(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        active: Optional[Dict[str, Any]],
        runner: Optional[Runner],
        handle_id: Optional[str],
    ) -> None:
        """Tell the same reviewer session its report was rejected (D2).

        Fixed text only, reviewer sessions only, best effort: a runner without
        ``send_notice`` or a failing send leaves the rejection itself intact.
        The supervisor sees the outcome through ``acceptance_correction_notified``
        and the node's ``acceptance_correction_notice`` flag.
        """
        if not (isinstance(active, dict) and active.get("role") == "reviewer"):
            return
        sender = getattr(runner, "send_notice", None)
        if not callable(sender) or not handle_id:
            return
        delivered = True
        try:
            sender(RunHandle(handle_id), ACCEPTANCE_REJECTION_NOTICE)
        except Exception:  # noqa: BLE001 - the notice is advisory
            delivered = False
        snapshot.nodes[node_id]["acceptance_correction_notice"] = {"delivered": delivered}
        self._record(
            snapshot,
            "acceptance_correction_notified",
            {"run_id": snapshot.run_id, "node_id": node_id, "delivered": delivered},
            active,
        )

    def _note_acceptance_format_rejection(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        active: Optional[Dict[str, Any]],
        rejection_code: Optional[str],
    ) -> int:
        """Count one consecutive format rejection for the same session.

        The count is keyed by the active task's handle and generation, both of
        which survive a crash because the counter lives on the durable node
        record (not in memory): a corrected re-report reaches acceptance and
        clears it, a new dispatch generation starts a fresh count, and the same
        unchanged delivery re-read on every tick keeps raising it -- exactly the
        signal that distinguishes "the session will fix this" from "nobody can
        re-report this" (#255).  Returns the new count.
        """
        current = snapshot.nodes[node_id]
        task = active if isinstance(active, dict) else {}
        record = current.get("acceptance_format_rejections")
        count = 1
        if (
            isinstance(record, dict)
            and record.get("handle_id") == task.get("handle_id")
            and record.get("generation") == task.get("generation")
        ):
            try:
                count = int(record.get("count", 0)) + 1
            except (TypeError, ValueError):
                count = 1
        current["acceptance_format_rejections"] = {
            "role": task.get("role"),
            "handle_id": task.get("handle_id"),
            "generation": task.get("generation"),
            "count": count,
            "last_code": rejection_code,
        }
        return count

    def _escalate_acceptance_format_rejection(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        active: Optional[Dict[str, Any]],
        reason: str,
        rejection_code: Optional[str],
        count: int,
        runner: Optional[Runner] = None,
        handle_id: Optional[str] = None,
    ) -> None:
        """Stop the rejection replay loop and make the stall visible (#255).

        Retiring the re-report channel is deliberate, not a new brick: the
        session that produced the payload has already spent
        ``ACCEPTANCE_FORMAT_REJECTION_LIMIT`` re-reads unable to correct it, so
        continuing to poll it can only replay the same delivery.  The node is
        parked as ``blocked_unknown`` (user-visible, run-level status follows),
        the durable counter keeps ``count``/``last_code`` for the audit trail,
        and ``recovery_action`` names the human entries: reshape the delivery
        per the pinned schema and re-deliver through ``vibe reconcile``
        (``Monitor.reconcile_evidence`` replays ordinary delivered/accepted
        events on the original bindings), or ``vibe requeue`` to re-dispatch.
        The integration reviewer's reshaped claim closes through
        ``Monitor.record_integration_review``.
        """
        current = snapshot.nodes[node_id]
        current["status"] = "blocked_unknown"
        current["reason"] = (
            "acceptance report format rejected {} times; "
            "no live session can correct and re-report".format(count)
        )
        current["recovery_action"] = ACCEPTANCE_FORMAT_ESCALATION_RECOVERY
        current["active_role"] = None
        current["active_task"] = None
        # The retry marker is what could re-activate this same session on the
        # next resume; an un-reportable delivery must not consume it.
        current["retryable_action"] = None
        # A quarantine means "the same task may still be resumed"; the
        # escalated rejection is the opposite claim -- nobody alive can
        # re-report.  Leaving it would let the delivered-developer
        # continuation recovery re-dispatch the node on the same tick and
        # silently overwrite the parked state.
        current["quarantine"] = None
        snapshot.handles.pop(node_id, None)
        record = current.get("acceptance_format_rejections")
        if isinstance(record, dict):
            record["escalated"] = True
        role = active.get("role") if isinstance(active, dict) else None
        stopped = False
        if role == "reviewer":
            # Only a *proven* stop may make the retired reviewer terminal: a
            # visible successor is registered over a stopped/failed/archived
            # predecessor (``provider_action.task_binding`` and
            # ``save_task_binding`` enforce it), so a terminal status written
            # without proof would let a second writer in beside a session
            # that may still be alive.  Repeated format rejections do not
            # prove the session ended.  ``runner.stop`` does: claude-bg stops
            # the session for real, and provider_action returns at once for a
            # handle whose terminal wait it already confirmed (the delivered
            # report).  Without that proof the binding stays non-terminal and
            # no replacement is armed; the human entry is ``vibe reconcile``.
            # ``_stop_active_for_transition`` is not reused: on failure it
            # re-parks the node with a different reason and recovery action,
            # while this escalation already parked it as documented.
            if runner is not None and isinstance(handle_id, str) and handle_id:
                try:
                    runner.stop(RunHandle(handle_id))
                    self._set_binding_status(snapshot, node_id, "reviewer", "stopped")
                    stopped = True
                except Exception:
                    stopped = False
        if role in {"developer", "reviewer"} and not stopped:
            # Same non-terminal status ``_mark_blocked_unknown`` writes: the
            # identity is parked, never released to a successor.
            try:
                self._set_binding_status(snapshot, node_id, str(role), "blocked_unknown")
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
        if role == "reviewer" and stopped:
            # Arm the reviewer-replacement recovery (same markers #153 stamps):
            # `vibe repair` then re-dispatches the review under a fresh
            # reviewer identity instead of resuming the session that could not
            # produce a well-formed report.
            current["reviewer_replacement_required"] = True
            current["reviewer_started"] = False
            current["pair_archived"] = True
            # Keep the stopped identity as the successor predecessor, the same
            # marker #153 and the reviewer-write-violation path stamp, so the
            # replacement review cannot accidentally reuse it.
            current["reviewer_identity"] = (
                active.get("task_id") if isinstance(active, dict) else None
            )
        event_data = {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "reason": redact_provider_text(reason),
            # Escalated rejections are no longer same-session recoverable; the
            # recovery path is the human one named in recovery_action.
            "recoverable": False,
            "rejection_count": count,
        }
        if isinstance(rejection_code, str) and rejection_code:
            event_data["rejection_code"] = rejection_code
        self._record(snapshot, "acceptance_rejected", event_data, active)

    def _apply_event(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        handle_id: str,
        event: RunEvent,
        runner: Runner,
    ) -> None:
        self._require_snapshot_authorization(snapshot)
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict) or active.get("handle_id") != handle_id:
            if handle_id in (current.get("stale_handles") or ()):
                # A superseded session still emitted something; the event is
                # evidence-free for the new dispatch, so record that it was
                # ignored instead of parking the node as blocked_unknown.
                self._record(
                    snapshot,
                    "stale_handle_event_ignored",
                    {
                        "run_id": snapshot.run_id,
                        "node_id": node_id,
                        "handle_id": handle_id,
                        "reason": "event from a superseded session ignored",
                    },
                )
                return
            self._mark_blocked_unknown(
                snapshot, node_id, "event arrived from an unregistered handle"
            )
            return
        if not self._event_claims_match(event, node_id, active):
            self._mark_blocked_unknown(
                snapshot, node_id, "event provenance conflicts with the registered task"
            )
            return

        if current.get("workspace_reconciliation_required") is True:
            self._mark_blocked_unknown(snapshot, node_id, "workspace reconciliation required before acceptance")
            return
        evidence = event.data.get("evidence") or event.data.get("finding")
        if evidence is not None:
            current.setdefault("evidence", []).append(redact_provider_text(evidence))
        role = active["role"]
        if role == "reviewer" and self._reviewer_write_violation(event.data):
            if is_integration_review_node(self.nodes[node_id]):
                # The pollution fact precedes stop: a failed stop must never
                # make a reported write eligible for acceptance again.
                self._record(snapshot, "integration_workspace_quarantined", {
                    "run_id": snapshot.run_id, "node_id": node_id,
                }, active)
                current["workspace_reconciliation_required"] = True
                current["acceptance"] = None
                current["reviewer_replacement_required"] = True
                self._save_snapshot(snapshot)
            # A reviewer that touched business files invalidates the whole
            # review round.  Stop that task, preserve the developer identity,
            # and force the next review to use a fresh reviewer identity.
            if not self._stop_active_for_transition(
                snapshot, node_id, role, handle_id, runner, "stopped"
            ):
                return
            current["status"] = "rework"
            current["reason"] = (
                "reviewer write violation; review round voided; returned to developer and reviewer replacement required"
            )
            current["recovery_action"] = "return_to_developer_and_change_reviewer"
            current["reviewer_replacement_required"] = True
            current["reviewer_started"] = False
            # Keep the stopped identity as the successor predecessor so the
            # next reviewer cannot accidentally reuse it.
            current["reviewer_identity"] = active.get("task_id")
            current["active_role"] = None
            current["active_task"] = None
            snapshot.handles.pop(node_id, None)
            self._record(
                snapshot,
                "reviewer_write_violation",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "reason": current["reason"],
                    "modified_files": event.data.get("reviewer_modified_files", []),
                    "review_round_voided": True,
                    "reviewer_replacement_required": True,
                },
                active,
            )
            if is_integration_review_node(self.nodes[node_id]):
                # A read-only closeout has no developer to receive rework.
                # Preserve the stopped reviewer and quarantine the reported
                # writes; a fresh reviewer cannot certify unverified pollution.
                current["acceptance"] = None
                current["workspace_reconciliation_required"] = True
                current["recovery_action"] = "reconcile_binding"
                self._mark_blocked_unknown(
                    snapshot, node_id, "integration reviewer write violation; workspace reconciliation required"
                )
                return
            try:
                self._start_task(
                    snapshot, node_id, "developer", "rework", runner, True, False
                )
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "reviewer violation could not return to developer ({})".format(
                        type(error).__name__
                    ),
                    cause=error,
                )
            return
        if event.event not in {
            "unknown",
            "timeout",
            "state_unknown",
            "visibility_unknown",
        }:
            # A provider-confirmed event consumes any reauthorization retry
            # marker; the active handle now carries the durable continuation.
            current["retryable_action"] = None

        # A worker may surface a product decision as part of a normal delivery.
        # Persist it before changing lifecycle state so a shift can recover the
        # same question from disk even when the worker session is archived.
        decision_requests = (
            event.data.get("decision_requests")
            if isinstance(event.data, dict)
            else None
        )
        if event.event in {"delivered", "complete"} and decision_requests:
            try:
                decisions, notifications = ingest_worker_decisions(
                    self.paths, snapshot.run_id, node_id, decision_requests
                )
            except (OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot, node_id, "decision request could not be persisted ({})".format(type(error).__name__),
                    cause=error,
                )
                return
            notification_unknown = any(
                isinstance(item, dict) and item.get("status") == "unknown"
                for item in notifications
            )
            decision_reason = (
                DECISION_NOTIFICATION_UNKNOWN_REASON
                if notification_unknown
                else "decision pending"
            )
            self._record(
                snapshot,
                "decision_raised",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "decision_ids": [item["decision_id"] for item in decisions],
                    "source": "worker_delivery",
                    "reason": decision_reason,
                    # `reason` is redacted on disk; replay reads this (#256).
                    "notification_status": "unknown" if notification_unknown else "sent",
                },
                active,
            )
            current["decision_ids"] = [item["decision_id"] for item in decisions]
            current["status"] = "blocked_unknown" if notification_unknown else "blocked_design"
            current["reason"] = decision_reason
            if notification_unknown:
                current["quarantine"] = {
                    "run_id": snapshot.run_id,
                    "handle_id": handle_id,
                    "reason": decision_reason,
                }
                try:
                    self._set_binding_status(snapshot, node_id, str(active["role"]), "blocked_unknown")
                except (FileNotFoundError, OSError, TypeError, ValueError):
                    pass
            current["active_role"] = None
            current["active_task"] = None
            snapshot.handles.pop(node_id, None)
            self._release_node_lease(snapshot, node_id)
            return

        if event.event == "blocked_engineering":
            # #335: the worker surfaced an engineering blocker through the
            # delivery channel.  That is not a delivery: park the node for
            # supervisor disposition without dispatching a reviewer and
            # without an auto-retry marker claiming the block self-heals.
            self._park_blocked_engineering(snapshot, node_id, role, event, active, handle_id)
            return
        if event.event in {"delivered", "complete"}:
            delivery = event.data.get("delivery_evidence")
            store_paths = getattr(self, "paths", None)
            archived = (ProviderActionStore(store_paths).archived_payload(
                snapshot.run_id, node_id, role, active.get("generation")
            ) if store_paths is not None and isinstance(active, dict) else None)
            blocked_fields = blocked_self_report_fields(
                archived if archived is not None
                else (event.data if isinstance(event.data, dict) else {})
            )
            if blocked_fields:
                # Defense in depth behind the worker-deliver shape gate: a
                # blocked self-report that still reached a delivery event
                # (archived before #335, or past validation) is parked, never
                # judged as a delivery.  The archive, not the event data, is
                # read first: runner projections forward only the delivery
                # vocabulary and would hide the blocked fields.
                self._park_blocked_engineering(snapshot, node_id, role, event, active, handle_id)
                return
            if snapshot.execution_engine == "vibeguide_monitor":
                binding = self._load_task_binding(snapshot, node_id, role)
                background_receipt = self._background_receipt(
                    snapshot, node_id, role, active, binding, delivery
                )
                gate = evaluate_delivery_evidence(
                    {**current, "status": "DELIVERED"},
                    binding,
                    delivery,
                    self_report_receipt=self._self_report_receipt(
                        snapshot, node_id, role, active, binding, delivery
                    ) is not None,
                    background_receipt=background_receipt is not None,
                    self_report=archived if archived is not None else event.data,
                )
                if not gate.complete:
                    if gate.classification == "acceptance_rejected":
                        # Pure artifact-format gap: audit the rejection, keep
                        # the handle and binding so the same task can
                        # re-report a corrected delivery in this session.  The
                        # structured code names the missing artifact fields;
                        # the free-text reasons stay redacted beside it.
                        self._reject_acceptance_format(
                            snapshot,
                            node_id,
                            active,
                            "; ".join(gate.reasons),
                            gate.code,
                            {
                                "disposition": gate.classification,
                                "finding": list(gate.reasons),
                            },
                            runner=runner,
                            handle_id=handle_id,
                        )
                        self._save_snapshot(snapshot)
                        return
                    self._mark_blocked_unknown(snapshot, node_id, "; ".join(gate.reasons))
                    return
            if role == "developer" and not check_delivery_landing(self, snapshot, node_id, active):
                return
            if role == "developer" and not check_delivery_content_landing(
                self, snapshot, node_id, active, runner=runner, handle_id=handle_id
            ):
                return
            visible_sdd_delivery = (
                role == "developer"
                and self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD
            )
            if visible_sdd_delivery:
                # Validate first (ISSUE-83): the in-session review evidence is
                # checked before the delivery event is recorded, the binding is
                # flipped, or the handle is popped.  A missing/malformed field
                # or a foreign protocol pointer is a format error the same
                # worker session can correct and re-report; only contract
                # digest drift stays fail-closed.
                review_failure = self._validate_visible_sdd_review(
                    snapshot, node_id, event, current
                )
                if review_failure is not None:
                    reason, recoverable, code = review_failure
                    if recoverable:
                        self._reject_acceptance_format(
                            snapshot, node_id, active, reason, code,
                            runner=runner, handle_id=handle_id,
                        )
                        return
                    self._mark_blocked_unknown(snapshot, node_id, reason)
                    return
                self._defer_structural_pending_items(snapshot, node_id, event, current)
                undisposed = self._undisposed_blocked_unknowns(
                    current, event.data.get("in_session_review")
                )
                if undisposed:
                    self._pause_delivery_for_blocked_unknowns(
                        snapshot, node_id, event, current, undisposed
                    )
                    return
            self._record_runner_event(snapshot, node_id, event, active)
            # Keep the provider's real delivery facts for reviewer
            # continuation.  Synthetic prompt values must not replace event
            # evidence received for this generation.
            if isinstance(delivery, dict):
                projected_delivery = {
                    key: delivery.get(key)
                    for key in ("completion_marker", "delivery_path", "thread_status")
                    if isinstance(delivery.get(key), str) and delivery.get(key).strip()
                }
                if projected_delivery:
                    current["delivery_evidence"] = projected_delivery
                    current["completion_marker"] = projected_delivery.get("completion_marker", "")
                    current["delivery_path"] = projected_delivery.get("delivery_path", "")
                    current["thread_status"] = projected_delivery.get("thread_status", "")
            if role != "developer":
                try:
                    self._set_binding_status(snapshot, node_id, role, "review")
                except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "task binding cannot record review state ({})".format(
                            type(error).__name__
                        ),
                        cause=error,
                    )
                    return
                current["status"] = "review"
                return
            try:
                self._set_binding_status(snapshot, node_id, role, "delivered")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record delivery ({})".format(
                        type(error).__name__
                    ),
                    cause=error,
                )
                return
            current["status"] = "delivered"
            current["active_role"] = None
            current["active_task"] = None
            snapshot.handles.pop(node_id, None)
            if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
                self._accept_visible_sdd_delivery(snapshot, node_id, event, current)
                return
            reviewer_continuation = False
            reviewer_successor = False
            replacement_required = bool(
                current.get("reviewer_replacement_required")
            )
            if replacement_required:
                # A voided review round must never resume the reviewer that
                # violated the write boundary. Keep its identity as the
                # successor predecessor while the fresh binding is created.
                reviewer_successor = True
            elif current.get("reviewer_started"):
                try:
                    reviewer_binding = self._load_task_binding(snapshot, node_id, "reviewer")
                except FileNotFoundError:
                    reviewer_successor = True
                except (OSError, TypeError, ValueError) as error:
                    self._mark_blocked_unknown(
                        snapshot,
                        node_id,
                        "reviewer task binding cannot be verified ({})".format(
                            type(error).__name__
                        ),
                        cause=error,
                    )
                    return
                else:
                    expected_reviewer_identity = current.get("reviewer_identity")
                    if (
                        not expected_reviewer_identity
                        or reviewer_binding.task_id != expected_reviewer_identity
                    ):
                        self._mark_blocked_unknown(
                            snapshot,
                            node_id,
                            "reviewer task binding identity is stale",
                        )
                        return
                    reviewer_continuation = True
            reviewer_started = self._start_task(
                snapshot,
                node_id,
                "reviewer",
                "review",
                runner,
                reviewer_continuation,
                reviewer_successor,
            )
            if replacement_required and reviewer_started and current.get("status") == "review":
                # Provider-pending/failed starts return without a confirmed
                # review task and must keep the replacement gate closed.
                current["reviewer_replacement_required"] = False
        elif event.event == "review_finding":
            if role != "reviewer":
                self._mark_blocked_unknown(
                    snapshot, node_id, "review finding did not come from the reviewer"
                )
                return
            self._record_runner_event(snapshot, node_id, event, active)
            if node_id == "integration-review":
                target_node_id = event.data.get("target_node_id")
                scope_request = event.data.get("scope_expansion")
                if target_node_id is not None or scope_request is not None:
                    if (
                        not isinstance(target_node_id, str)
                        or target_node_id == "integration-review"
                        or target_node_id not in snapshot.nodes
                        or snapshot.nodes[target_node_id].get("status") != "accepted"
                    ):
                        self._mark_blocked_unknown(
                            snapshot,
                            node_id,
                            "integration scope expansion target is invalid",
                        )
                        return
                    resolution = self._resolve_scope_request(
                        snapshot, target_node_id, scope_request
                    )
                    if resolution is None or not resolution.files:
                        requested_files = _requested_scope_files(scope_request)
                        current["status"] = "blocked_design"
                        current["blocked_design_cause"] = "integration_scope_review"
                        current["blocked_design_target"] = target_node_id
                        current["blocked_design_files"] = requested_files
                        current["reason"] = redact_provider_text(
                            "integration scope expansion requires product-manager review"
                        )
                        current["active_role"] = None
                        current["active_task"] = None
                        snapshot.handles.pop(node_id, None)
                        self._release_node_lease(snapshot, node_id)
                        self._record(
                            snapshot,
                            "blocked_design",
                            {
                                "run_id": snapshot.run_id,
                                "node_id": node_id,
                                "reason": current["reason"],
                                "old_task_reconciled": True,
                                "blocked_design_cause": "integration_scope_review",
                                "blocked_design_target": target_node_id,
                                "blocked_design_files": requested_files,
                            },
                        )
                        self._ensure_blocked_design_decision(snapshot, node_id)
                        return
                    self._apply_scope_expansion(
                        snapshot,
                        target_node_id,
                        resolution,
                        "integration_rework",
                        queue_rework=True,
                        active=active,
                    )
                # The integration node is read-only: findings never create a
                # business writer. Preserve the reviewer lineage and queue the
                # same visible reviewer task for re-review.
                current.setdefault("integration_findings", []).append(redact_provider_text(event.data))
                try:
                    review_round = int(current.get("review_loop_rounds", 0)) + 1
                except (TypeError, ValueError):
                    review_round = 1
                current["review_loop_rounds"] = review_round
                if review_round >= REVIEW_LOOP_LIMIT:
                    self._park_review_loop(
                        snapshot, node_id, current, active, role, handle_id, runner, review_round
                    )
                    return
                current["status"] = "rework"
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                current["retryable_action"] = {
                    "role": "reviewer", "phase": "review", "continuation": True,
                    "pending_schedule": True, "successor": False,
                }
                self._release_node_lease(snapshot, node_id)
                return
            disposition = self._review_finding_disposition(event.data)
            if disposition == "observation":
                current.setdefault("review_observations", []).append(
                    redact_provider_text(event.data)
                )
                return
            if disposition == "implementation" and event.data.get("consistency") is None:
                if self._finding_changes_prd_acceptance(event.data):
                    # A disagreement about the acceptance judgment itself is a
                    # product decision: escalate to the PM instead of spending
                    # the engineering rework budget on it (#153).
                    if not self._stop_active_for_transition(
                        snapshot, node_id, role, handle_id, runner, "blocked_design"
                    ):
                        return
                    current["status"] = "blocked_design"
                    current["blocked_design_cause"] = "out_of_contract_finding"
                    current["reason"] = redact_provider_text(
                        event.data.get("finding", "out-of-contract finding")
                    )
                    current["old_task_reconciled"] = True
                    current["active_role"] = None
                    current["active_task"] = None
                    snapshot.handles.pop(node_id, None)
                    self._release_node_lease(snapshot, node_id)
                    self._record(
                        snapshot,
                        "blocked_design",
                        {
                            "run_id": snapshot.run_id,
                            "node_id": node_id,
                            "reason": current["reason"],
                            "old_task_reconciled": True,
                            "blocked_design_cause": "out_of_contract_finding",
                        },
                    )
                    self._ensure_blocked_design_decision(snapshot, node_id)
                    return
                try:
                    review_round = int(current.get("review_loop_rounds", 0)) + 1
                except (TypeError, ValueError):
                    review_round = 1
                current["review_loop_rounds"] = review_round
                if review_round == 2:
                    before = self._effective_model_tier(self.nodes[node_id], current, "developer")
                    if before != model_catalog.TIERS[-1]:
                        self._commit_model_tier_adjustment(
                            snapshot, node_id, current, 1, before,
                            model_catalog.adjust_tier(before, 1, "review rejected twice"),
                            "review rejected twice", "monitor",
                        )
                if review_round >= REVIEW_LOOP_LIMIT:
                    self._park_review_loop(
                        snapshot, node_id, current, active, role, handle_id, runner, review_round
                    )
                    return
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._start_task(
                    snapshot, node_id, "developer", "rework", runner, True, False
                )
                return
            if not event.data.get("in_contract", False):
                record = self._snapshot_record(snapshot)
                auto_paths = self._auto_scope_paths()
                resolution = resolve_consistency(
                    event.data.get("consistency"),
                    self.plan.decisions,
                    self.nodes[node_id].contract,
                    list(record.allowed_actions),
                    list(record.file_scope),
                    self._consistency_binding(record, self.nodes[node_id]),
                    node_files=self._node_scope_files(node_id, current),
                    auto_scope_paths=auto_paths,
                    occupied_files=self._files_held_by_other_active_nodes(
                        snapshot, node_id
                    ),
                    acceptance_rule_approved=self._acceptance_rule_approved(),
                )
                if resolution is not None:
                    if not self._stop_active_for_transition(
                        snapshot, node_id, role, handle_id, runner, "stopped"
                    ):
                        return
                    if resolution.scope_expanded_files:
                        scope_request = (
                            event.data.get("consistency", {})
                            .get("scope_expansion")
                        )
                        scope_reason = (
                            scope_request.get("reason", "")
                            if isinstance(scope_request, dict)
                            else ""
                        )
                        expansion = ScopeExpansionResolution(
                            list(resolution.scope_expanded_files),
                            list(resolution.scope_rules),
                            scope_reason,
                        )
                        self._apply_scope_expansion(
                            snapshot,
                            node_id,
                            expansion,
                            "ordinary_rework",
                            active=active,
                        )
                    current.setdefault("contract_overrides", {})[
                        resolution.field
                    ] = resolution.value
                    correction_values = {
                        "field": resolution.field,
                        "value": resolution.value,
                        "source": resolution.source,
                        "action": resolution.action,
                        "files": list(resolution.files),
                        "consistency_binding": dict(
                            resolution.consistency_binding
                        ),
                        "decision": (
                            dict(resolution.decision)
                            if resolution.decision is not None
                            else None
                        ),
                    }
                    correction = {
                        key: correction_values[key]
                        for key in CONSISTENCY_CORRECTION_KEYS
                    }
                    current.setdefault("corrections", []).append(correction)
                    self._record(
                        snapshot,
                        "consistency_corrected",
                        {
                            "run_id": snapshot.run_id,
                            "node_id": node_id,
                            **correction,
                        },
                    )
                    self._start_task(
                        snapshot, node_id, "developer", "rework", runner, True
                    )
                    return
                if not self._stop_active_for_transition(
                    snapshot,
                    node_id,
                    role,
                    handle_id,
                    runner,
                    "blocked_design",
                ):
                    return
                current["status"] = "blocked_design"
                current["blocked_design_cause"] = "out_of_contract_finding"
                current["reason"] = redact_provider_text(
                    event.data.get("finding", "out-of-contract finding")
                )
                current["old_task_reconciled"] = True
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
                self._record(
                    snapshot,
                    "blocked_design",
                    {
                        "run_id": snapshot.run_id,
                        "node_id": node_id,
                        "reason": current["reason"],
                        "old_task_reconciled": True,
                        "blocked_design_cause": "out_of_contract_finding",
                    },
                )
                self._ensure_blocked_design_decision(snapshot, node_id)
                return
            current["active_role"] = None
            current["active_task"] = None
            snapshot.handles.pop(node_id, None)
            self._start_task(snapshot, node_id, "developer", "rework", runner, True)
        elif event.event == "accepted":
            if role != "reviewer":
                self._mark_blocked_unknown(
                    snapshot, node_id, "acceptance did not come from the reviewer"
                )
                return
            try:
                registered = self._load_task_binding(snapshot, node_id, "reviewer")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "reviewer binding cannot be verified ({})".format(
                        type(error).__name__
                    ),
                    cause=error,
                )
                return
            if (
                registered.task_id != active["task_id"]
                or registered.generation != active["generation"]
            ):
                self._mark_blocked_unknown(
                    snapshot, node_id, "reviewer identity or generation is stale"
                )
                return
            # ISSUE-02 (dual-visible): recompute the live contract digest before
            # recording the acceptance, matching the guard already in
            # _accept_visible_sdd_delivery.  A drifted or unreadable contract
            # must not silently bind a stale snapshot digest.
            try:
                live_digest_matches = self._live_node_contract_digest_matches(
                    node_id, current.get("contract_digest")
                )
            except ValueError as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "dual-visible acceptance refused: node contract is unreadable ({})".format(
                        error
                    ),
                    cause=error,
                )
                return
            if not live_digest_matches:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "dual-visible acceptance refused: contract digest does not match the live node contract",
                )
                return
            integration_package = None
            if node_id == "integration-review":
                # Validate first (ISSUE-83): derive the run-level package
                # before the event is recorded, the binding is flipped, or
                # the handle is popped.  A malformed claim is a format error
                # the same reviewer can correct and re-report on its live
                # handle; bricking here destroyed every recovery path first.
                # The rejection rides with its structured code (#254) so the
                # redacted free text never loses the failure class.
                integration_package, rejection, recoverable, rejection_code = (
                    self._derive_integration_acceptance(snapshot, evidence)
                )
                if rejection is not None:
                    if recoverable:
                        self._reject_acceptance_format(
                            snapshot, node_id, active, rejection, rejection_code,
                            runner=runner, handle_id=handle_id,
                        )
                        return
                    self._mark_blocked_unknown(snapshot, node_id, rejection)
                    return
            # ISSUE-140: contract-declared invariant test commands gate the
            # acceptance.  Nodes without invariants return None immediately
            # and follow the legacy path unchanged.
            contract_test_failure = self._execute_contract_test_commands(
                snapshot, node_id, "acceptance", active
            )
            if contract_test_failure is not None:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "contract test execution failed: {}".format(contract_test_failure),
                )
                return
            # Validate first (ISSUE-249): an acceptance payload that carries
            # no registered P0-P2 clearance evidence is a format error the
            # same reviewer session can correct and re-report on its live
            # handle.  The legacy ordering recorded the event and flipped
            # the binding first and only then noticed the gap, leaving the
            # node accepted (binding + ``acceptance``) and blocked_unknown
            # (snapshot) at once -- a state every recovery gate is mutually
            # exclusive with.  Nothing below runs until the payload shape
            # is complete.
            if evidence is None:
                self._reject_acceptance_format(
                    snapshot,
                    node_id,
                    active,
                    REVERSE_BLOCK_REASON,
                    "acceptance_evidence:missing",
                    runner=runner,
                    handle_id=handle_id,
                )
                return
            acceptance_event = RunEvent(
                event.event,
                {
                    **event.data,
                    "contract_digest": current["contract_digest"],
                    "authorization_epoch": snapshot.authorization_digest,
                },
            )
            self._record_runner_event(snapshot, node_id, acceptance_event, active)
            try:
                self._set_binding_status(snapshot, node_id, role, "accepted")
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record acceptance ({})".format(
                        type(error).__name__
                    ),
                    cause=error,
                )
                return
            current["status"] = "accepted"
            current["acceptance"] = {
                "contract_digest": current["contract_digest"],
                "authorization_epoch": snapshot.authorization_digest,
            }
            # A corrected re-report must not keep the rejection text -- nor the
            # #255 consecutive-rejection counter the same session raised.
            current["reason"] = None
            current.pop("acceptance_format_rejections", None)
            current["active_role"] = None
            current["active_task"] = None
            current["quarantine"] = None
            snapshot.handles.pop(node_id, None)
            if node_id == "integration-review":
                # The acceptance of this node is the run-level closeout, so the
                # clearance is whatever the derived package says rather than an
                # assumed zero.  The package was already derived and validated
                # before any state changed; this write cannot fail validation.
                _record_integration_review(snapshot, integration_package)
            else:
                current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
            self._archive_pair(snapshot, node_id)
            self._release_node_lease(snapshot, node_id)
        elif event.event in {"unknown", "timeout", "state_unknown", "visibility_unknown"}:
            if event.event == "visibility_unknown" and self._archived_blocked_engineering(
                snapshot, node_id, role, active
            ):
                # #335: the runner's wait-event vocabulary does not know
                # blocked_engineering yet, so a blocked self-report surfaces
                # here as "provider event is unsupported" after the archive
                # was consumed.  The archive still holds the full payload:
                # park the node exactly as if the event name had flowed
                # through, instead of losing the report to a generic block.
                self._park_blocked_engineering(snapshot, node_id, role, event, active, handle_id)
                return
            raw_reason = event.data.get("reason", event.event)
            reason = str(redact_provider_text(raw_reason))
            failure = classify_provider_failure(event.data)
            # A durable native wait that is still pending is a same-task
            # retry.  It cannot prove completion, but it does not invalidate
            # the already registered handle or binding.
            # Integration review can also be waiting after a rejected claim
            # or a lost acceptance snapshot.  Keep its existing recovery gate.
            if (
                node_id == "integration-review"
                and event.event == "visibility_unknown"
                and str(raw_reason) == "provider wait is pending"
            ):
                failure = {"kind": "binding_unknown", "legacy_kind": "unknown"}
            if failure.get("legacy_kind") == "engineering":
                self._queue_active_retry(snapshot, node_id, raw_reason, observation=event.data)
                self._set_binding_status(snapshot, node_id, role, "blocked_unknown")
                current["status"] = self._v42_retry_status()
                current["reason"] = reason
                current["quarantine"] = None
            else:
                # Unknown evidence and external boundaries are fail-closed;
                # neither is allowed to consume an engineering retry budget.
                self._mark_blocked_unknown(
                    snapshot, node_id, reason,
                    quarantine_lease=False,
                    retryable_same_task=False,
                )
        elif event.event in {"failed", "stopped", "terminal_failed"}:
            failure = classify_provider_failure(event.data)
            if failure["kind"] == "external_decision":
                self._mark_external_authorization_required(
                    snapshot, node_id,
                    str(redact_provider_text(event.data.get("reason", event.event))),
                )
                return
            if (
                event.event == "stopped"
                and event.data.get("stop_origin") == "unexpected"
                and not current.get("unexpected_stop_retried")
            ):
                # Nobody at vibe asked for this stop (a manual ``claude stop``):
                # retry the same task once.  A second unexpected stop, or any
                # stop without an origin record, keeps the failed outcome.
                current["unexpected_stop_retried"] = True
                raw_reason = event.data.get("reason", event.event)
                self._queue_active_retry(snapshot, node_id, raw_reason, observation=event.data)
                if isinstance(current.get("retryable_action"), dict):
                    self._set_binding_status(snapshot, node_id, role, "blocked_unknown")
                    current["status"] = self._v42_retry_status()
                    current["reason"] = str(redact_provider_text(raw_reason))
                    current["quarantine"] = None
                    # The stopped session is dead: polling the old handle
                    # would only report ``stopped`` again.  Retire it so the
                    # retry scheduler dispatches a fresh session.
                    stale = current.setdefault("stale_handles", [])
                    if handle_id not in stale:
                        stale.append(handle_id)
                    snapshot.handles.pop(node_id, None)
                    return
            if failure.get("legacy_kind") == "engineering" and event.event != "stopped":
                raw_reason = event.data.get("reason", event.event)
                reason = str(redact_provider_text(raw_reason))
                self._queue_active_retry(snapshot, node_id, raw_reason, observation=event.data)
                self._set_binding_status(snapshot, node_id, role, "blocked_unknown")
                current["status"] = self._v42_retry_status()
                current["reason"] = reason
                current["quarantine"] = None
                return
            self._record_runner_event(snapshot, node_id, event, active)
            terminal_status = "failed" if event.event != "stopped" else "stopped"
            try:
                self._set_binding_status(snapshot, node_id, role, terminal_status)
            except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                self._mark_blocked_unknown(
                    snapshot,
                    node_id,
                    "task binding cannot record terminal state ({})".format(
                        type(error).__name__
                    ),
                    cause=error,
                )
                return
            current["status"] = terminal_status
            current["reason"] = redact_provider_text(
                event.data.get("reason", event.event)
            )
            current["active_role"] = None
            current["active_task"] = None
            current["quarantine"] = None
            snapshot.handles.pop(node_id, None)
            self._release_node_lease(snapshot, node_id)
        elif event.event == WORKER_QUESTION_EVENT:
            park_worker_question(self, snapshot, node_id, event, active, runner, handle_id)
        else:
            self._mark_blocked_unknown(
                snapshot, node_id, "unrecognized runner event: " + event.event
            )

    def _stop_active_for_transition(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        role: str,
        handle_id: str,
        runner: Runner,
        terminal_status: str,
    ) -> bool:
        try:
            runner.stop(RunHandle(handle_id))
            self._set_binding_status(snapshot, node_id, role, terminal_status)
        except Exception as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "old task stop cannot be proven ({})".format(
                    type(error).__name__
                ),
            )
            return False
        snapshot.handles.pop(node_id, None)
        snapshot.nodes[node_id]["active_role"] = None
        snapshot.nodes[node_id]["active_task"] = None
        return True

    def _reconcile_completed_delivery_for_transition(
        self, snapshot: RunSnapshot, node_id: str, active: Dict[str, Any]
    ) -> bool:
        """Prove that an active handle already has a terminal delivery.

        This is deliberately narrow.  It only accepts a delivery that is
        already projected on the node, matches the active task binding, and
        has a real in-worktree artifact containing its completion marker.  A
        missing, stale, or malformed delivery falls back to the ordinary
        provider stop proof and therefore keeps reauthorization fail-closed.
        """
        role = active.get("role")
        if role not in {"developer", "reviewer"}:
            return False
        current = snapshot.nodes[node_id]
        delivery = current.get("delivery_evidence")
        if not isinstance(delivery, dict):
            return False
        marker = delivery.get("completion_marker")
        delivery_path = delivery.get("delivery_path")
        if not isinstance(marker, str) or not marker.strip():
            return False
        if not isinstance(delivery_path, str) or not delivery_path.strip():
            return False
        try:
            binding = self._load_task_binding(snapshot, node_id, role)
        except (FileNotFoundError, OSError, TypeError, ValueError):
            return False
        generation_key = (
            "review_generation" if role == "reviewer" else "developer_generation"
        )
        if (
            binding.task_id != active.get("task_id")
            or binding.task_id != current.get(role + "_identity")
            or binding.generation != active.get("generation")
            or binding.generation != current.get(generation_key)
        ):
            return False
        if (
            delivery.get("thread_status") not in {"complete", "completed", "DELIVERED"}
            or binding.worktree != current.get("worktree")
            or binding.branch != current.get("branch")
        ):
            return False
        artifact = Path(delivery_path)
        root = self._worktree_path(current).resolve()
        try:
            artifact = artifact if artifact.is_absolute() else root / artifact
            artifact = artifact.resolve()
            if not artifact.is_relative_to(root) or not artifact.is_file():
                return False
            if marker.encode("utf-8") not in artifact.read_bytes():
                return False
        except (OSError, ValueError, UnicodeError):
            return False
        return True

    def _defer_structural_pending_items(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        event: RunEvent,
        current: Dict[str, Any],
    ) -> None:
        """Dispose structural pending-verification items without pausing.

        Items only real devices, production or the acceptance period can verify
        get a ``deferred_to_acceptance`` disposition (one audit event) and join
        ``acceptance_pending_verification`` for the delivery/acceptance report.
        An item already disposed, even in other words, is not disposed again.
        Items that cannot be judged structural are left to the normal gate.
        """
        review = event.data.get("in_session_review")
        items = review.get("blocked_unknowns") if isinstance(review, dict) else None
        if not isinstance(items, list):
            return
        dispositions = current.setdefault("blocked_unknown_dispositions", [])
        for item in items:
            if not is_structural_pending_verification(item):
                continue
            if any(
                entry.get("disposition") in BLOCKED_UNKNOWN_CLEARING_DISPOSITIONS
                and (entry.get("item") == item
                     or same_pending_verification(item, entry.get("item")))
                for entry in dispositions
            ):
                continue
            note = "结构性待验项：只能在真机、生产环境或验收期验证，随交付进入验收汇报"
            dispositions.append(
                {"item": item, "disposition": "deferred_to_acceptance", "note": note}
            )
            current.setdefault("acceptance_pending_verification", []).append(
                {"item": item, "note": note}
            )
            current["blocked_unknowns_pending"] = [
                pending for pending in current.get("blocked_unknowns_pending") or []
                if pending != item
            ]
            self._record(
                snapshot,
                "blocked_unknown_disposition",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "disposition": "deferred_to_acceptance",
                    "proof": {"item": item, "note": note},
                },
            )

    @staticmethod
    def _undisposed_blocked_unknowns(
        current: Dict[str, Any], review: Any
    ) -> List[str]:
        """Blocked_unknown items still blocking acceptance.

        Two closure rules keep the escape hatch shut in both directions:

        * An item that already paused a delivery (``blocked_unknowns_pending``)
          stays blocking until ANY disposition answers it -- silently dropping
          it from a later payload is not closure.  ``escalated_to_findings``
          answers it too: the unknown became a tracked finding, so the
          reworked re-delivery may legitimately stop carrying the item.
        * An item the CURRENT payload still carries needs a clearing
          disposition (``confirmed_benign`` / ``resolved_with_facts``):
          acceptance never proceeds while the reviewer still reports the
          item as unknown, even if it was escalated before.
        """
        pending = [
            item
            for item in current.get("blocked_unknowns_pending") or []
            if isinstance(item, str)
        ]
        payload_items: List[str] = []
        if isinstance(review, dict):
            payload_items = [
                item
                for item in review.get("blocked_unknowns") or []
                if isinstance(item, str)
            ]
        answered = set()
        cleared = set()
        for entry in current.get("blocked_unknown_dispositions") or []:
            if not isinstance(entry, dict):
                continue
            answered.add(entry.get("item"))
            if entry.get("disposition") in BLOCKED_UNKNOWN_CLEARING_DISPOSITIONS:
                cleared.add(entry.get("item"))
        undisposed = [item for item in pending if item not in answered]
        for item in payload_items:
            # A structural item re-reported in other words keeps the original
            # clearing disposition instead of being disposed a second time.
            if item in cleared or any(
                same_pending_verification(item, done) for done in cleared
            ):
                continue
            if item not in undisposed:
                undisposed.append(item)
        return undisposed

    def _pause_delivery_for_blocked_unknowns(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        event: RunEvent,
        current: Dict[str, Any],
        undisposed: List[str],
    ) -> None:
        """Pause acceptance until every blocked_unknown item is disposed.

        A non-empty ``blocked_unknowns`` array is not a format error and is
        never folded into the P0-P2 clearance counts -- that would turn
        "cannot confirm" into a silent escape hatch.  The delivery stays
        unrecorded and the worker-session handle, binding and lease stay
        untouched, so the same session re-reports once dispositions close
        every item; the node waits in ``blocked_unknown`` until then.
        """
        review = event.data.get("in_session_review")
        items = review.get("blocked_unknowns") if isinstance(review, dict) else []
        current["status"] = "blocked_unknown"
        current["reason"] = (
            "acceptance paused: blocked_unknown items await disposition"
        )
        current["blocked_unknowns_pending"] = list(undisposed)
        # The recovery entry is the disposition recorder itself: every pending
        # item is answered through Monitor.record_blocked_unknown_disposition
        # (#153).  Naming it keeps this blocked_unknown on the same "every
        # reason names a documented action" contract as the other causes.
        current["recovery_action"] = "record_blocked_unknown_disposition"
        active = current.get("active_task")
        self._record(
            snapshot,
            "blocked_unknown_disposition_required",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                # The durable event schema only preserves structured values
                # under pass-through keys (see _record_contract_test_execution);
                # the item lists ride under ``proof``.
                "proof": {
                    "blocked_unknowns": list(items or []),
                    "undisposed": list(undisposed),
                },
            },
            active if isinstance(active, dict) else None,
        )

    def _validate_visible_sdd_review(self, snapshot: RunSnapshot, node_id: str, event: RunEvent, current: Dict[str, Any]):
        return monitor_acceptance._validate_visible_sdd_review(self, snapshot, node_id, event, current)

    @staticmethod
    def _validate_visible_sdd_coverage(coverage: Any) -> Optional[str]:
        return monitor_acceptance._validate_visible_sdd_coverage(coverage)

    def _accept_visible_sdd_delivery(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        event: RunEvent,
        current: Dict[str, Any],
    ) -> None:
        """Accept a visible-sdd node from its in-session review evidence.

        The single worker session carries the whole SDD loop (implement,
        independent read-only review, rework, re-review); the delivery event
        must therefore cite the in-session review clearance.  The live
        delivery path validates this evidence before any state changes
        (ISSUE-83) and only reaches this function with a payload that passed;
        the fail-closed checks are kept here so a direct or replay-adjacent
        caller can never write an acceptance the validator would refuse.
        """
        review = event.data.get("in_session_review")
        evidence_ref = review.get("evidence_ref") if isinstance(review, dict) else None
        failure = self._validate_visible_sdd_review(snapshot, node_id, event, current)
        if failure is not None:
            reason, _recoverable, _code = failure
            self._mark_blocked_unknown(snapshot, node_id, reason)
            return
        if self._undisposed_blocked_unknowns(
            current, event.data.get("in_session_review")
        ):
            # Fail-closed twin of the _apply_event pause: a direct or
            # replay-adjacent caller never writes an acceptance over
            # undisposed blocked_unknown items either.
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "visible-sdd acceptance refused: blocked_unknown items await disposition",
            )
            return
        try:
            acceptance = VisibleSddAcceptance(
                contract_digest=current.get("contract_digest"),
                protocol_ref=VISIBLE_SDD_PROTOCOL_REF,
                evidence_ref=evidence_ref.strip(),
            )
        except (AttributeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "visible-sdd acceptance refused: {}".format(error),
            )
            return
        # ISSUE-140: invariant test commands gate the acceptance here too,
        # before the binding transition and the durable accepted event.
        contract_test_failure = self._execute_contract_test_commands(
            snapshot, node_id, "acceptance"
        )
        if contract_test_failure is not None:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "contract test execution failed: {}".format(contract_test_failure),
            )
            return
        # The in-session reviewer acts under the same single session
        # identity (ISSUE-01/05 semantics): the acceptance provenance names
        # the reviewer role of that session, and the review proof travels in
        # the event payload (evidence keeps the durable, replay-checked
        # clearance reference; topology/protocol/clearance annotate it).
        review_identity = current.get("developer_identity")
        review_generation = int(current.get("developer_generation", 0))
        try:
            # The binding transition lands before the durable accepted event:
            # a failure here must not leave an acceptance the registry never
            # confirmed.
            self._set_binding_status(snapshot, node_id, "developer", "accepted")
        except (FileNotFoundError, OSError, TypeError, ValueError) as error:
            self._mark_blocked_unknown(
                snapshot,
                node_id,
                "task binding cannot record acceptance ({})".format(
                    type(error).__name__
                ),
            )
            return
        current["reviewer_identity"] = review_identity
        current["review_generation"] = review_generation
        self._record(
            snapshot,
            "accepted",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "evidence": evidence_ref.strip(),
                "topology": TOPOLOGY_VISIBLE_SDD,
                "clearance": {"p0": 0, "p1": 0, "p2": 0},
                **acceptance.to_dict(),
                "authorization_epoch": snapshot.authorization_digest,
            },
            {
                "role": "reviewer",
                "task_id": review_identity,
                "handle_id": None,
                "generation": review_generation,
            },
        )
        current["status"] = "accepted"
        current["acceptance"] = {
            "contract_digest": current["contract_digest"],
            "authorization_epoch": snapshot.authorization_digest,
        }
        # A corrected re-report must not keep the rejection text -- nor the
        # #255 consecutive-rejection counter the same session raised.
        current["reason"] = None
        current.pop("acceptance_format_rejections", None)
        current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
        current["blocked_unknowns_pending"] = []
        current["quarantine"] = None
        self._archive_pair(snapshot, node_id)
        self._release_node_lease(snapshot, node_id)

    def _archive_pair(self, snapshot: RunSnapshot, node_id: str) -> None:
        """Archive both bindings of a closed node and record the audit event.

        ``_set_binding_status`` mutates the shared/cached ``TaskBinding``
        object in place, so this call destroys the pre-archive status of both
        bindings.  A caller that may need to roll the pair back must capture
        ``BindingFacts`` (or the plain status strings) *before* calling here;
        reading ``.status`` afterwards only ever returns ``archived`` (#256 ①).
        Callers without a rollback rely on the durable ``accepted`` event and
        its replay instead; see ``RECOVERY_FIDELITY_AUDIT``.
        """
        current = snapshot.nodes[node_id]
        roles = ("developer", "reviewer")
        if is_integration_review_node(self.nodes[node_id]):
            # New closeout runs have only a reviewer; legacy runs may also
            # carry a developer binding, which must still be archived.
            roles = (("developer", "reviewer") if current.get("developer_identity")
                     else ("reviewer",))
        elif self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
            # A visible-sdd node holds exactly one worker-session binding.
            roles = ("developer",)
        for role in roles:
            self._set_binding_status(snapshot, node_id, role, "archived")
        current["pair_archived"] = True
        self._record(
            snapshot,
            "pair_archived",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "clearance": current["review_clearance"],
            },
        )

    @staticmethod
    def _event_claims_match(event: RunEvent, node_id: str, active: Dict[str, Any]) -> bool:
        return monitor_acceptance._event_claims_match(event, node_id, active)

    @staticmethod
    def _review_finding_severity(data: Dict[str, Any]):
        return monitor_acceptance._review_finding_severity(data)

    @classmethod
    def _review_finding_disposition(cls, data: Dict[str, Any]) -> str:
        return monitor_acceptance._review_finding_disposition(cls, data)

    @staticmethod
    def _reviewer_write_violation(data: Dict[str, Any]) -> bool:
        return monitor_acceptance._reviewer_write_violation(data)

    @staticmethod
    def _finding_changes_prd_acceptance(data: Dict[str, Any]) -> bool:
        return monitor_acceptance._finding_changes_prd_acceptance(data)

    def _record_runner_event(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        event: RunEvent,
        active: Dict[str, Any],
    ) -> None:
        data = dict(event.data)
        data.update({"run_id": snapshot.run_id, "node_id": node_id})
        self._record(snapshot, event.event, data, active)

    def _queue_active_retry(
        self, snapshot: RunSnapshot, node_id: str, reason: str, *, record_event: bool = True,
        observation: Optional[Dict[str, Any]] = None
    ) -> None:
        """Record a retry intent without allocating a new task identity.

        A transient provider result is still unknown, but an active handle is
        durable evidence that the same task may be polled again.  Persist the
        logical retry alongside that handle so snapshot recovery can resume
        the original task even when the next poll happens in a new process.
        """
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        handle_id = snapshot.handles.get(node_id)
        if not isinstance(active, dict) or not isinstance(handle_id, str):
            return
        role = active.get("role")
        task_id = active.get("task_id")
        generation = active.get("generation")
        if role not in {"developer", "reviewer"} or not isinstance(task_id, str):
            return
        classification = classify_provider_failure(observation if observation is not None else {"reason": reason})
        if classification["kind"] == "external_decision":
            current["retryable_action"] = None
            return
        phase = "review" if role == "reviewer" else (
            "rework" if current.get("status") == "rework" else "develop"
        )
        previous = current.get("retryable_action")
        attempt = 1
        if isinstance(previous, dict) and previous.get("same_task") is True:
            try:
                attempt = int(previous.get("attempt", 0)) + 1
            except (TypeError, ValueError):
                attempt = 1
        observation_ref = digests.sha256_bytes(str(reason).encode("utf-8"))
        cursor = None
        try:
            binding = self._load_task_binding(snapshot, node_id, str(role))
            cursor = binding.cursor
        except (FileNotFoundError, OSError, TypeError, ValueError):
            pass
        retry = {
            "role": role,
            "phase": phase,
            "continuation": True,
            "successor": False,
            "successor_candidate": False,
            "same_task": True,
            "same_task_required": True,
            "task_id": task_id,
            "handle_id": handle_id,
            "generation": generation,
            "cursor": cursor,
            "attempt": attempt,
            "reason_class": classification["kind"],
            "next_retry_at": time.time() + min(60.0, float(2 ** min(attempt - 1, 5))),
            "binding_digest": str(current.get("contract_digest") or snapshot.node_contract_digest or ""),
            "last_observation_ref": observation_ref,
            "reason": redact_provider_text(reason),
        }
        current["retryable_action"] = retry
        if not record_event:
            retry["defer_once"] = True
        if record_event:
            self._record(
                snapshot,
                "retry_scheduled",
                {
                    "run_id": snapshot.run_id,
                    "node_id": node_id,
                    "role": role,
                    "task_id": task_id,
                    "generation": generation,
                    "cursor": cursor,
                    "attempt": attempt,
                    "reason_class": classification["kind"],
                    "next_retry_at": retry["next_retry_at"],
                    "same_task_required": True,
                    "binding_digest": retry["binding_digest"],
                    "last_observation_ref": observation_ref,
                    "reason": reason,
                },
                active,
            )

    def _recovery_snapshot(self, node_id: str, run_id: Optional[str] = None) -> RunSnapshot:
        """Load the uniquely bound durable run snapshot for a node."""
        runs = self.paths.vibe / "runs"
        if run_id:
            return load_snapshot(self.paths, run_id)
        candidates = []
        for path in runs.glob("*/state.json"):
            try:
                snapshot = load_snapshot(self.paths, path.parent.name)
                if node_id in snapshot.nodes:
                    candidates.append(snapshot)
            except (OSError, TypeError, ValueError):
                continue
        if len(candidates) != 1:
            raise ValueError("binding_unknown: run id is required for ambiguous node binding")
        return candidates[0]

    def schedule_retry(self, node_id: str, reason: str, next_retry_at: Optional[float] = None, run_id: Optional[str] = None) -> None:
        """Persist same-task retry state with exponential backoff."""
        snapshot = self._recovery_snapshot(node_id, run_id)
        current = snapshot.nodes[node_id]
        classification = classify_provider_failure({"reason": reason})
        if classification["kind"] == "external_decision":
            current["status"] = "blocked_design"
            current["reason"] = redact_provider_text(reason)
            current["reason_class"] = "external_decision"
            current["retryable_action"] = None
            current["recovery_action"] = "await_external_decision"
            current["owner_layer"] = "external_authorization"
            current["user_interrupt"] = True
        else:
            previous = current.get("retryable_action") if isinstance(current.get("retryable_action"), dict) else {}
            attempt = int(previous.get("attempt", 0) or 0) + 1
            delay = min(60.0, float(2 ** min(attempt - 1, 5)))
            retry = dict(previous)
            retry.update({"same_task": True, "same_task_required": True, "successor": False,
                          "attempt": attempt, "reason_class": classification["kind"],
                          "reason": redact_provider_text(reason),
                          "next_retry_at": float(next_retry_at if next_retry_at is not None else time.time() + delay)})
            # A supervisor requeue supersedes an in-flight fallback sequence:
            # rewind the chain so the next dispatch starts from the resolved
            # primary again.  (The role/phase defaults also make a requeue of
            # a parked node -- which has no previous retry dict -- replayable;
            # without them the sweep crashes on a missing ``role``.)
            self._clear_model_fallback_state(current)
            retry.setdefault(
                "role",
                str(current.get("active_role") or "developer"),
            )
            retry.setdefault(
                "phase",
                "review" if retry.get("role") == "reviewer" else "develop",
            )
            current["retryable_action"] = retry
            current["status"] = "retry_pending" if classification["kind"] in {"retryable", "capacity_wait", "repairable"} else "blocked_unknown"
            current["reason"] = redact_provider_text(reason)
            current["reason_class"] = str(classification["kind"])
        self._record(snapshot, "retry_scheduled", {"run_id": snapshot.run_id, "node_id": node_id,
            "reason_class": classification["kind"], "reason": reason,
            "same_task_required": True, "next_retry_at": current.get("retryable_action", {}).get("next_retry_at") if isinstance(current.get("retryable_action"), dict) else None})
        self._save_snapshot(snapshot)

    def _park_review_loop(
        self, snapshot, node_id, current, active, role, handle_id, runner, review_round
    ) -> None:
        # Three unconverged rework rounds: the fix is a different
        # reviewer (or a split node), never another automatic
        # rework round and never a PM question (#153).  Park the
        # node with the documented action; executing it
        # (``vibe repair``) dispatches a successor reviewer.
        if not self._stop_active_for_transition(
            snapshot, node_id, role, handle_id, runner, "stopped"
        ):
            return
        current["status"] = "blocked_unknown"
        current["reason"] = (
            "review loop limit reached; change reviewer or split node required"
        )
        current["recovery_action"] = "change_reviewer_or_split_node"
        current["reviewer_replacement_required"] = True
        current["reviewer_started"] = False
        # Keep the stopped identity as the successor predecessor so
        # the replacement review cannot accidentally reuse it.
        current["reviewer_identity"] = active.get("task_id")
        current["active_role"] = None
        current["active_task"] = None
        snapshot.handles.pop(node_id, None)
        self._release_node_lease(snapshot, node_id)
        self._record(
            snapshot,
            "review_loop_limit_reached",
            {
                "run_id": snapshot.run_id,
                "node_id": node_id,
                "reason": current["reason"],
                "recovery_action": "change_reviewer_or_split_node",
                # The durable schema keeps structured values only
                # under pass-through keys, so the replay branch
                # restores the park from ``proof`` -- top-level
                # review_round / reviewer_replacement_required
                # would be dropped by _sanitize_event_data, and a
                # top-level ``reason`` would be redacted as
                # provider text even though it is a vibe-authored
                # fixed string.
                "proof": {
                    "park_reason": current["reason"],
                    "review_loop_rounds": review_round,
                    "reviewer_replacement_required": True,
                    "reviewer_identity": current.get("reviewer_identity"),
                },
            },
            active,
        )

    @staticmethod
    def _acceptance_format_rejection_count(current: Dict[str, Any]) -> int:
        record = current.get("acceptance_format_rejections")
        count = record.get("count") if isinstance(record, dict) else 0
        return count if isinstance(count, int) and not isinstance(count, bool) else 0

    def replace_ended_reviewer(self, node_id: str, run_id: str) -> Dict[str, Any]:
        """Replace a rejected reviewer whose session is gone (daemon self-heal).

        The caller has already shown the session is retired.  This does what a
        manual ``vibe repair`` does for a reviewer that must be replaced: retire
        the stopped identity, set ``reviewer_replacement_required`` and let
        ``repair_node`` queue the successor.  At most once per node: a second
        rejection is left to the product-manager escalation.  Reads no value
        the reviewer wrote.
        """
        self._reset_binding_cache()
        snapshot = self._recovery_snapshot(node_id, run_id)
        current = snapshot.nodes[node_id]
        count = self._acceptance_format_rejection_count(current)
        if (
            current.get("status") != "review"
            or current.get("reviewer_auto_replaced") is True
            or current.get("reviewer_replacement_required")
            or not 0 < count < ACCEPTANCE_FORMAT_REJECTION_LIMIT
        ):
            raise ValueError("reviewer replacement preconditions are not met")
        binding = self._set_binding_status(snapshot, node_id, "reviewer", "failed")
        release_writer_lease(
            self.paths, node_id, str(current.get("worktree", "")), snapshot.run_id
        )
        snapshot.handles.pop(node_id, None)
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = None
        current["reviewer_identity"] = binding.task_id
        current["reviewer_started"] = False
        current["reviewer_replacement_required"] = True
        current["reviewer_auto_replaced"] = True
        current["recovery_action"] = "change_reviewer_or_split_node"
        current["reason"] = "reviewer session ended after a rejected acceptance; replacement dispatched"
        self._save_snapshot(snapshot)
        return self.repair_node(node_id, snapshot.run_id)

    def repair_node(self, node_id: str, run_id: Optional[str] = None) -> Dict[str, Any]:
        """Repair a node in place; identity fields are immutable across repair."""
        self._reset_binding_cache()
        snapshot = self._recovery_snapshot(node_id, run_id)
        current = snapshot.nodes[node_id]
        before = {key: current.get(key) for key in ("task_id", "generation", "writer", "lease", "cursor", "intent_digest", "contract_digest")}
        reason = str(current.get("reason") or "repair requested")
        if (
            current.get("recovery_action")
            in {"change_reviewer_or_split_node", ACCEPTANCE_FORMAT_ESCALATION_RECOVERY}
            and current.get("reviewer_replacement_required")
        ):
            # A three-round unconverged review loop (#153) -- or an acceptance
            # whose format was rejected past the escalation limit (#255) -- is
            # repaired by replacing the reviewer identity, not by re-polling
            # the stopped task.  Every identity field stays untouched (so
            # ``preserved_identity`` remains true); only the live handle is
            # retired and a retry marker queues the successor review for the
            # scheduler, which dispatches it with a fresh reviewer task id.
            current["status"] = "rework"
            current["reviewer_started"] = False
            # Retire the pair slot so the scheduler's ``pending_schedule``
            # retry can dispatch the successor review inside the concurrency
            # budget; the dispatch itself re-activates the slot.
            current["pair_archived"] = True
            current["retryable_action"] = {
                "action": "change_reviewer_or_split_node",
                "role": "reviewer",
                "phase": "review",
                "continuation": False,
                "pending_schedule": True,
                "successor": True,
                "same_task": False,
                "same_task_required": False,
                "predecessor_task_id": current.get("reviewer_identity"),
                "reason": reason,
            }
            after = {key: current.get(key) for key in before}
            preserved = before == after
            reconciliation = {
                "git": {"observed": False},
                "provider": {"observed": False, "status": "unavailable"},
            }
            self._record(snapshot, "node_repaired", {"run_id": snapshot.run_id, "node_id": node_id,
                "kind": "reviewer_replacement", "preserved_identity": preserved,
                "repair_action": "change_reviewer_or_split_node", "same_task": False,
                "reconciliation": reconciliation,
                "task_id": current.get("task_id"), "generation": current.get("generation")})
            self._save_snapshot(snapshot)
            return {"node_id": node_id, "kind": "reviewer_replacement", "preserved_identity": preserved,
                    "status": current.get("status"), "task_id": current.get("task_id"),
                    "generation": current.get("generation"),
                    "repair_action": "change_reviewer_or_split_node",
                    "recovery_action": current.get("recovery_action"),
                    "run_id": snapshot.run_id, "reconciliation": reconciliation}
        # Stuck-session repair (#278): when the node still carries the marks of
        # a live session (active task, run handle, or a quarantined lease) but
        # the task binding is already terminal -- the session was stopped
        # externally or a previous recovery retired it -- a same-task retry can
        # never work: the identity is dead.  repair_node then folds the whole
        # manual checklist into one call: mark the binding failed, release the
        # writer lease, retire the dead handle, clear the quarantine and queue
        # a successor for the next ``vibe resume``.  Every piece tolerates
        # absence so a partially cleaned state still converges instead of
        # reporting a false success.  A binding that is still live means the
        # session may be working; it falls through to the same-task path below
        # rather than being parked by a repair.
        role = str(
            current.get("active_role")
            or (current.get("active_task") or {}).get("role")
            or "developer"
        )
        binding = None
        binding_unreadable = False
        try:
            # Deliberately bypass ``_binding_cache``: the session may have been
            # stopped by hand (or by a previous supervisor in another process)
            # after the in-memory binding was cached, and a stale live status
            # would skip the very cleanup this path exists for.
            binding = load_task_binding(
                self.paths, node_id, role, run_id=snapshot.run_id
            )
        except FileNotFoundError:
            binding = None
        except (OSError, TypeError, ValueError):
            # The registry exists but cannot be read (corrupt JSON, schema
            # drift, recursive ancestry).  "Unreadable" is not "dead": the
            # binding may still describe a live writer, so cleanup and any
            # successor would risk two writers.  Fail closed by parking the
            # node for reconcile_binding without touching lease, handle or
            # quarantine.
            binding_unreadable = True
        stuck_marks = (
            isinstance(current.get("active_task"), dict)
            or node_id in snapshot.handles
            or isinstance(current.get("quarantine"), dict)
        )
        if stuck_marks and binding_unreadable:
            current["status"] = "blocked_unknown"
            current["recovery_action"] = "reconcile_binding"
            current["reason"] = redact_provider_text(reason)
            current["retryable_action"] = {
                "action": "reconcile_binding",
                "manual_repair_required": True,
                "same_task": False,
                "same_task_required": True,
                "successor": False,
                "reason": (
                    "stuck-session repair could not read the task binding registry"
                ),
            }
            reconciliation = {
                "git": {"observed": False},
                "provider": {"observed": False, "status": "unavailable"},
            }
            self._record(snapshot, "node_repaired", {"run_id": snapshot.run_id,
                "node_id": node_id,
                "kind": "stuck_session", "preserved_identity": False,
                "repair_action": "stuck_session_cleanup", "same_task": False,
                "successor": False, "role": role,
                "reconciliation": reconciliation,
                "task_id": None,
                "generation": current.get(
                    "review_generation" if role == "reviewer" else "developer_generation"
                )})
            self._save_snapshot(snapshot)
            return {"node_id": node_id, "kind": "stuck_session",
                    "preserved_identity": False,
                    "status": current["status"],
                    "task_id": None,
                    "generation": current.get(
                        "review_generation" if role == "reviewer" else "developer_generation"
                    ),
                    "repair_action": "stuck_session_cleanup",
                    "recovery_action": current["recovery_action"],
                    "steps": {
                        "binding_failed": False,
                        "lease_released": False,
                        "handle_retired": False,
                        "quarantine_cleared": False,
                    },
                    "run_id": snapshot.run_id,
                    "reconciliation": reconciliation}
        # A parked or missing registry entry cannot prove provider termination.
        # Only registry terminal states prove that another task may replace
        # this binding. Unknown/blocked/delivered are not termination evidence.
        active = current.get("active_task")
        generation_key = "review_generation" if role == "reviewer" else "developer_generation"
        binding_matches = binding is not None and (
            binding.task_id == current.get(role + "_identity")
            and binding.generation == current.get(generation_key)
            and (not isinstance(active, dict) or (
                binding.task_id == active.get("task_id")
                and binding.generation == active.get("generation")
                and role == active.get("role")
                and snapshot.handles.get(node_id) == active.get("handle_id")
            ))
        )
        binding_dead = binding_matches and binding.status in {
            "stopped", "failed", "archived",
        }
        if (
            not binding_dead and binding_matches and role == "reviewer"
            and binding.status in {"running", "review"}
            and 0 < self._acceptance_format_rejection_count(current)
            < ACCEPTANCE_FORMAT_REJECTION_LIMIT
            and worker_liveness.session_ended(
                self.paths, snapshot.run_id, node_id, current
            )
        ):
            # D4: a reviewer that delivered a malformed report and then exited
            # leaves its binding reading "review".  Below the escalation limit
            # that is a dead identity, not an unknown one: replace the
            # reviewer instead of parking the node for reconcile_binding.
            binding_dead = True
        retry = current.get("retryable_action")
        delivered_continuation = (
            binding_matches and binding.status == "delivered" and role == "developer"
            and self._node_dispatch_topology(self.nodes[node_id]) != TOPOLOGY_VISIBLE_SDD
            and current.get("active_task") is None
            and current.get("active_role") is None
            and current.get("start_intent") is None
            and node_id not in snapshot.handles
            and current.get("reviewer_started") is not True
            and current.get("pair_archived") is True
            and isinstance(retry, dict)
            and retry.get("role") == "developer" and retry.get("phase") == "rework"
            and retry.get("continuation") is True
            and retry.get("pending_schedule") is True
            and retry.get("successor") is not True
            and retry.get("successor_candidate") is not True
            and not retry.get("manual_repair_required")
        )
        if delivered_continuation or (
            stuck_marks and binding_matches and binding.status == "delivered"
            and self._recover_quarantined_delivered_developer(snapshot, node_id)
        ):
            self._save_snapshot(snapshot)
            return {"node_id": node_id, "kind": "same_task_retry",
                    "preserved_identity": True, "status": current["status"],
                    "task_id": binding.task_id, "run_id": snapshot.run_id}
        if stuck_marks and (
            not binding_matches or (not binding_dead and binding.status not in {
                "created", "start_pending", "running", "review", "rework",
            })
        ):
            current["status"] = "blocked_unknown"
            current["recovery_action"] = "reconcile_binding"
            current["retryable_action"] = {
                "action": "reconcile_binding", "manual_repair_required": True,
                "same_task": True, "same_task_required": True,
                "successor": False,
                "reason": "old task termination is not proven",
            }
            self._record(snapshot, "node_repaired", {
                "run_id": snapshot.run_id, "node_id": node_id,
                "kind": "binding_unknown", "preserved_identity": True,
                "repair_action": "reconcile_binding", "same_task": True,
                "successor": False, "role": role,
                "task_id": binding.task_id if binding is not None else None,
            })
            self._save_snapshot(snapshot)
            return {"node_id": node_id, "kind": "binding_unknown",
                    "preserved_identity": True,
                    "status": current["status"],
                    "recovery_action": "reconcile_binding",
                    "run_id": snapshot.run_id}
        if stuck_marks and binding_dead:
            steps = {
                "binding_failed": False,
                "lease_released": False,
                "handle_retired": False,
                "quarantine_cleared": False,
            }
            predecessor = binding.task_id if binding is not None else None
            previous_binding_status = (
                binding.status if binding is not None else None
            )
            # Normalize only proven terminal bindings to ``failed``.  The
            # pre-repair status remains in the event below so this cleanup
            # does not erase history or promote unknown states to terminal.
            if binding is not None and binding.status != "failed":
                try:
                    binding = self._set_binding_status(
                        snapshot, node_id, role, "failed"
                    )
                except (FileNotFoundError, OSError, TypeError, ValueError):
                    # A binding we cannot retire cannot be proven dead; a
                    # successor beside it would risk two live writers.  Fail
                    # closed instead of queueing the dispatch.
                    current["status"] = "blocked_unknown"
                    current["recovery_action"] = "reconcile_binding"
                    current["retryable_action"] = {
                        "action": "reconcile_binding",
                        "manual_repair_required": True,
                        "same_task": False,
                        "same_task_required": True,
                        "successor": False,
                        "reason": "stuck-session repair could not retire the old binding",
                    }
                    self._record(snapshot, "node_repaired", {
                        "run_id": snapshot.run_id, "node_id": node_id,
                        "kind": "binding_unknown", "preserved_identity": True,
                        "repair_action": "reconcile_binding", "successor": False,
                        "predecessor_task_id": predecessor,
                        "previous_binding_status": previous_binding_status,
                    })
                    self._save_snapshot(snapshot)
                    return {"node_id": node_id, "kind": "binding_unknown",
                            "status": current["status"],
                            "recovery_action": "reconcile_binding",
                            "steps": steps, "run_id": snapshot.run_id}
            steps["binding_failed"] = (
                binding is not None and binding.status == "failed"
            )
            lease_released = release_writer_lease(
                self.paths,
                node_id,
                str(current.get("worktree", "")),
                snapshot.run_id,
            )
            observed_lease = read_writer_lease(
                self.paths, node_id, str(current.get("worktree", ""))
            )
            steps["lease_released"] = lease_released or observed_lease is None or not observed_lease.active
            retired_handle = snapshot.handles.pop(node_id, None)
            steps["handle_retired"] = (
                retired_handle is not None or node_id not in snapshot.handles
            )
            steps["quarantine_cleared"] = isinstance(
                current.get("quarantine"), dict
            )
            current["quarantine"] = None
            current["active_role"] = None
            current["active_task"] = None
            # Free the pair slot so the scheduler's pending_schedule retry can
            # dispatch the successor inside the concurrency budget.
            current["pair_archived"] = True
            if role == "reviewer":
                current["status"] = "rework"
                current["reviewer_started"] = False
            else:
                current["status"] = "retry_pending"
            current["reason"] = redact_provider_text(reason)
            current["recovery_action"] = "repair_node"
            current["retryable_action"] = {
                "role": role,
                "phase": "review" if role == "reviewer" else "develop",
                "continuation": False,
                "pending_schedule": True,
                "successor": True,
                "same_task": False,
                "same_task_required": False,
                "old_task_reconciled": True,
                "predecessor_task_id": predecessor,
                "reason": redact_provider_text(reason),
            }
            after = {key: current.get(key) for key in before}
            preserved = before == after
            reconciliation = {
                "git": {"observed": False},
                "provider": {"observed": False, "status": "unavailable"},
            }
            self._record(snapshot, "node_repaired", {"run_id": snapshot.run_id,
                "node_id": node_id,
                "kind": "stuck_session", "preserved_identity": preserved,
                "repair_action": "stuck_session_cleanup", "same_task": False,
                "successor": current["retryable_action"].get("successor") is True,
                "role": role,
                "phase": current["retryable_action"].get("phase"),
                "predecessor_task_id": predecessor,
                "previous_binding_status": previous_binding_status,
                "handle_id": retired_handle,
                "reconciliation": reconciliation,
                "task_id": predecessor,
                "generation": current.get(
                    "review_generation" if role == "reviewer" else "developer_generation"
                )})
            self._save_snapshot(snapshot)
            return {"node_id": node_id, "kind": "stuck_session",
                    "preserved_identity": preserved,
                    "status": current.get("status"),
                    "task_id": predecessor,
                    "generation": current.get(
                        "review_generation" if role == "reviewer" else "developer_generation"
                    ),
                    "repair_action": "stuck_session_cleanup",
                    "recovery_action": current.get("recovery_action"),
                    "steps": steps,
                    "run_id": snapshot.run_id,
                    "reconciliation": reconciliation}
        if isinstance(current.get("model_unavailable"), dict):
            # Model-fallback park: the supervisor asked for repair after the
            # chain exhausted every model.  The redacted persisted reason
            # would classify as binding_unknown and park the node forever;
            # instead clear the monitor-authored fallback state (and the
            # switched override profile, so a fixed ``model_map`` or tier
            # move takes effect) and requeue the same task.
            self._clear_model_fallback_state(current)
            current["status"] = self._v42_retry_status()
            current["recovery_action"] = "requeue_same_task"
            current["reason"] = "model fallback chain cleared by repair; requeueing"
            current["retryable_action"] = {
                "role": role,
                "phase": "review" if role == "reviewer" else "develop",
                "continuation": False,
                "successor": False,
                "same_task": True,
                "same_task_required": True,
                "attempt": 1,
                "reason_class": "model_unavailable",
                "next_retry_at": time.time(),
                "binding_digest": current.get("contract_digest", ""),
                "last_observation_ref": "model-fallback-repair",
            }
            classification = {"kind": "retryable", "legacy_kind": "engineering"}
            fallback_repair = True
        else:
            classification = classify_provider_failure({"reason": reason})
            fallback_repair = False
        if fallback_repair:
            pass  # the requeue above is the whole repair; never reclassify below
        elif classification["kind"] == "binding_unknown":
            current["status"] = "blocked_unknown"
            current["recovery_action"] = "reconcile_binding"
            retry = current.get("retryable_action")
            if not isinstance(retry, dict):
                current["retryable_action"] = {
                    "action": "reconcile_binding",
                    "manual_repair_required": True,
                    "same_task": False,
                    "same_task_required": True,
                    "successor": False,
                    "reason": reason,
                }
        elif classification["kind"] == "external_decision":
            current["status"] = "blocked_design"
            current["recovery_action"] = "await_external_decision"
            current["owner_layer"] = "external_authorization"
            current["user_interrupt"] = True
        else:
            current["status"] = "retry_pending"
            current["recovery_action"] = recovery_action_for_failure({"reason": reason})
            self.schedule_retry(node_id, reason, run_id=snapshot.run_id)
            snapshot = self._recovery_snapshot(node_id, snapshot.run_id)
            current = snapshot.nodes[node_id]
        after = {key: current.get(key) for key in before}
        preserved = before == after
        repair_action = "git/provider_reconcile" if classification["kind"] == "repairable" else "same_task_retry"
        reconciliation = {"git": {"observed": False}, "provider": {"observed": False, "status": "unavailable"}}
        if classification["kind"] == "repairable":
            worktree = current.get("worktree")
            if isinstance(worktree, str) and worktree:
                try:
                    reconciliation["git"] = {
                        "observed": True,
                        "worktree": worktree,
                        "head": subprocess.check_output(["git", "-C", worktree, "rev-parse", "HEAD"], text=True, stderr=subprocess.STDOUT, timeout=5).strip(),
                        "branch": subprocess.check_output(["git", "-C", worktree, "branch", "--show-current"], text=True, stderr=subprocess.STDOUT, timeout=5).strip(),
                        "dirty": bool(subprocess.check_output(["git", "-C", worktree, "status", "--porcelain"], text=True, stderr=subprocess.STDOUT, timeout=5).strip()),
                    }
                except (OSError, subprocess.SubprocessError) as error:
                    reconciliation["git"] = {"observed": False, "error": type(error).__name__}
        self._record(snapshot, "node_repaired", {"run_id": snapshot.run_id, "node_id": node_id,
            "kind": classification["kind"], "preserved_identity": preserved,
            "repair_action": repair_action, "same_task": True,
            "reconciliation": reconciliation,
            "task_id": current.get("task_id"), "generation": current.get("generation")})
        self._save_snapshot(snapshot)
        return {"node_id": node_id, "kind": classification["kind"], "preserved_identity": preserved,
                "status": current.get("status"), "task_id": current.get("task_id"),
                "generation": current.get("generation"), "repair_action": repair_action,
                "recovery_action": current.get("recovery_action"),
                "run_id": snapshot.run_id, "reconciliation": reconciliation}

    def _mark_blocked_unknown(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        reason: Any,
        quarantine_lease: bool = True,
        retryable_same_task: bool = False,
        extra_event_data: Optional[Dict[str, Any]] = None,
        reason_code: Optional[str] = None,
        cause: Optional[BaseException] = None,
    ) -> None:
        """Park a node as blocked_unknown and stamp its recovery markers.

        The stamps are structural (``recovery_action``, ``quarantine`` with
        the handle this block retires) while ``reason`` is provider text that
        persistence redacts.  Two consequences for anything that reads them
        back (#256):

        * never match on ``reason`` -- after a restart it is
          ``[REDACTED_PROVIDER_TEXT]``;
        * the ``recovery_action`` / ``quarantine`` pair is the block's own
          fingerprint, not proof of live work.  Rescue gates judge it with
          ``defect_adjacent_marker_allows_rescue``; see
          ``RECOVERY_FIDELITY_AUDIT`` for the call-site verdicts.

        #277: alongside the redacted free text the block now stamps the
        structured classification on the node and the event --
        ``reason_class`` (the classifier's fixed-enum kind),
        ``error_type`` (the exception class when the block came from one --
        pass the exception itself as ``reason`` or separately as ``cause``)
        and an optional ``reason_code``.  All three are program-authored
        enums/names, so persistence keeps them verbatim and ``vibe status``
        can name the failure class without the provider text.
        """
        if cause is None and isinstance(reason, BaseException):
            cause = reason
        reason_text = str(reason)
        error_type = type(cause).__name__ if cause is not None else None
        classification = classify_provider_failure(
            cause if cause is not None else {"reason": reason_text}
        )
        reason_class = str(classification["kind"])
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        handle_id = snapshot.handles.get(node_id)
        current["status"] = "blocked_unknown"
        current["reason"] = reason_text
        current["reason_class"] = reason_class
        if error_type is not None:
            current["error_type"] = error_type
        else:
            current.pop("error_type", None)
        if reason_code is not None:
            current["reason_code"] = reason_code
        else:
            current.pop("reason_code", None)
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": handle_id,
            "reason": reason_text,
        }
        action = recovery_action_for_failure(
            cause if cause is not None else {"reason": reason_text}
        )
        current["recovery_action"] = action
        if isinstance(active, dict) and active.get("role") in {"developer", "reviewer"}:
            try:
                self._set_binding_status(
                    snapshot, node_id, str(active["role"]), "blocked_unknown"
                )
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
        if quarantine_lease:
            quarantine_writer_lease(
                self.paths,
                node_id,
                str(current.get("worktree", "")),
                snapshot.run_id,
                reason_text,
            )
        event_data = {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "reason": reason_text,
            "reason_class": reason_class,
        }
        if error_type is not None:
            event_data["error_type"] = error_type
        if reason_code is not None:
            event_data["reason_code"] = reason_code
        if retryable_same_task:
            event_data["retryable_same_task"] = True
        if extra_event_data:
            event_data.update(extra_event_data)
        self._record(
            snapshot,
            "blocked_unknown",
            event_data,
            active if isinstance(active, dict) else None,
        )

    def _mark_external_authorization_required(
        self, snapshot: RunSnapshot, node_id: str, reason: str,
        active: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Park a node on a structured external authorization boundary.

        Unlike blocked_unknown this is a legitimate wait-for-user: the status
        maps to 需要你决定 and no engineering retry budget is consumed.
        """
        # Lease check first, outside any try: the binding write below sits in
        # an ``except ValueError`` that would swallow the guard's conflict
        # error, and the lease release would then run before ``_record``
        # stops a superseded supervisor (see _ensure_blocked_design_decision).
        guard = getattr(self, "_guard_lease", None)
        if guard is not None:
            guard(snapshot.run_id)
        current = snapshot.nodes[node_id]
        if active is None and isinstance(current.get("active_task"), dict):
            active = current["active_task"]
        if isinstance(active, dict) and active.get("role") in {"developer", "reviewer"}:
            try:
                self._set_binding_status(snapshot, node_id, str(active["role"]), "blocked_unknown")
            except (FileNotFoundError, OSError, TypeError, ValueError):
                pass
        redacted = redact_provider_text(reason)
        current["status"] = "blocked_design"
        current["reason"] = redacted
        # #277: external authorization is a fixed routing class -- keep the
        # structured class readable beside the redacted free text.
        current["reason_class"] = "external_decision"
        current["retryable_action"] = None
        current["recovery_action"] = "await_external_decision"
        current["owner_layer"] = "external_authorization"
        current["user_interrupt"] = True
        current["active_role"] = None
        current["active_task"] = None
        current["quarantine"] = None
        current["old_task_reconciled"] = True
        snapshot.handles.pop(node_id, None)
        self._release_node_lease(snapshot, node_id)
        self._record(snapshot, "blocked_design", {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "reason": redacted,
            "reason_class": "external_decision",
            "old_task_reconciled": True,
            "owner_layer": "external_authorization",
            "recovery_action": "await_external_decision",
        }, active if isinstance(active, dict) else None)

    def _ensure_blocked_design_decision(self, snapshot: RunSnapshot, node_id: str) -> bool:
        """Register the missing decision for a decision-less blocked_design.

        Text is vibe-authored and fixed per cause; provider finding text never
        reaches the mailbox.  Returns False (state untouched) when the mailbox
        write fails so the caller can fall back to the lifecycle state.
        """
        # The supervisor-lease node supplies ``_guard_lease`` (absent before it
        # merges).  It must run before the mailbox write and the push: those
        # side effects are not covered by the lease check in ``_record``, so a
        # superseded supervisor would otherwise leave a request and a
        # notification behind.  Its exception propagates on purpose.
        guard = getattr(self, "_guard_lease", None)
        if guard is not None:
            guard(snapshot.run_id)
        current = snapshot.nodes[node_id]
        cause = current.get("blocked_design_cause")
        spec = _BLOCKED_DESIGN_DECISIONS.get(cause)
        if spec is None:
            # Unknown cause: not a product decision; the lifecycle route owns it.
            return False
        evidence: Dict[str, Any] = {"cause": cause}
        if cause == "integration_scope_review":
            if isinstance(current.get("blocked_design_target"), str):
                evidence["target_node_id"] = current["blocked_design_target"]
            # Answering approves exactly these files, so the PM must see them.
            files = current.get("blocked_design_files")
            if isinstance(files, list):
                evidence["files"] = [item for item in files if isinstance(item, str)]
        elif cause == "out_of_contract_finding":
            evidence["role"] = "reviewer"
            if isinstance(current.get("review_generation"), int):
                evidence["review_generation"] = current["review_generation"]
        try:
            mailbox = ScopedDecisionMailbox(self.paths)
            for attempt in range(1, _BLOCKED_DESIGN_MAX_ATTEMPTS + 1):
                # An answered request is closed: a new block for the same cause
                # needs a new question text (hence a new id) to reach the PM.
                question = spec["question"] if attempt == 1 else "{}（第 {} 次）".format(spec["question"], attempt)
                # Mailbox write only: notifying inside the loop would re-push
                # every previously answered decision.
                record = mailbox.raise_decision(
                    run_id=snapshot.run_id,
                    node_id=node_id,
                    question=question,
                    options=[dict(item) for item in spec["options"]],
                    recommendation=None,
                    evidence=evidence,
                    scope="run",
                )
                decision_id = str(record["decision_id"])
                if mailbox.result(decision_id) is None:
                    break
            else:
                return False
        except (OSError, TypeError, ValueError):
            return False
        # One push per newly adopted decision.  The request is already on
        # disk, so a bridge failure is an unknown status, never a blocker.
        try:
            notification = NotificationBridge(self.paths).notify(record)
            notification_status = str(notification.get("status")) if isinstance(notification, dict) else "unknown"
        except Exception:
            notification_status = "unknown"
        self._record(snapshot, "decision_raised", {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "decision_ids": [decision_id],
            "source": "monitor_blocked_design",
            "reason": "decision pending",
            "notification_status": notification_status,
            "old_task_reconciled": True,
        })
        current["old_task_reconciled"] = True
        current["decision_ids"] = [decision_id]
        current["status"] = "blocked_design"
        current["reason"] = "decision pending"
        current["owner_layer"] = "product_acceptance"
        current["user_interrupt"] = True
        current["recovery_action"] = None
        current["quarantine"] = None
        return True

    def _consume_answered_decisions(self, snapshot: RunSnapshot) -> None:
        """Advance blocked_design nodes whose decisions are all answered.

        A node moves only when every one of its decisions has a valid answer;
        an answer voided by changed options reads as still pending.  Nodes in
        any other state are left alone.
        """
        mailbox: Optional[DecisionMailbox] = None
        for node_id, current in list(snapshot.nodes.items()):
            ids = current.get("decision_ids")
            if current.get("status") != "blocked_design" or not isinstance(ids, list) or not ids:
                continue
            if mailbox is None:
                mailbox = DecisionMailbox(self.paths)
            answers: List[Dict[str, Any]] = []
            for decision_id in ids:
                try:
                    result = mailbox.result(str(decision_id))
                except (OSError, TypeError, ValueError):
                    result = None
                if result is None:
                    break
                answer = {"decision_id": str(decision_id), "choice": result["choice"]}
                if isinstance(result.get("note"), str) and result["note"]:
                    answer["note"] = result["note"]
                answers.append(answer)
            if len(answers) != len(ids):
                continue
            guard = getattr(self, "_guard_lease", None)
            if guard is not None:
                guard(snapshot.run_id)
            data, problem = self._decision_answer_transition(snapshot, node_id, current, answers)
            if data is None:
                self._mark_blocked_unknown(snapshot, node_id, problem, quarantine_lease=False)
                continue
            self._record(snapshot, DECISION_ANSWER_APPLIED, data)
            self._apply_decision_answer_state(snapshot, node_id, data)

    def _decision_answer_transition(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        current: Dict[str, Any],
        answers: List[Dict[str, Any]],
    ) -> Tuple[Optional[Dict[str, Any]], str]:
        """Event data for the answered node, or (None, reason) if undecidable."""
        data: Dict[str, Any] = {
            "run_id": snapshot.run_id,
            "node_id": node_id,
            "decision_ids": [item["decision_id"] for item in answers],
            "decision_answers": answers,
        }
        cause = current.get("blocked_design_cause")
        if cause is None:
            data["answer_action"] = "developer_resume"
            return data, ""
        choices = {item["choice"] for item in answers}
        action = _ANSWER_ACTIONS.get((cause, next(iter(choices)))) if len(choices) == 1 else None
        if action is None:
            return None, "answered decision cannot be mapped to a node transition"
        data["answer_action"] = action
        if action != "scope_expanded":
            return data, ""
        target = current.get("blocked_design_target")
        target_node = snapshot.nodes.get(target) if isinstance(target, str) else None
        if (
            target == node_id
            or not isinstance(target_node, dict)
            or target_node.get("status") != "accepted"
        ):
            return None, "scope expansion target is no longer an accepted node"
        requested = current.get("blocked_design_files")
        if not isinstance(requested, list) or not requested or len(set(requested)) != len(requested):
            return None, "scope expansion request has no usable file list"
        held = [_scope_compare_key(item) for item in self._files_held_by_other_active_nodes(snapshot, target)]
        own = set(self._node_scope_files(target, target_node))
        files = []
        for item in requested:
            key = _scope_compare_key(item)
            if key == ".":
                return None, "scope expansion request names an unverifiable path"
            # The PM's answer may widen scope past the automatic rules, never
            # past their hard floor: listing the path as configured is the
            # only way auto_scope_rule still refuses it (credentials, .git,
            # colon/format characters, paths leaving the repository).
            if auto_scope_rule(item, (item,)) is None:
                return None, "scope expansion request names a protected path"
            if any(_dag_module._write_paths_overlap(key, other) for other in held):
                return None, "scope expansion file is held by another active node"
            if item not in own:
                files.append(item)
        data["blocked_design_target"] = target
        data["files"] = files
        return data, ""

    def _apply_decision_answer_state(
        self, snapshot: RunSnapshot, node_id: str, data: Dict[str, Any]
    ) -> None:
        """State change of one consumed answer; shared by live run and replay."""
        current = snapshot.nodes[node_id]
        action = data.get("answer_action")
        answers = [dict(item) for item in data.get("decision_answers") or [] if isinstance(item, dict)]
        for key in (
            "decision_ids", "blocked_design_cause", "blocked_design_target",
            "blocked_design_files", "owner_layer", "recovery_action",
        ):
            current.pop(key, None)
        current["user_interrupt"] = False
        current["reason"] = None
        current["quarantine"] = None
        current.setdefault("decision_answers", []).extend(answers)
        reviewer_retry = {
            "role": "reviewer", "phase": "review", "continuation": True,
            "pending_schedule": True, "successor": False, "successor_candidate": False,
        }
        if action == "scope_expanded":
            target = snapshot.nodes[data["blocked_design_target"]]
            expansions = target.setdefault("scope_expansions", [])
            for item in data.get("files", []):
                if item not in expansions:
                    expansions.append(item)
            self._queue_scope_rework(target)
            current["status"] = "rework"
            self._queue_integration_review_rework(snapshot)
        elif action == "scope_kept" or action == "finding_deferred":
            # The next reviewer must see what was decided, or it re-raises it.
            current.setdefault("contract_overrides", {})["decision_answers"] = list(current["decision_answers"])
            if action == "finding_deferred":
                deferred = current.setdefault("deferred_findings", [])
                deferred.extend(item["decision_id"] for item in answers if item["decision_id"] not in deferred)
            current["status"] = "rework"
            current["active_role"] = None
            current["active_task"] = None
            current["pair_archived"] = True
            current["retryable_action"] = dict(reviewer_retry)
            snapshot.handles.pop(node_id, None)
            self._release_node_lease(snapshot, node_id)
        else:
            overrides = current.setdefault("contract_overrides", {})
            if action == "contract_changed":
                accepted = overrides.setdefault("contract_change_accepted", [])
                accepted.extend(item["decision_id"] for item in answers if item["decision_id"] not in accepted)
                overrides["decision_answers"] = list(current["decision_answers"])
            else:
                overrides["decision_answers"] = answers
            snapshot.handles.pop(node_id, None)
            self._queue_scope_rework(current)

    def _route_undecided_blocked_design(self, snapshot: RunSnapshot) -> None:
        """Turn a decision-less blocked_design into a lifecycle recovery state."""
        pending: Optional[List[Dict[str, Any]]] = None
        pending_error: Optional[str] = None
        for node_id, current in list(snapshot.nodes.items()):
            if (
                current.get("status") == "blocked_unknown"
                and current.get("owner_layer") == "supervisor_lifecycle"
                and current.get("recovery_action") == LIFECYCLE_DECISION_RECOVERY_ACTION
            ):
                # The supervisor performed the recovery (decision-raise):
                # close the loop once the mailbox holds a request for the node.
                if pending is None and pending_error is None:
                    try:
                        pending = list(DecisionMailbox(self.paths).list_pending(snapshot.run_id))
                    except (OSError, TypeError, ValueError) as error:
                        pending_error = type(error).__name__
                ids = [
                    str(item.get("decision_id")) for item in pending or []
                    if isinstance(item, dict) and item.get("node_id") == node_id and item.get("decision_id")
                ]
                if not ids and pending_error is None and self._ensure_blocked_design_decision(snapshot, node_id):
                    continue
                if ids:
                    current["status"] = "blocked_design"
                    current["reason"] = "decision pending"
                    current["decision_ids"] = ids
                    current["quarantine"] = None
                    current["owner_layer"] = "product_acceptance"
                    current["recovery_action"] = None
                    current["user_interrupt"] = True
                    self._record(snapshot, "decision_raised", {
                        "run_id": snapshot.run_id,
                        "node_id": node_id,
                        "decision_ids": ids,
                        "source": "supervisor_lifecycle",
                        "reason": "decision pending",
                    })
                continue
            if current.get("status") != "blocked_design":
                continue
            if (
                current.get("recovery_action") == "await_external_decision"
                or current.get("owner_layer") == "external_authorization"
                or current.get("decision_ids")
            ):
                continue
            if pending is None and pending_error is None:
                try:
                    pending = list(DecisionMailbox(self.paths).list_pending(snapshot.run_id))
                except (OSError, TypeError, ValueError) as error:
                    pending_error = type(error).__name__
            if pending_error is None and any(
                isinstance(item, dict) and item.get("node_id") == node_id for item in pending or []
            ):
                continue
            if pending_error is None and self._ensure_blocked_design_decision(snapshot, node_id):
                continue
            if pending_error is None:
                reason = "blocked_design has no decision record; supervisor lifecycle defect"
            else:
                reason = "decision record cannot be verified ({}); supervisor lifecycle defect".format(pending_error)
            self._mark_blocked_unknown(
                snapshot, node_id, reason, quarantine_lease=False,
                extra_event_data={
                    "owner_layer": "supervisor_lifecycle",
                    "recovery_action": LIFECYCLE_DECISION_RECOVERY_ACTION,
                    "user_interrupt": False,
                },
            )
            current["recovery_action"] = LIFECYCLE_DECISION_RECOVERY_ACTION
            current["owner_layer"] = "supervisor_lifecycle"
            current["user_interrupt"] = False

    def _record(
        self,
        snapshot: RunSnapshot,
        event_name: str,
        data: Dict[str, Any],
        active: Optional[Dict[str, Any]] = None,
    ) -> None:
        provenance = {
            "role": active.get("role", "system") if active else "system",
            "task_id": active.get("task_id") if active else None,
            "handle_id": active.get("handle_id") if active else None,
            "generation": active.get("generation", 0) if active else 0,
            "authorization_digest": snapshot.authorization_digest,
            "node_contract_digest": snapshot.node_contract_digest,
        }
        self._guard_lease(getattr(snapshot, "run_id", None))
        snapshot.event_sequence = append_event(
            self.paths, RunEvent(event_name, data), provenance
        )

    def _context_estimate(self, snapshot: RunSnapshot, runner: Runner):
        """Obtain an evidence-bound context estimate when the runner exposes one.

        V1 runners do not expose a context limit and therefore retain the
        existing scheduling behavior.  V2 adapters may provide either a
        ``context_budget`` mapping or ``context_limit_tokens`` plus optional
        text fields; absence is intentionally not treated as zero.
        """
        policy = self.context_policy
        supplied = getattr(runner, "context_budget", None)
        if callable(supplied):
            supplied = supplied()
        if isinstance(supplied, dict):
            policy = policy or ContextBudgetPolicy(
                supplied.get("context_limit_tokens", "observed-model-limit"),
                supplied.get("reserve_tokens"),
                supplied.get("warning_ratio", 0.70),
                supplied.get("checkpoint_ratio", 0.80),
                supplied.get("hard_stop_ratio", 0.90),
            )
            fields = dict(getattr(runner, "context_input", {}) or {})
            fields.update(supplied)
        else:
            limit = getattr(runner, "context_limit_tokens", None)
            if limit is None and policy is None:
                return None
            policy = policy or ContextBudgetPolicy(limit)
            fields = getattr(runner, "context_input", {})
            if not isinstance(fields, dict):
                fields = {}
        estimator = ContextBudgetEstimator(policy)
        if "event_summary" not in fields:
            try:
                recent = load_events(self.paths, snapshot.run_id)[-5:]
                fields["event_summary"] = json.dumps(recent, ensure_ascii=False, sort_keys=True)
            except (OSError, ValueError, TypeError):
                fields["event_summary"] = ""
        if "checkpoint" not in fields:
            checkpoint_path = self.paths.root / ".vibe" / "runs" / snapshot.run_id / "monitor_checkpoint.json"
            try:
                fields["checkpoint"] = checkpoint_path.read_text(encoding="utf-8") if checkpoint_path.is_file() else ""
            except OSError:
                fields["checkpoint"] = ""
        return estimator.estimate(
            str(fields.get("system_prompt", "")),
            str(fields.get("current_input", "")),
            str(fields.get("event_summary", "")),
            str(fields.get("checkpoint", "")),
            str(fields.get("expected_output", "")),
            fields.get("tokenizer"),
        )

    def _checkpoint_context(self, snapshot: RunSnapshot, reason: str, exhausted: bool = False, estimate: Any = None) -> None:
        profiles = {}
        for node_id, node in self.nodes.items():
            profile = node.contract.get("worker_profile")
            if isinstance(profile, dict):
                profiles[node_id] = profile
        checkpoint = MonitorCheckpoint(
            run_id=snapshot.run_id,
            plan_revision="{}@{}".format(snapshot.plan_id, snapshot.plan_version),
            state_version=snapshot.schema_version,
            last_event_seq=snapshot.event_sequence,
            next_action="resume",
            stop_conditions=["preserve one run lease", "do not duplicate writer"],
            authorization_digest=snapshot.authorization_digest,
            node_contract_digest=snapshot.node_contract_digest,
            capability_contract_digest=snapshot.capability_contract_digest,
            nodes=snapshot.nodes,
            handles=snapshot.handles,
            worker_profiles=profiles,
            evidence=[{"reason": reason}],
            estimate=(estimate.__dict__ if estimate is not None else None),
        )
        write_checkpoint(self.paths, checkpoint)
        event_name = "monitor_context_exhausted" if exhausted else "monitor_context_checkpoint"
        self._record(
            snapshot,
            event_name,
            {
                "run_id": snapshot.run_id,
                "reason": reason,
                "checkpoint_sha": checkpoint.sha256,
                "last_event_seq": snapshot.event_sequence,
            },
        )
        if exhausted:
            active_nodes = [node_id for node_id, current in snapshot.nodes.items() if current.get("active_task")]
            for node_id in active_nodes:
                snapshot.nodes[node_id]["status"] = "blocked_unknown"
            snapshot.status = "blocked_unknown"

    def _context_allows_dispatch(self, snapshot: RunSnapshot, runner: Runner) -> bool:
        estimate = self._context_estimate(snapshot, runner)
        if estimate is None:
            return True
        if estimate.status == "normal":
            return True
        events = load_events(self.paths, snapshot.run_id)
        if estimate.status == "warning":
            if not any(item.get("event") == "monitor_context_warning" for item in events):
                self._record(snapshot, "monitor_context_warning", {
                    "run_id": snapshot.run_id,
                    "ratio": estimate.ratio,
                    "total_tokens": estimate.total_tokens,
                    "limit_tokens": estimate.limit_tokens,
                    "source": estimate.source,
                })
            return True
        if estimate.status in {"checkpoint", "hard_stop", "blocked_unknown"}:
            self._checkpoint_context(
                snapshot,
                "context budget status: " + estimate.status,
                exhausted=estimate.status in {"hard_stop", "blocked_unknown"},
                estimate=estimate,
            )
            return False
        return True

    def _reconcile_start_intent(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        data: Dict[str, Any],
        provenance: Dict[str, Any],
    ) -> None:
        current = snapshot.nodes[node_id]
        role = data.get("role")
        phase = data.get("phase")
        if role not in {"developer", "reviewer"}:
            raise ValueError("start intent role is invalid")
        if (role == "reviewer" and phase != "review") or (
            role == "developer" and phase not in {"develop", "rework"}
        ):
            raise ValueError("start intent phase is invalid")
        identity = data.get("task_id")
        expected_identity = (
            current.get(role + "_identity")
            or self._identity_from_contract(self.nodes[node_id], role)
            or "{}:{}".format(role, node_id)
        )
        generation_key = (
            "review_generation" if role == "reviewer" else "developer_generation"
        )
        expected_generation = int(current.get(generation_key, 0)) + 1
        if identity != expected_identity or data.get("generation") != expected_generation:
            raise ValueError("start intent identity or generation is stale")
        if (
            provenance.get("role") != role
            or provenance.get("task_id") != identity
            or provenance.get("generation") != expected_generation
            or provenance.get("handle_id") is not None
        ):
            raise ValueError("start intent provenance is invalid")
        binding = self._load_task_binding(snapshot, node_id, role)
        if (
            binding.task_id != identity
            or binding.generation != expected_generation
            or binding.status != "start_pending"
        ):
            raise ValueError("start intent task binding is stale")
        current[role + "_identity"] = identity
        current[generation_key] = expected_generation
        if role == "reviewer":
            current["reviewer_started"] = True
        current["status"] = "start_pending"
        current["active_role"] = role
        current["start_intent"] = {
            key: data[key]
            for key in ("intent_id", "role", "phase", "task_id", "generation")
        }
        current["active_task"] = {
            "role": role,
            "task_id": identity,
            "generation": expected_generation,
            "handle_id": None,
        }

    def _reconcile_start_confirmed(
        self,
        snapshot: RunSnapshot,
        node_id: str,
        data: Dict[str, Any],
        provenance: Dict[str, Any],
    ) -> None:
        current = snapshot.nodes[node_id]
        active = current.get("active_task")
        if not isinstance(active, dict) or active.get("handle_id") is not None:
            raise ValueError("start confirmation has no matching intent")
        expected = {
            "role": data.get("role"),
            "task_id": data.get("task_id"),
            "generation": data.get("generation"),
        }
        if any(active.get(key) != value for key, value in expected.items()):
            raise ValueError("start confirmation identity or generation is stale")
        handle_id = data.get("handle_id")
        if not isinstance(handle_id, str) or not handle_id:
            raise ValueError("start confirmation handle is invalid")
        if (
            provenance.get("role") != expected["role"]
            or provenance.get("task_id") != expected["task_id"]
            or provenance.get("generation") != expected["generation"]
            or provenance.get("handle_id") != handle_id
        ):
            raise ValueError("start confirmation provenance is invalid")
        if any(
            other_node != node_id and other_handle == handle_id
            for other_node, other_handle in snapshot.handles.items()
        ):
            raise ValueError("start confirmation duplicates an active handle")
        binding = self._load_task_binding(snapshot, node_id, str(expected["role"]))
        desired_status = self._running_status(str(expected["role"]), str(data.get("phase")))
        if binding.status not in {"start_pending", desired_status}:
            raise ValueError("start confirmation task binding status is invalid")
        self._set_binding_status(
            snapshot, node_id, str(expected["role"]), desired_status
        )
        active["handle_id"] = handle_id
        current["active_role"] = expected["role"]
        current["start_intent"] = None
        current["status"] = desired_status
        snapshot.handles[node_id] = handle_id

    def _reconcile_unapplied_events(self, snapshot: RunSnapshot) -> None:
        records = load_events(self.paths, snapshot.run_id)
        pending = records[snapshot.event_sequence :]
        for record in pending:
            if record.get("event") in {
                "supervisor_heartbeat",
                "run_archived",
                "run_archive_failed",
                "run_archive_deferred",
                "daemon_heal",
                "daemon_lease_readopted",
            }:
                # Heartbeats are supervisor lease telemetry, archive records
                # are filesystem evidence and daemon records are its own
                # audit trail (self-heal steps, lease re-adoption, closeout);
                # none of them is a node lifecycle event.
                # They may be appended between snapshot writes and must be
                # consumed before provider polling; a completed run can still
                # be reopened by an accepted patch, at which point its reads
                # transparently land inside .vibe/runs-archive/.
                snapshot.event_sequence = record["sequence"]
                continue
            provenance = record["provenance"]
            if (
                provenance["authorization_digest"] != snapshot.authorization_digest
                or provenance["node_contract_digest"] != snapshot.node_contract_digest
            ):
                raise ValueError("unapplied event lineage is inconsistent")
            data = record["data"]
            if data.get("run_id") != snapshot.run_id:
                raise ValueError("unapplied event run lineage is inconsistent")
            if record["event"] in {
                "execution_topology_observed",
                "monitor_entry_evidence_reconciled",
                "capacity_counter_corrected",
                "capacity_registry_unknown",
            }:
                # System projection evidence is read-only metadata; legacy
                # V3.9 replay must not interpret it as a node lifecycle event.
                snapshot.event_sequence = record["sequence"]
                continue
            if record["event"] == "authorization_reauthorized":
                self._apply_reauthorization_transition(snapshot, data)
                snapshot.event_sequence = record["sequence"]
                continue
            if str(record["event"]).startswith("change_request_"):
                # Change-request evidence is a system-level, target-bound
                # observation.  Replay must be idempotent and preserve it
                # even when the originating node was removed from a newer
                # snapshot; unlike worker events this is not an unknown-node
                # failure.
                if provenance.get("role") != "system":
                    raise ValueError("change request replay lacks system provenance")
                evidence = data.get("evidence")
                if not isinstance(evidence, dict):
                    raise ValueError("change request replay evidence is invalid")
                try:
                    validated = ChangeRequestEvidence.from_dict(evidence)
                    prepared_check = execute_change_request(validated.action, validated.target_contract)
                    if prepared_check.target_digest != validated.target_digest or validated.remote_mutated:
                        raise ValueError("change request replay target evidence is inconsistent")
                except (TypeError, ValueError) as error:
                    raise ValueError("change request replay evidence is invalid") from error
                if not str(record["event"]).startswith("change_request_") or str(record["event"])[len("change_request_"):] != validated.status:
                    raise ValueError("change request replay status is inconsistent")
                node_id = data.get("node_id")
                if isinstance(node_id, str) and node_id in snapshot.nodes:
                    bucket = snapshot.nodes[node_id].setdefault("change_request_evidence", [])
                    if evidence not in bucket:
                        bucket.append(evidence)
                else:
                    bucket = snapshot.tasks.setdefault("_change_request_evidence", {"records": []})
                    if isinstance(bucket, dict):
                        records_bucket = bucket.setdefault("records", [])
                        if evidence not in records_bucket:
                            records_bucket.append(evidence)
                snapshot.event_sequence = record["sequence"]
                continue
            node_id = data.get("node_id")
            if node_id not in snapshot.nodes:
                raise ValueError("unapplied event references an unknown node")
            current = snapshot.nodes[node_id]
            if record['event'] == 'successor_registration_reconciled':
                proof = data.get('proof')
                if provenance.get('role') != 'system' or not isinstance(proof, dict):
                    raise ValueError('successor registration replay proof is invalid')
                action = ProviderActionStore(self.paths).action(proof.get('action_id'))
                new = load_task_binding(self.paths, node_id, 'reviewer', run_id=snapshot.run_id)
                old = load_historical_task_binding(self.paths, snapshot.run_id, node_id, 'reviewer',
                                                  proof.get('predecessor_task_id'))
                if (new.task_id != proof.get('task_id') or new.successor_of != old.task_id
                        or current.get('active_task') is not None or node_id in snapshot.handles
                        or old.status not in {'stopped', 'failed', 'archived'}
                        or not self._proven_successor_action(snapshot, node_id, 'reviewer', new.generation,
                            new.provider, proof.get('action_id'), proof.get('request_digest'),
                            old.task_id, snapshot.authorization_digest)):
                    raise ValueError('successor registration replay lineage is invalid')
                if proof.get('delivery_recovery') is not None:
                    delivery, recovered = self._recover_developer_delivery_projection(
                        snapshot, node_id, require_receipt=True, replay_sequence=record['sequence'])
                    if recovered != proof['delivery_recovery']:
                        raise ValueError('successor registration replay delivery receipt changed')
                    current['delivery_evidence'] = dict(delivery)
                    current.update({key:delivery[key] for key in ('completion_marker','delivery_path','thread_status')})
                current['reviewer_identity'] = new.task_id
                current['review_generation'] = new.generation
                current['status'] = 'blocked_unknown'
                current['quarantine'] = None
                current['retryable_action'] = {'role': 'reviewer', 'phase': 'review', 'continuation': True,
                    'successor': False, 'same_task': True, 'same_task_required': True,
                    'successor_candidate': False, 'reason_class': 'engineering'}
                snapshot.tasks[node_id+':reviewer'] = new.to_dict()
            elif record["event"] == ORPHAN_DISPATCH_ADOPTED:
                replay_orphan_adoption(self, snapshot, node_id, data, provenance)
            elif record["event"] == SELF_REPORT_DELIVERY_RECONCILED:
                # The recovery only lifts the cursor-only refusal on the same
                # registered task; the delivery event recorded after it is
                # replayed by its own branch.
                active = current.get("active_task")
                if (provenance.get("role") != "system" or not isinstance(active, dict)
                        or active.get("task_id") != data.get("task_id")
                        or active.get("generation") != data.get("generation")
                        or snapshot.handles.get(node_id) != data.get("handle_id")):
                    raise ValueError("self-report recovery replay does not follow the node state")
                current["status"] = "running"
                current["retryable_action"] = None
                current["quarantine"] = None
                current["reason"] = None
                current["recovery_action"] = None
            elif record["event"] == "start_intent":
                self._reconcile_start_intent(
                    snapshot, node_id, data, provenance
                )
            elif record["event"] == "successor_binding_pending":
                proof = data.get("proof")
                retry = proof.get("retry") if isinstance(proof, dict) else None
                generation = proof.get("generation") if isinstance(proof, dict) else None
                role = retry.get("role") if isinstance(retry, dict) else None
                generation_key = "review_generation" if role == "reviewer" else "developer_generation"
                if (provenance.get("role") != "system" or role not in {"developer", "reviewer"}
                        or type(generation) is not int
                        or current.get(generation_key, 0) != generation - 1
                        or current.get("active_task") is not None or node_id in snapshot.handles
                        or retry.get("successor") is not True or retry.get("same_task") is not False
                        or retry.get("same_task_required") is not False
                        or retry.get("successor_candidate") is not False
                        or retry.get("phase") not in ({"review"} if role == "reviewer" else {"develop", "rework", "implement"})
                        or retry.get("continuation") is not False
                        or not self._proven_successor_action(
                            snapshot, node_id, role, generation, proof.get("provider"),
                            retry.get("pending_provider_action"), retry.get("request_digest"),
                            retry.get("predecessor_task_id"), snapshot.authorization_digest)):
                    raise ValueError("pending successor replay proof is invalid")
                current["retryable_action"] = dict(retry)
                current["status"] = "blocked_unknown"
                current["active_role"] = None
                current["start_intent"] = None
            elif record["event"] == "start_confirmed":
                self._reconcile_start_confirmed(
                    snapshot, node_id, data, provenance
                )
            elif record["event"] in {"blocked_unknown", "unknown", "timeout"}:
                if provenance["role"] != "system":
                    binding = self._registered_active_binding(
                        snapshot, node_id, provenance
                    )
                    self._set_binding_status(
                        snapshot, node_id, str(provenance["role"]), "blocked_unknown"
                    )
                if (
                    record["event"] in {"unknown", "timeout"}
                    or data.get("retryable_same_task") is True
                ):
                    self._queue_active_retry(
                        snapshot,
                        node_id,
                        str(data.get("reason", record["event"])),
                        record_event=False,
                    )
                current["status"] = self._v42_retry_status() if data.get("retryable_same_task") is True or record["event"] in {"unknown", "timeout"} else "blocked_unknown"
                current["reason"] = data.get("reason", record["event"])
                current["quarantine"] = {
                    "run_id": snapshot.run_id,
                    "handle_id": snapshot.handles.get(node_id),
                    "reason": current["reason"],
                }
                for marker in ("owner_layer", "recovery_action", "user_interrupt"):
                    if marker in data:
                        current[marker] = data[marker]
            elif record["event"] == "retry_scheduled":
                if provenance.get("role") not in {"developer", "reviewer"}:
                    raise ValueError("retry event lacks task provenance")
                active = current.get("active_task")
                if not isinstance(active, dict) or active.get("task_id") != data.get("task_id"):
                    raise ValueError("retry event task identity is stale")
                for field in ("task_id", "handle_id", "generation"):
                    if provenance.get(field) != active.get(field):
                        raise ValueError("retry event provenance is stale")
                if data.get("generation") != active.get("generation"):
                    raise ValueError("retry event generation is stale")
                # The retry marker is durable supervisor state, but the
                # provider binding must also be quarantined during replay.
                # Without this projection the registry remains ``running``
                # after an interrupted snapshot write, allowing a stale
                # binding to be treated as live on the next resume.
                self._set_binding_status(
                    snapshot,
                    node_id,
                    str(provenance["role"]),
                    "blocked_unknown",
                )
                current["retryable_action"] = {
                    "role": data.get("role"),
                    "phase": "review" if data.get("role") == "reviewer" else "develop",
                    "continuation": True,
                    "successor": False,
                    "successor_candidate": False,
                    "same_task": True,
                    "same_task_required": bool(data.get("same_task_required", True)),
                    "task_id": data.get("task_id"),
                    "handle_id": active.get("handle_id"),
                    "generation": data.get("generation"),
                    "cursor": data.get("cursor"),
                    "attempt": data.get("attempt", 1),
                    "reason_class": data.get("reason_class", "unknown"),
                    "next_retry_at": data.get("next_retry_at"),
                    "binding_digest": data.get("binding_digest", ""),
                    "last_observation_ref": data.get("last_observation_ref", ""),
                    "reason": data.get("reason", "retry scheduled"),
                    "defer_once": True,
                }
                current["status"] = self._v42_retry_status()
                current["quarantine"] = {
                    "run_id": snapshot.run_id,
                    "handle_id": active.get("handle_id"),
                    "reason": data.get("reason", "retry scheduled"),
                }
            elif record["event"] == "old_task_reconciled":
                if provenance["role"] != "system":
                    raise ValueError("old task reconciliation lacks system provenance")
                role = data.get("role")
                retry = current.get("retryable_action")
                valid = (
                    isinstance(retry, dict)
                    and retry.get("role") == role
                    and retry.get("successor_candidate") is True
                    and data.get("proof") in {"absent", "stopped"}
                )
                predecessor = data.get("predecessor_task_id")
                proof_identity = None
                if valid:
                    proof_identity = self._prove_old_task_stopped_or_absent(
                        snapshot,
                        node_id,
                        retry,
                    )
                    if data.get("proof") == "absent":
                        valid = proof_identity == "" and predecessor in {None, ""}
                    else:
                        valid = (
                            isinstance(predecessor, str)
                            and bool(predecessor)
                            and proof_identity == predecessor
                        )
                if not valid:
                    self._reject_replayed_old_task_reconciliation(
                        snapshot, node_id, data
                    )
                    snapshot.event_sequence = record["sequence"]
                    continue
                current["old_task_reconciled"] = True
                if isinstance(retry, dict):
                    retry = dict(retry)
                    retry["old_task_reconciled"] = True
                    retry["successor"] = True
                    retry["continuation"] = False
                    retry["predecessor_task_id"] = predecessor
                    current["retryable_action"] = retry
            elif record["event"] == "accepted":
                if self._node_dispatch_topology(self.nodes[node_id]) == TOPOLOGY_VISIBLE_SDD:
                    self._replay_visible_sdd_acceptance(
                        snapshot, node_id, data, provenance
                    )
                    snapshot.event_sequence = record["sequence"]
                    continue
                if provenance["role"] != "reviewer":
                    raise ValueError("unapplied acceptance lacks reviewer provenance")
                self._registered_active_binding(snapshot, node_id, provenance)
                if (
                    provenance["task_id"] != current.get("reviewer_identity")
                    or provenance["generation"] != current.get("review_generation")
                    or provenance["handle_id"] != snapshot.handles.get(node_id)
                ):
                    raise ValueError(
                        "unapplied acceptance reviewer identity or generation is stale"
                    )
                try:
                    registered = self._load_task_binding(snapshot, node_id, "reviewer")
                except (FileNotFoundError, OSError, TypeError, ValueError) as error:
                    raise ValueError(
                        "unapplied acceptance reviewer binding is unavailable"
                    ) from error
                if (
                    registered.task_id != provenance["task_id"]
                    or registered.generation != provenance["generation"]
                ):
                    raise ValueError("unapplied acceptance registry generation is stale")
                self._set_binding_status(
                    snapshot, node_id, "reviewer", "accepted"
                )
                current["status"] = "accepted"
                contract_digest = data.get("contract_digest")
                authorization_epoch = data.get("authorization_epoch")
                # ISSUE-02 (dual-visible replay): recompute the live contract
                # digest and require it to match, identical to the visible-sdd
                # replay path at _replay_visible_sdd_acceptance.  A snapshot
                # copy alone cannot detect a drifted contract.
                try:
                    live_digest_matches = self._live_node_contract_digest_matches(
                        node_id, contract_digest
                    )
                except ValueError as error:
                    raise ValueError(
                        "unapplied dual-visible acceptance node contract is unreadable"
                    ) from error
                if (
                    not isinstance(contract_digest, str)
                    or len(contract_digest) != 64
                    or any(character not in "0123456789abcdef" for character in contract_digest)
                    or contract_digest != current.get("contract_digest")
                    or not live_digest_matches
                    or authorization_epoch != snapshot.authorization_digest
                ):
                    raise ValueError("unapplied acceptance contract epoch is stale")
                current["acceptance"] = {
                    "contract_digest": contract_digest,
                    "authorization_epoch": authorization_epoch,
                }
                # Match the live path: a replayed acceptance must not
                # resurrect the rejection text the lost snapshot carried -- nor
                # the #255 consecutive-rejection counter it raised, nor the
                # escalation recovery action the parked node carried.
                current["reason"] = None
                current.pop("acceptance_format_rejections", None)
                current["recovery_action"] = None
                current["active_role"] = None
                current["active_task"] = None
                current["quarantine"] = None
                snapshot.handles.pop(node_id, None)
                if node_id == "integration-review":
                    # The appended event keeps only redacted provider text, so the
                    # reviewer's structured claim cannot be rebuilt here.  Rather
                    # than close the run out on a package we would have to invent,
                    # expose the gap and let the reviewer report the acceptance
                    # again on a live handle.
                    current["status"] = "blocked_unknown"
                    current["reason"] = (
                        "integration review acceptance must be reported again after recovery"
                    )
                else:
                    # A durable acceptance record is proof the review round
                    # closed: reviewer identity, generation and the live
                    # contract digest were all validated before the event was
                    # recorded.  Re-blocking a recorded acceptance because its
                    # payload lacked the top-level evidence object (the
                    # ISSUE-249 ordering defect) re-created the unrecoverable
                    # reverse-block on every replay, so replay completes the
                    # acceptance the record asserts.
                    current["review_clearance"] = {
                        "p0": 0,
                        "p1": 0,
                        "p2": 0,
                    }
                    for role in ("developer", "reviewer"):
                        self._set_binding_status(
                            snapshot, node_id, role, "archived"
                        )
                    current["pair_archived"] = True
                self._release_node_lease(snapshot, node_id)
            elif record["event"] in {"failed", "stopped", "terminal_failed"}:
                self._registered_active_binding(snapshot, node_id, provenance)
                terminal_status = (
                    "failed" if record["event"] != "stopped" else "stopped"
                )
                self._set_binding_status(
                    snapshot, node_id, str(provenance["role"]), terminal_status
                )
                current["status"] = terminal_status
                current["reason"] = data.get("reason", record["event"])
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            elif record["event"] in {"delivered", "complete"}:
                self._registered_active_binding(snapshot, node_id, provenance)
                if provenance["role"] != "developer":
                    current["status"] = "blocked_unknown"
                    current["reason"] = "unapplied reviewer delivery needs reconciliation"
                else:
                    self._set_binding_status(
                        snapshot, node_id, "developer", "delivered"
                    )
                    current["status"] = "delivered"
                    current["active_role"] = None
                    current["active_task"] = None
                    snapshot.handles.pop(node_id, None)
            elif record["event"] == "quarantine_continuation_recovered":
                if provenance["role"] != "system":
                    raise ValueError(
                        "quarantine continuation recovery lacks system provenance"
                    )
                if (
                    data.get("role") != "developer"
                    or data.get("task_id") != current.get("developer_identity")
                    or data.get("generation") != current.get("developer_generation")
                ):
                    raise ValueError("quarantine continuation recovery is stale")
                if not self._recover_quarantined_delivered_developer(
                    snapshot, node_id, record_event=False
                ):
                    raise ValueError(
                        "quarantine continuation recovery cannot be verified"
                    )
            elif record["event"] == "review_finding":
                # The following transition event/start intent is authoritative.
                self._registered_active_binding(snapshot, node_id, provenance)
                if node_id == "integration-review":
                    current.setdefault("integration_findings", []).append(
                        redact_provider_text(data)
                    )
            elif record["event"] in {"binding_self_healed", "binding_degraded", "action_isolated"}:
                if provenance["role"] != "system":
                    raise ValueError("self-healing event lacks system provenance")
                if record["event"] == "binding_self_healed":
                    binding = data.get("binding", {})
                    frozen = current.get("binding")
                    if not isinstance(binding, dict):
                        raise ValueError("self-healing binding evidence is invalid")
                    if isinstance(frozen, dict):
                        for field in ("task_id", "worktree", "branch", "allowlist", "cursor", "token", "continuation_token", "continuation_digest"):
                            if field in binding and field in frozen and binding[field] != frozen[field]:
                                raise ValueError("self-healing replay binding drift")
                    # Restore the frozen values after validating the event;
                    # observations are never used as replacement facts.
                    if isinstance(frozen, dict):
                        for field in ("task_id", "worktree", "branch", "allowlist", "cursor", "token", "continuation_token", "continuation_digest"):
                            if field in frozen:
                                current[field] = frozen[field]
                    else:
                        current["binding"] = dict(binding)
                        for field in ("task_id", "worktree", "branch", "allowlist", "cursor", "continuation_digest"):
                            if field in binding:
                                current[field] = binding[field]
                    current["healing"] = {"status": "repaired", "observation_kind": data.get("observation_kind", "unknown")}
                else:
                    current["status"] = "blocked_unknown"
                    current["reason"] = data.get("reason", data.get("observation_kind", record["event"]))
                    current["isolated"] = True
                    current["retryable_action"] = None
                    current["quarantine"] = {"run_id": snapshot.run_id, "handle_id": snapshot.handles.get(node_id), "reason": current["reason"]}
            elif record["event"] == "scope_correction_registered":
                if provenance.get("role") != "system":
                    raise ValueError("scope correction replay lacks system provenance")
                self._apply_scope_correction(snapshot, node_id, data.get("proof", {}))
            elif record["event"] == "scope_auto_expanded":
                expansions = current.setdefault("scope_expansions", [])
                for item in data.get("files", []):
                    if item not in expansions:
                        expansions.append(item)
                if data.get("scope_transition") == "queue_rework":
                    self._queue_scope_rework(current)
                    if data.get("trigger") == "integration_rework":
                        self._queue_integration_review_rework(snapshot)
                    else:
                        self._invalidate_integration_review(snapshot)
            elif record["event"] == "consistency_corrected":
                correction = {
                    key: data[key]
                    for key in CONSISTENCY_CORRECTION_KEYS
                }
                current.setdefault("contract_overrides", {})[
                    correction["field"]
                ] = correction["value"]
                if correction not in current.setdefault("corrections", []):
                    current["corrections"].append(correction)
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                current["status"] = "blocked_unknown"
                current["retryable_action"] = {
                    "role": "developer",
                    "phase": "rework",
                    "continuation": True,
                }
            elif record["event"] == "blocked_design":
                current["status"] = "blocked_design"
                current["reason"] = data.get("reason", "design decision required")
                current["old_task_reconciled"] = bool(
                    data.get("old_task_reconciled")
                )
                if data.get("owner_layer"):
                    current["owner_layer"] = data["owner_layer"]
                    current["user_interrupt"] = data["owner_layer"] == "external_authorization"
                if data.get("recovery_action"):
                    current["recovery_action"] = data["recovery_action"]
                if data.get("blocked_design_cause"):
                    current["blocked_design_cause"] = data["blocked_design_cause"]
                if data.get("blocked_design_target"):
                    current["blocked_design_target"] = data["blocked_design_target"]
                if isinstance(data.get("blocked_design_files"), list):
                    current["blocked_design_files"] = list(data["blocked_design_files"])
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            elif record["event"] == DECISION_ANSWER_APPLIED:
                if provenance.get("role") != "system":
                    raise ValueError("decision answer replay lacks system provenance")
                self._apply_decision_answer_state(snapshot, node_id, data)
            elif record["event"] == "decision_raised":
                reason = data.get("reason", "decision pending")
                # The persisted `reason` is always redacted, so a worker
                # delivery's unknown notification rides in the structured
                # `notification_status` (#256).  Only that source parks as
                # blocked_unknown live; the reason comparison stays for
                # events built in memory.
                notification_unknown = reason == DECISION_NOTIFICATION_UNKNOWN_REASON or (
                    data.get("source") == "worker_delivery"
                    and data.get("notification_status") == "unknown"
                )
                if notification_unknown:
                    reason = DECISION_NOTIFICATION_UNKNOWN_REASON
                current["status"] = "blocked_unknown" if notification_unknown else "blocked_design"
                current["reason"] = reason
                current["decision_ids"] = list(data.get("decision_ids") or [])
                current["old_task_reconciled"] = bool(data.get("old_task_reconciled"))
                if notification_unknown:
                    current["quarantine"] = {
                        "run_id": snapshot.run_id,
                        "handle_id": None,
                        "reason": reason,
                    }
                elif data.get("source") in {"supervisor_lifecycle", "monitor_blocked_design"}:
                    # Lifecycle recovery closed: drop the defect markers and
                    # the quarantine the blocked_unknown replay set.
                    current["quarantine"] = None
                    current["owner_layer"] = "product_acceptance"
                    current["recovery_action"] = None
                    current["user_interrupt"] = True
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            elif record["event"] == "review_loop_limit_reached":
                # Crash-window twin of the live three-round park: the event
                # was appended but the snapshot save missed it.  Restore the
                # same fail-closed park so the recovery entry stays executable
                # instead of degrading to the generic manual-reconciliation
                # state (the known cost of the older reviewer_write_violation
                # event, which has no such branch).
                proof = data.get("proof")
                if (
                    data.get("recovery_action") != "change_reviewer_or_split_node"
                    or not isinstance(proof, dict)
                    or not isinstance(proof.get("park_reason"), str)
                    or not proof.get("park_reason")
                    or not isinstance(proof.get("review_loop_rounds"), int)
                    or isinstance(proof.get("review_loop_rounds"), bool)
                    or proof.get("reviewer_replacement_required") is not True
                    or not isinstance(proof.get("reviewer_identity"), str)
                    or not proof.get("reviewer_identity")
                ):
                    raise ValueError("replayed review loop limit evidence is invalid")
                current["status"] = "blocked_unknown"
                # Restore the vibe-authored park reason from ``proof``: the
                # top-level ``reason`` is redacted as provider text on write
                # (``reason`` is a _PROVIDER_TEXT_KEYS member).
                current["reason"] = proof["park_reason"]
                current["recovery_action"] = "change_reviewer_or_split_node"
                current["reviewer_replacement_required"] = True
                current["reviewer_started"] = False
                current["review_loop_rounds"] = proof["review_loop_rounds"]
                current["reviewer_identity"] = proof["reviewer_identity"]
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                self._release_node_lease(snapshot, node_id)
            elif record["event"] == "blocked_engineering":
                # Crash-window twin of _park_blocked_engineering (#335): the
                # event is appended before the snapshot save; without this
                # branch the fall-through would replace the fixed
                # engineering_blocked class with the generic "unapplied event
                # needs manual reconciliation" block and the supervisor could
                # no longer tell a declared engineering block from a lost one.
                if data.get("reason_class") != "engineering_blocked" \
                        or data.get("reason_code") != "blocked_self_report":
                    raise ValueError("replayed blocked engineering evidence is invalid")
                current["status"] = "blocked_unknown"
                current["reason"] = "worker reported a blocked engineering state"
                current["reason_class"] = "engineering_blocked"
                current["reason_code"] = "blocked_self_report"
                current.pop("recovery_action", None)
                current["active_role"] = None
                current["active_task"] = None
                current["retryable_action"] = None
                snapshot.handles.pop(node_id, None)
                handle_id = data.get("handle_id")
                if isinstance(handle_id, str) and handle_id:
                    current["quarantine"] = {
                        "run_id": snapshot.run_id,
                        "handle_id": handle_id,
                        "reason": "worker reported a blocked engineering state",
                    }
                self._release_node_lease(snapshot, node_id)
            elif record["event"] == "pair_archived":
                current["pair_archived"] = True
            elif record["event"] == "acceptance_rejected":
                # Validate-first rejections (ISSUE-83) are pure audit: the
                # live pass mutated no lifecycle state -- the handle, binding
                # and node status were all left for the same task's corrected
                # re-report -- so replay must mutate nothing either.
                pass
            elif record["event"] == "acceptance_recovered":
                # ISSUE-249 rescue audit: a scheduling pass completed an
                # acceptance the legacy ordering had reverse-blocked.  The
                # crash-window snapshot saved before that pass is restored to
                # the same completed state; the rescue pass re-verifies the
                # signature on the next schedule either way.
                current["status"] = "accepted"
                current["reason"] = None
                current["recovery_action"] = None
                current["quarantine"] = None
                if not isinstance(current.get("review_clearance"), dict):
                    current["review_clearance"] = {"p0": 0, "p1": 0, "p2": 0}
                current["pair_archived"] = True
            elif record["event"] == "contract_test_execution":
                # Pure audit like acceptance_rejected: the live pass only
                # gates acceptance (or the expected_red observation) and
                # mutates no lifecycle state, so replay must mutate nothing
                # either -- otherwise a crash between the execution record
                # and the durable acceptance would brick the node into
                # manual reconciliation for no real inconsistency.
                pass
            elif record["event"] == "blocked_unknown_disposition_required":
                # The live pause recorded no delivery and left the handle,
                # binding and lease untouched, so replay restores only the
                # pause marker and the still-pending items.
                current["status"] = "blocked_unknown"
                current["reason"] = (
                    "acceptance paused: blocked_unknown items await disposition"
                )
                proof = data.get("proof")
                undisposed = proof.get("undisposed") if isinstance(proof, dict) else None
                current["blocked_unknowns_pending"] = (
                    [item for item in undisposed if isinstance(item, str)]
                    if isinstance(undisposed, list)
                    else []
                )
            elif record["event"] == "blocked_unknown_disposition":
                if provenance["role"] != "system":
                    raise ValueError(
                        "blocked_unknown disposition lacks system provenance"
                    )
                proof = data.get("proof")
                item = proof.get("item") if isinstance(proof, dict) else None
                disposition = data.get("disposition")
                if (
                    not isinstance(item, str)
                    or not item.strip()
                    or disposition not in BLOCKED_UNKNOWN_DISPOSITION_KINDS
                ):
                    raise ValueError("replayed blocked_unknown disposition is invalid")
                replay_note = proof.get("note", "") if isinstance(proof.get("note", ""), str) else ""
                current.setdefault("blocked_unknown_dispositions", []).append(
                    {"item": item, "disposition": disposition, "note": replay_note}
                )
                if disposition == "deferred_to_acceptance":
                    current.setdefault("acceptance_pending_verification", []).append(
                        {"item": item, "note": replay_note}
                    )
                # Mirror the live path: any disposition answers the pending
                # item; only clearing kinds release the acceptance gate.
                pending = current.get("blocked_unknowns_pending") or []
                current["blocked_unknowns_pending"] = [
                    pending_item
                    for pending_item in pending
                    if pending_item != item
                ]
            elif record["event"] == "handoff_lease_rebound":
                # Evidence only: the lease file was re-keyed before the event.
                if provenance.get("role") != "system" or not self._run_has_platform_handoff(snapshot):
                    raise ValueError("handoff lease rebound replay provenance is invalid")
            elif record["event"] == "handoff_stale_creates_hidden":
                # Evidence only: the stale markers were written before the event.
                if provenance.get("role") != "system" or not self._run_has_platform_handoff(snapshot):
                    raise ValueError("handoff stale creates replay provenance is invalid")
            elif record["event"] == "handoff_predecessor_reconciled":
                # Evidence only: the registry already holds the archived
                # predecessor, and the successor's own start events replay
                # through their branches.
                if provenance.get("role") != "system" or data.get("proof") != "platform_handoff":
                    raise ValueError("handoff predecessor replay provenance is invalid")
            elif record["event"] == "integration_workspace_quarantined":
                if not is_integration_review_node(self.nodes[node_id]) or provenance.get("role") != "reviewer":
                    raise ValueError("integration quarantine provenance is invalid")
                current["workspace_reconciliation_required"] = True
                current["reviewer_replacement_required"] = True
                current["acceptance"] = None
                current["status"] = "blocked_unknown"
            elif (
                record["event"] == "contract_error"
                and data.get("reason_code") == "integration_initial_preflight_retry"
                and is_integration_review_node(self.nodes[node_id])
                and not current.get("reviewer_identity")
                and not current.get("active_task")
                and not current.get("start_intent")
                and node_id not in snapshot.handles
            ):
                current["status"] = "planned"
                current["review_generation"] = 0
                current["reviewer_started"] = False
                current["retryable_action"] = None
                self._release_node_lease(snapshot, node_id)
            elif record["event"] in {"worktree_created", "worktree_preflight_blocked"}:
                # #235 crash window.  The CLI appends one of these events and
                # only then saves the snapshot, so a crash in between leaves
                # the event unapplied.  Without a twin the fall-through else
                # parks the node as blocked_unknown "needs manual
                # reconciliation" -- which is the silent 自动修复中 the #235
                # marker exists to replace, resurrected by the very crash the
                # marker was meant to survive.
                if provenance.get("role") != "system":
                    raise ValueError("worktree preflight replay lacks system provenance")
                marker = {
                    key: data[key]
                    for key in WORKTREE_PREFLIGHT_KEYS
                    if isinstance(data.get(key), str) and data[key].strip()
                }
                if record["event"] == "worktree_created":
                    # The launcher built the worktree: the action a supervisor
                    # was asked to take is done, so the marker must go.
                    current.pop(WORKTREE_PREFLIGHT_KEY, None)
                    if marker.get("worktree_base"):
                        current["worktree_base"] = marker["worktree_base"]
                else:
                    # A block without its fixed-enum code is a malformed
                    # record, not a block: the label is derived from the code,
                    # so an empty marker would render nothing while the node
                    # still refuses to dispatch.  Fail closed instead.
                    if marker.get("reason_code") not in WORKTREE_REASON_CODES:
                        raise ValueError("worktree preflight replay lacks a reason code")
                    current[WORKTREE_PREFLIGHT_KEY] = marker
                # The label is derived from the marker, never stored beside
                # it twice: recomputing keeps replay and the live path equal.
                current["user_status"] = map_user_status(current)
            elif record["event"] == "model_fallback_used":
                proof = data.get("proof")
                if (
                    not isinstance(proof, dict)
                    or not isinstance(proof.get("from_model"), str)
                    or not proof["from_model"]
                    or not isinstance(proof.get("to_model"), str)
                    or not proof["to_model"]
                    or not isinstance(proof.get("reason_class"), str)
                    or not proof["reason_class"]
                ):
                    raise ValueError("replayed model fallback evidence is invalid")
                overrides = current.setdefault("contract_overrides", {})
                raw_profile = overrides.get("worker_profile")
                if not isinstance(raw_profile, dict):
                    raw_profile = self.nodes[node_id].contract.get("worker_profile")
                if not isinstance(raw_profile, dict):
                    raise ValueError("replayed model fallback profile is unavailable")
                current.setdefault("model_dispatch_chain_profile", dict(raw_profile))
                chain = self._profile_model_chain({"worker_profile": raw_profile}, current)
                if chain:
                    current["model_dispatch_chain"] = chain
                attempts = current.setdefault("model_dispatch_attempts", [])
                if not any(
                    isinstance(item, dict) and item.get("model") == proof["from_model"]
                    for item in attempts
                ):
                    attempts.append({
                        "model": proof["from_model"],
                        "reason_class": proof["reason_class"],
                        "reason": "replayed model fallback",
                    })
                profile = dict(raw_profile)
                profile["model"] = proof["to_model"]
                profile["fallbacks"] = [
                    item for item in profile.get("fallbacks") or []
                    if (item.get("model") if isinstance(item, dict) else item) != proof["to_model"]
                ]
                overrides["worker_profile"] = profile
            elif record["event"] == "model_unavailable_parked":
                proof = data.get("proof")
                if (
                    not isinstance(proof, dict)
                    or not isinstance(proof.get("rounds"), int)
                    or isinstance(proof.get("rounds"), bool)
                    or proof["rounds"] < 1
                    or not isinstance(proof.get("attempts"), list)
                ):
                    raise ValueError("replayed model unavailable evidence is invalid")
                attempts = proof["attempts"]
                current["model_unavailable_rounds"] = proof["rounds"]
                current["model_unavailable"] = {
                    "rounds": proof["rounds"], "attempts": list(attempts),
                }
                current["status"] = self._v42_retry_status()
                current["reason"] = "model fallback chain exhausted"
                current["active_role"] = None
                current["active_task"] = None
                current["start_intent"] = None
                current["retryable_action"] = None
            elif record["event"] == "empty_landing_allowed":
                # Audit-only event: the delivery transition that follows is
                # authoritative.  Recognise it so a crash before the snapshot
                # save never degrades an allowed empty landing into a manual
                # reconciliation block.
                pass
            elif record["event"] == "scope_landing_rejected" and data.get(
                "reason_class"
            ) == "landing_unreadable":
                # #336 / IR-6 crash-window twin of _park_landing_unreadable:
                # an unconfirmable landing parks blocked_unknown, not the
                # worker-question awaiting_supervisor state; without this
                # branch the generic replay below would rebuild the legacy
                # park and lose the structural code.
                if data.get("reason_code") != "scope_landing:landing_unreadable":
                    raise ValueError("replayed landing-unreadable evidence is invalid")
                current["status"] = "blocked_unknown"
                current["reason"] = "delivery landing cannot be confirmed"
                current["reason_class"] = "landing_unreadable"
                current["reason_code"] = "scope_landing:landing_unreadable"
                current["scope_landing_rejection"] = [
                    {"path": "*", "cause": "landing_unreadable"}
                ]
                current.pop("recovery_action", None)
                current.pop("error_type", None)
                current["retryable_action"] = None
                current["active_role"] = None
                current["active_task"] = None
                snapshot.handles.pop(node_id, None)
                current["quarantine"] = {
                    "run_id": snapshot.run_id,
                    "handle_id": data.get("handle_id"),
                    "reason": "delivery landing cannot be confirmed",
                }
                self._release_node_lease(snapshot, node_id)
            elif record["event"] in WORKER_QUESTION_REPLAY_EVENTS:
                # The park/answer paths append the event before the snapshot
                # save; a crash in between must not turn the open question
                # into "needs manual reconciliation".
                replay_worker_question(snapshot, node_id, record["event"], data)
            else:
                current["status"] = "blocked_unknown"
                current["reason"] = "unapplied event needs manual reconciliation"
            snapshot.event_sequence = record["sequence"]

    def _release_node_lease(self, snapshot: RunSnapshot, node_id: str) -> None:
        """Drop the node's writer lease.

        Reads only structural node fields (worktree) and the run id, so it
        carries none of the #256 distortions; callers use it as a terminal
        side effect and must not wrap it in a rollback that reads a binding
        status afterwards (see ``RECOVERY_FIDELITY_AUDIT``).
        """
        current = snapshot.nodes[node_id]
        release_writer_lease(
            self.paths,
            node_id,
            str(current.get("worktree", "")),
            snapshot.run_id,
        )

    def _contract_test_entries(self, node_id: str):
        return monitor_contracts._contract_test_entries(self, node_id)

    def _record_contract_test_execution(self, snapshot: RunSnapshot, node_id: str, phase: str, entry: Dict[str, Any], exit_code: Optional[int], output_tail: str, worktree: Path, active: Optional[Dict[str, Any]]=None) -> None:
        return monitor_contracts._record_contract_test_execution(self, snapshot, node_id, phase, entry, exit_code, output_tail, worktree, active)

    def _execute_contract_test_commands(self, snapshot: RunSnapshot, node_id: str, phase: str, active: Optional[Dict[str, Any]]=None) -> Optional[str]:
        return monitor_contracts._execute_contract_test_commands(self, snapshot, node_id, phase, active)

    def _worktree_path(self, current: Dict[str, Any]) -> Path:
        worktree_path = Path(str(current.get("worktree", ".")))
        if not worktree_path.is_absolute():
            worktree_path = self.paths.root / worktree_path
        return worktree_path

    @staticmethod
    def _refresh_run_status(snapshot: RunSnapshot) -> None:
        for current in snapshot.nodes.values():
            current["user_status"] = map_user_status(current)
        statuses = [node.get("status") for node in snapshot.nodes.values()]
        closeout = evaluate_v41_closeout(snapshot)
        if closeout.allowed:
            snapshot.status = "complete"
        elif "blocked_unknown" in statuses:
            snapshot.status = "blocked_unknown"
        elif "blocked_design" in statuses:
            snapshot.status = "blocked_design"
        elif "failed" in statuses or "stopped" in statuses:
            snapshot.status = "failed"
        else:
            snapshot.status = "running"
