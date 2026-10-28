"""Extracted from monitor.py (behavior unchanged)."""

from typing import Any, Dict, Optional

from .contracts import RunEvent
from .state import RunSnapshot


#: Protocol pointer carried by every visible-sdd create request so the worker
#: session can resolve the in-session SDD protocol from the installed package.
VISIBLE_SDD_PROTOCOL_REF = "vibe_guide/protocols/visible-sdd-worker.md"


def _reject_replayed_old_task_reconciliation(
    monitor, snapshot: RunSnapshot, node_id: str, data: Dict[str, Any]
) -> None:
    """Consume an untrusted reconciliation event without starting a writer."""
    current = snapshot.nodes[node_id]
    current["status"] = "blocked_unknown"
    current["reason"] = "replayed old task reconciliation proof is invalid"
    current["quarantine"] = {
        "run_id": snapshot.run_id,
        "handle_id": snapshot.handles.get(node_id),
        "reason": current["reason"],
    }
    current["old_task_reconciled"] = False
    current["retryable_action"] = None


def _validate_visible_sdd_review(
    monitor,
    snapshot: RunSnapshot,
    node_id: str,
    event: RunEvent,
    current: Dict[str, Any],
):
    """Pure validation of the in-session review evidence; no writes.

    Returns ``None`` when the delivery may be accepted, otherwise
    ``(reason, recoverable, code)``.  ``code`` (#254) is the stable
    machine-readable rejection class that survives the provider-text
    redaction the free-text ``reason`` goes through on persistence.
    Missing or malformed evidence and a foreign protocol pointer are
    format errors (recoverable -- the same worker session can correct and
    re-report on its live handle, per ISSUE-83); an unreadable or drifted
    node contract is an evidence problem and stays fail-closed.
    """
    review = event.data.get("in_session_review")
    clearance = review.get("clearance") if isinstance(review, dict) else None
    evidence_ref = review.get("evidence_ref") if isinstance(review, dict) else None
    valid = (
        isinstance(review, dict)
        and isinstance(clearance, dict)
        and all(
            isinstance(clearance.get(severity), int)
            and not isinstance(clearance.get(severity), bool)
            and clearance.get(severity) == 0
            for severity in ("p0", "p1", "p2")
        )
        and isinstance(evidence_ref, str)
        and bool(evidence_ref.strip())
    )
    if not valid:
        return (
            "visible-sdd delivery lacks verifiable in-session review evidence",
            True,
            "sdd_review:missing_evidence",
        )
    blocked_unknowns = review.get("blocked_unknowns", [])
    if not isinstance(blocked_unknowns, list) or any(
        not isinstance(item, str) or not item.strip() for item in blocked_unknowns
    ):
        # Shape errors stay on the recoverable acceptance_rejected
        # channel; only a well-formed non-empty list reaches the
        # disposition gate in _apply_event.
        return (
            "visible-sdd in-session review blocked_unknowns must be a list of non-empty strings",
            True,
            "sdd_review:blocked_unknowns_shape",
        )
    if review.get("protocol") != VISIBLE_SDD_PROTOCOL_REF:
        # The session must have followed the protocol shipped with this
        # package; a different or missing pointer is not the visible-sdd
        # review this acceptance certifies.
        return (
            "visible-sdd delivery cites an unknown in-session review protocol",
            True,
            "sdd_review:unknown_protocol",
        )
    environment_facts_ref = review.get("environment_facts_ref")
    if not isinstance(environment_facts_ref, str) or not environment_facts_ref.strip():
        # The shipped protocol (visible-sdd-worker.md section 3) makes
        # this field mandatory so the acceptance stays traceable to the
        # environment facts the in-session review was dispatched with
        # ("none" when the contract declares no environment_facts).  A
        # missing or blank value is a format error the same session can
        # correct and re-report on its live handle.
        return (
            "visible-sdd in-session review environment_facts_ref must be a non-empty string",
            True,
            "sdd_review:environment_facts_ref_missing",
        )
    # Coverage denominator (protocol section 2.5 step 0): the review must
    # state how many of the reviewable files it actually covered.  Without
    # a denominator a review that silently skipped half the changeset is
    # indistinguishable from an exhaustive one -- the clearance numbers
    # only count findings, never coverage.  The field is therefore
    # mandatory and its arithmetic is checked here instead of being
    # trusted from prose.  ``mode: "none"`` is the explicit waiver path:
    # it must carry a reason, so "did not obtain the list" is an auditable
    # fact rather than a missing key.
    coverage_failure = monitor._validate_visible_sdd_coverage(
        review.get("coverage") if isinstance(review, dict) else None
    )
    if coverage_failure is not None:
        return coverage_failure, True, "sdd_review:coverage_invalid"
    # ISSUE-02: the acceptance evidence is bound to the node contract it
    # reviewed.  The carried digest is the dispatch-time node digest and
    # must still equal the digest recomputed from the live contract; an
    # unreadable contract or a drifted one refuses the acceptance instead
    # of writing an empty, placeholder or stale digest.
    try:
        live_digest_matches = monitor._live_node_contract_digest_matches(
            node_id, current.get("contract_digest")
        )
    except ValueError as error:
        return (
            "visible-sdd acceptance refused: {}".format(error),
            False,
            "sdd_review:contract_unreadable",
        )
    if not live_digest_matches:
        return (
            "visible-sdd acceptance refused: contract digest does not match the live node contract",
            False,
            "sdd_review:contract_digest_drift",
        )
    return None


