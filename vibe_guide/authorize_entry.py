"""V4.5 `authorize` entry: project published artifacts into workflow evidence.

Monitor reads the mandatory ten-node workflow evidence out of ``.vibe/state.json``
and refuses to start a complex run without it, but no command ever wrote it.
This module closes that gap without weakening the gate:

* Every one of the ten node records is a projection of an artifact that already
  passed the planning gate, and each carries that artifact's project-relative
  path plus the SHA-256 of the bytes actually read, so a reviewer can re-derive
  it.  ``verify_workflow_artifacts`` re-hashes them before execution, so a plan
  edited after authorization does not run on the old evidence.
* The decision digest is recomputed from the decisions read here and compared
  against the card, rather than copied out of the card, so an edited decision is
  detected instead of attested.
* ``user_authorization`` is recorded only from the exact token the user supplied
  on the command line, and the guard that accepts that token runs before any
  artifact is read.
* Evidence is stored per plan and selected by plan identity, so a token given
  for one plan cannot start another.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .authorization import _canonical_digest
from .diagnostics import assert_planning_gate, require_execution_ready
from .planner import required_workflow_nodes
from .workflow_gate import record_workflow_node, verify_workflow


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, data: Any) -> None:
    """Write ``data`` the way the rest of the package writes state.

    A plain ``write_text`` on the live path can leave a truncated
    ``state.json`` behind, and every later ``require_v42_sdd_first`` would then
    fail with ``v42_state_required``.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path: Path) -> Any:
    if not path.is_file() or path.is_symlink():
        raise ValueError("published artifact is missing or not a regular file: " + path.name)
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact(root: Path, name: str) -> Dict[str, str]:
    """Return the project-relative reference and content digest of an artifact."""
    path = root / name
    if not path.is_file() or path.is_symlink():
        raise ValueError("published artifact is missing or not a regular file: " + name)
    return {"ref": name, "sha256": _sha(path)}