def _validate_visible_sdd_coverage(coverage: Any) -> Optional[str]:
    """Validate the review's coverage denominator; ``None`` when acceptable.

    The shipped protocol (visible-sdd-worker.md section 2.5 step 0)
    requires the reviewer to obtain the deterministic reviewable-file list
    (``ocr delegate preview``) and to account for every entry -- reviewed,
    or skipped with a stated reason.  A review that never states its
    denominator cannot be told apart from one that silently skipped files,
    so ``coverage`` is a required field and its arithmetic must add up.

    ``mode: "none"`` is the waiver path: it makes "no deterministic list
    was obtained" a visible, reasoned fact instead of an absent key.  The
    waiver covers the *tool*, never the obligation to account for scope.

    Every branch returns a recoverable format error: the same worker
    session can correct the payload and re-report on its live handle.

    Known ceiling: ``coverage`` is monitor-reported.  This validator checks
    types and internal arithmetic only -- it never cross-checks ``listed``
    against the real changeset or against the ``ocr delegate preview``
    output, so a reviewer can still pass by declaring a *smaller*
    changeset.  The protocol names that as fabrication (section 5); closing
    it mechanically would need a ground truth this layer does not have.
    ``CoverageDenominatorTests`` pins the accepted case, so adding the
    cross-check later has to be a conscious change rather than a silent one.
    """
    # ponytail: coverage 是自报字段，这里只校验类型与算术自洽
    # （listed == reviewed + len(skipped)），不核对真实 git 变更集或
    # preview 输出；需要机械证伪时，改为要求 coverage 附带 preview 输出的
    # 确定性摘要（文件数/内容哈希）并与监工侧可复算的量比对。
    if not isinstance(coverage, dict):
        return (
            "visible-sdd in-session review coverage must be an object "
            '({"mode": "ocr", "listed": N, "reviewed": M, "skipped": [...]} '
            'or {"mode": "none", "reason": "..."})'
        )
    mode = coverage.get("mode")
    if mode == "none":
        reason = coverage.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return (
                'visible-sdd in-session review coverage mode "none" requires '
                "a non-empty reason"
            )
        return None
    if mode != "ocr":
        return (
            'visible-sdd in-session review coverage mode must be "ocr" or "none"'
        )
    listed = coverage.get("listed")
    reviewed = coverage.get("reviewed")
    for field_name, value in (("listed", listed), ("reviewed", reviewed)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return (
                "visible-sdd in-session review coverage {} must be a "
                "non-negative integer".format(field_name)
            )
    skipped = coverage.get("skipped")
    if not isinstance(skipped, list):
        return (
            "visible-sdd in-session review coverage skipped must be a list "
            'of {"file", "reason"} objects'
        )
    seen_files = set()
    for item in skipped:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("file"), str)
            or not item.get("file", "").strip()
            or not isinstance(item.get("reason"), str)
            or not item.get("reason", "").strip()
        ):
            return (
                "visible-sdd in-session review coverage skipped entries must "
                'each carry non-empty "file" and "reason" strings'
            )
        # ``len(skipped)`` is an addend of the denominator, so duplicate
        # entries would inflate it for free and turn "per-file
        # disposition" back into "count the entries".
        name = item["file"].strip()
        if name in seen_files:
            return (
                "visible-sdd in-session review coverage skipped lists {} "
                "more than once; account for each file once".format(name)
            )
        seen_files.add(name)
    if listed != reviewed + len(skipped):
        return (
            "visible-sdd in-session review coverage does not add up: "
            "listed {} != reviewed {} + skipped {}".format(
                listed, reviewed, len(skipped)
            )
        )
    return None


def _event_claims_match(
    event: RunEvent, node_id: str, active: Dict[str, Any]
) -> bool:
    claims = {
        "node_id": node_id,
        "role": active.get("role"),
        "task_id": active.get("task_id"),
        "handle_id": active.get("handle_id"),
        "generation": active.get("generation"),
    }
    return all(event.data.get(key) == value for key, value in claims.items())


def _review_finding_severity(data: Dict[str, Any]):
    """Extract a reviewer severity from the supported finding shapes."""
    values = []
    if isinstance(data, dict):
        values.append(data.get("severity"))
        for key in ("finding", "review_finding"):
            value = data.get(key)
            if isinstance(value, dict):
                values.append(value.get("severity"))
                nested = value.get("finding")
                if isinstance(nested, dict):
                    values.append(nested.get("severity"))
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return None


def _review_finding_disposition(cls, data: Dict[str, Any]) -> str:
    """Classify a reviewer finding before applying product-boundary logic.

    P3 is an observation and cannot block a run.  Missing or non-standard
    severities are malformed engineering feedback, so the same developer
    gets a corrective pass instead of the node being escalated as a
    product decision.  P0-P2 are implementation findings and likewise
    return to the original developer.
    """
    severity = cls._review_finding_severity(data)
    if severity == "P3":
        return "observation"
    if severity in {"P0", "P1", "P2"}:
        return "implementation"
    # A malformed severity with a consistency payload still goes through
    # the approved-decision resolver: it may be a genuine product
    # boundary disagreement.  When the consistency payload is absent,
    # there is no product choice to resolve, so retry the same developer.
    if isinstance(data, dict) and data.get("consistency"):
        return "defer_to_consistency"
    return "implementation"


def _reviewer_write_violation(data: Dict[str, Any]) -> bool:
    """Recognize an explicit reviewer write violation from provider data."""
    if not isinstance(data, dict):
        return False
    if data.get("reviewer_write_violation") is True:
        return True
    if data.get("reviewer_modified_files"):
        return True
    if data.get("business_code_edited") is True:
        return True
    kind = str(data.get("kind") or data.get("type") or "").casefold()
    return kind in {"reviewer_write_violation", "reviewer_modified_business_code"}


def _finding_changes_prd_acceptance(data: Dict[str, Any]) -> bool:
    """Return whether an unresolved finding changes the product contract."""
    if not isinstance(data, dict):
        return False
    return any(
        data.get(key) is True
        for key in (
            "changes_prd_acceptance",
            "changes_acceptance",
            "product_scope_change",
            "product_design_change",
        )
    )