def build_workflow_evidence(paths, plan_id: str, authorization_token: str) -> Dict[str, Any]:
    """Project the published plan of ``plan_id`` into verified workflow evidence.

    The planning gate runs first, so a plan that is not execution-ready never
    produces evidence.  Raises ``PermissionError`` when the gate blocks and
    ``ValueError`` when a required artifact is absent or self-inconsistent.
    """
    if not isinstance(authorization_token, str) or authorization_token != "AUTHORIZE":
        raise PermissionError("authorization required: the exact AUTHORIZE token is required")
    require_execution_ready(assert_planning_gate(paths, plan_id))

    root = paths.resolve_vibe_path(Path("plans") / plan_id)
    plan = _read_json(root / "plan.json")
    nodes = _read_json(root / "nodes.json")
    card = _read_json(root / "authorization-card.json")
    audit = _read_json(root / "dag-audit.json")
    confirmation = _read_json(root / "plan-confirmation.json")
    prd_text = (root / "prd.md").read_text(encoding="utf-8")
    if not isinstance(plan, Mapping) or not isinstance(card, Mapping):
        raise ValueError("published plan and authorization card must be objects")
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("published nodes.json must be a non-empty list")
    if plan.get("complexity_band") != "complex":
        raise ValueError("the authorize entry is only defined for complex plans")

    node_ids = [str(item.get("id")) for item in nodes]
    objective = str(plan.get("prd_path", ""))
    decisions = plan.get("decisions") or []
    if not isinstance(decisions, list) or not decisions:
        raise ValueError("a complex plan requires at least one approved product decision")
    for item in decisions:
        if not isinstance(item, Mapping) or item.get("status") != "approved" or item.get("selected") not in (item.get("options") or []):
            raise ValueError("published product decisions are not all approved")

    spec_names = sorted(item.stem for item in (root / "specs").glob("*.md"))
    issue_names = sorted(item.stem for item in (root / "issues").glob("*.md"))
    if spec_names != sorted(node_ids) or issue_names != sorted(node_ids):
        raise ValueError("published spec/issue set does not match the node set")
    if audit.get("status") != "reviewed" or sorted(audit.get("node_ids") or []) != sorted(node_ids):
        raise ValueError("published DAG audit does not match the node set")
    if confirmation.get("authorization_digest") != card.get("digest"):
        raise ValueError("plan confirmation is not bound to the authorization card digest")

    # `classify_s0` screens the raw user request, which a published plan does not
    # retain; running it on the PRD path or body returns `simple` for a complex
    # plan and would record a fabricated assessment.  The authentic S0 outcome is
    # the escalation the published route already evidences.
    # Recompute the decision digest over the decisions actually read here, using
    # the same canonicalization the card used.  Re-reading the card's stored
    # digest would be a constant relative to the validation above, so it could
    # not detect a decision edited after publication.
    decision_digest = _canonical_digest(
        {"decisions": decisions, "evidence_priority": plan.get("evidence_priority") or []}
    )
    if decision_digest != card.get("decision_digest"):
        raise ValueError("published decisions do not match the authorization card decision digest")
    # The route is read from the published plan, not re-derived from a
    # fabricated S1 score; `complexity_band` was asserted to be complex above.
    workflow = {
        "task_id": plan_id,
        "route": "complex",
        "nodes": required_workflow_nodes("complex"),
        "node_records": {},
        "authorization_granted": False,
    }

    # Each entry is (node_id, input, output, evidence).  Every evidence value
    # names the artifact it was read from, so a reviewer can re-derive it.
    steps: List[tuple] = [
        (
            "s0",
            {"plan_id": plan_id, "objective_ref": objective},
            {"simple": False, "needs_s1": True, "route_hint": "s1", "rationale": "已发布计划的 complexity_band 为 complex，S0 必然升级到 S1"},
            {
                "verified_fact": "published plan.json records complexity_band=complex, which only an S0 escalation can reach",
                "artifact": _artifact(root, "plan.json"),
                "complexity_band": str(plan.get("complexity_band")),
            },
        ),
        (
            "s1",
            {"complexity_band": plan.get("complexity_band"), "node_count": len(nodes)},
            {"route": "complex", "required_workflow": list(card.get("required_workflow") or [])},
            {"verified_fact": "published plan.json records complexity_band=complex", "artifact": _artifact(root, "plan.json")},
        ),
        (
            "requirements",
            {"prd_ref": str(plan.get("prd_path"))},
            {"approved": "approved" in prd_text.lower(), "reviewed": "review" in prd_text.lower(), "length": len(prd_text)},
            {"verified_fact": "published prd.md read from disk", "artifact": _artifact(root, "prd.md")},
        ),
        (
            "product_decision",
            {"decision_count": len(decisions)},
            {"all_approved": True, "selected": [str(item.get("selected")) for item in decisions]},
            {
                "verified_fact": "decisions recomputed from plan.json match the card decision digest",
                "artifact": _artifact(root, "plan.json"),
                "decision_digest": decision_digest,
            },
        ),
        (
            "prd",
            {"prd_ref": str(plan.get("prd_path"))},
            {"status": "approved", "review": "reviewed"},
            {"verified_fact": "planning gate confirmed prd.md carries approved and review markers", "artifact": _artifact(root, "prd.md")},
        ),
        (
            "spec_issue",
            {"node_ids": node_ids},
            {"specs": spec_names, "issues": issue_names},
            {
                "verified_fact": "one reviewed spec and issue per node, no extras",
                "artifact": _artifact(root, "nodes.json"),
                "spec_artifacts": [_artifact(root, "specs/" + name + ".md") for name in spec_names],
                "issue_artifacts": [_artifact(root, "issues/" + name + ".md") for name in issue_names],
            },
        ),
        (
            "dag_audit",
            {"node_ids": node_ids},
            {"status": str(audit.get("status")), "node_count": audit.get("node_count")},
            {"verified_fact": "published dag-audit.json is reviewed and node-consistent", "artifact": _artifact(root, "dag-audit.json")},
        ),
        (
            "plan_confirmation",
            {"plan_revision": str(plan.get("version"))},
            {"status": str(confirmation.get("status")), "authorization_digest": str(confirmation.get("authorization_digest"))},
            {"verified_fact": "plan confirmation digest equals the authorization card digest", "artifact": _artifact(root, "plan-confirmation.json")},
        ),
        (
            "authorization_card",
            {"plan_id": plan_id, "plan_revision": str(plan.get("version"))},
            {
                "digest": str(card.get("digest")),
                "node_contract_digest": str(card.get("node_contract_digest")),
                "remote_git_actions": str(card.get("remote_git_actions")),
                "excluded_actions": list(card.get("excluded_actions") or []),
            },
            {"verified_fact": "published authorization-card.json read from disk", "artifact": _artifact(root, "authorization-card.json")},
        ),
        (
            # Recorded only from the token the user actually supplied; the guard
            # at the top of this function is the single place that accepts it.
            "user_authorization",
            {"token_required": "AUTHORIZE", "authorization_digest": str(card.get("digest"))},
            {"granted": True, "token": authorization_token},
            {
                "verified_fact": "the user supplied the exact AUTHORIZE token on the command line",
                "artifact": _artifact(root, "authorization-card.json"),
                "authorization_digest": str(card.get("digest")),
                "authorized_plan_id": plan_id,
            },
        ),
    ]

    for node_id, input_data, output_data, evidence in steps:
        record_workflow_node(workflow, node_id, input_data, output_data, evidence)

    result = verify_workflow(workflow)
    if result.get("status") != "complete":
        raise PermissionError("required_workflow_blocked: " + str(result.get("node") or result.get("reason") or "unknown"))
    return workflow


def select_plan_workflow(state_data: Any, plan_id: str) -> Any:
    """Return the evidence authorizing ``plan_id``, or ``None``.

    Evidence is stored per plan under ``task_workflow``.  A token given for one
    plan must never start another, so a workflow whose ``task_id`` names a
    different plan is treated as absent rather than accepted.  The single-object
    layout written before per-plan keying is still read, under the same
    identity check.
    """
    if not isinstance(state_data, Mapping):
        return None
    stored = state_data.get("task_workflow")
    if stored is None:
        stored = state_data.get("workflow")
    if not isinstance(stored, Mapping):
        return None
    if "task_id" in stored:
        return stored if stored.get("task_id") == plan_id else None
    candidate = stored.get(plan_id)
    if isinstance(candidate, Mapping) and candidate.get("task_id") == plan_id:
        return candidate
    return None


def verify_workflow_artifacts(paths, workflow: Mapping[str, Any]) -> None:
    """Re-hash every artifact a record was derived from.

    Recorded digests are otherwise never compared again, which would let a plan
    be edited after authorization and still execute on the old evidence.
    """
    plan_id = workflow.get("task_id")
    if not isinstance(plan_id, str) or not plan_id:
        raise PermissionError("workflow_evidence_stale: workflow identity is missing")
    root = paths.resolve_vibe_path(Path("plans") / plan_id)
    records = workflow.get("node_records")
    if not isinstance(records, Mapping):
        raise PermissionError("workflow_evidence_stale: workflow records are missing")
    for node_id, record in records.items():
        evidence = record.get("evidence") if isinstance(record, Mapping) else None
        if not isinstance(evidence, Mapping):
            continue
        pending = [evidence.get("artifact")]
        for key in ("spec_artifacts", "issue_artifacts"):
            listed = evidence.get(key)
            if isinstance(listed, list):
                pending.extend(listed)
        for artifact in pending:
            if not isinstance(artifact, Mapping):
                continue
            reference = artifact.get("ref")
            recorded = artifact.get("sha256")
            if not isinstance(reference, str) or not isinstance(recorded, str):
                raise PermissionError("workflow_evidence_stale: malformed artifact reference in " + str(node_id))
            path = root / reference
            if not path.is_file() or path.is_symlink() or _sha(path) != recorded:
                raise PermissionError(
                    "workflow_evidence_stale: {} no longer matches the evidence recorded for {}".format(reference, node_id)
                )


def materialize_workflow_evidence(paths, plan_id: str, authorization_token: str) -> Dict[str, Any]:
    """Persist the projected evidence into the single place Monitor reads it.

    The evidence is stored under ``task_workflow[plan_id]`` so that authorizing
    one plan cannot start another.  The four V4.2 session-gate keys are left
    untouched, so the session gate keeps validating the same contract.
    """
    workflow = build_workflow_evidence(paths, plan_id, authorization_token)
    state_path = paths.vibe / "state.json"
    if state_path.is_symlink():
        raise ValueError("state.json may not be a symlink")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("state.json must contain an object")
    existing = state.get("task_workflow")
    if isinstance(existing, Mapping) and "task_id" in existing:
        # Migrate the single-object layout, preserving whatever it authorized.
        existing = {str(existing.get("task_id")): dict(existing)}
    elif not isinstance(existing, Mapping):
        existing = {}
    else:
        existing = dict(existing)
    existing[plan_id] = workflow
    state["task_workflow"] = existing
    _atomic_json(state_path, state)
    return workflow
