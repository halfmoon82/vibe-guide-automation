"""V4.5 `authorize` entry: project published artifacts into workflow evidence.

Monitor reads the mandatory ten-node workflow evidence out of ``.vibe/state.json``
and refuses to start a complex run without it, but no command ever wrote it.
This module closes that gap without weakening the gate: every node record is a
*projection of an artifact that already passed the planning gate*, and each
record carries the file path plus the SHA-256 of the bytes that were read.  No
node is ever recorded from an assumption, and `user_authorization` is recorded
only from the exact token the user supplied on the command line.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .diagnostics import assert_planning_gate, require_execution_ready
from .planner import TaskContext, classify_s0
from .workflow_gate import create_task_workflow, record_workflow_node, verify_workflow

_COMPLEX_S1 = TaskContext(5, 5, 5, 5, 5, rationale={"source": "published_complex_plan"})


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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

    s0 = classify_s0(objective or plan_id)
    workflow = create_task_workflow(plan_id, _COMPLEX_S1)

    # Each entry is (node_id, input, output, evidence).  Every evidence value
    # names the artifact it was read from, so a reviewer can re-derive it.
    steps: List[tuple] = [
        (
            "s0",
            {"plan_id": plan_id, "objective_ref": objective},
            {"simple": bool(s0.simple), "needs_s1": bool(s0.needs_s1), "route_hint": s0.route, "rationale": s0.rationale},
            {"verified_fact": "classify_s0 applied to the published PRD reference", "objective_ref": objective},
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
            {"objective_present": "目标" in prd_text, "length": len(prd_text)},
            {"verified_fact": "published prd.md read from disk", "artifact": _artifact(root, "prd.md")},
        ),
        (
            "product_decision",
            {"decision_count": len(decisions)},
            {"all_approved": True, "selected": [str(item.get("selected")) for item in decisions]},
            {"verified_fact": "every published decision is approved with selected in options", "decision_digest": str(card.get("decision_digest"))},
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
            {"verified_fact": "one reviewed spec and issue per node, no extras", "spec_count": len(spec_names), "issue_count": len(issue_names)},
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
            {"verified_fact": "the user supplied the exact AUTHORIZE token on the command line", "authorization_digest": str(card.get("digest"))},
        ),
    ]

    for node_id, input_data, output_data, evidence in steps:
        record_workflow_node(workflow, node_id, input_data, output_data, evidence)

    result = verify_workflow(workflow)
    if result.get("status") != "complete":
        raise PermissionError("required_workflow_blocked: " + str(result.get("node") or result.get("reason") or "unknown"))
    return workflow


def materialize_workflow_evidence(paths, plan_id: str, authorization_token: str) -> Dict[str, Any]:
    """Persist the projected evidence into the single place Monitor reads it.

    Only the ``task_workflow`` key is added; the four V4.2 session-gate keys are
    left byte-identical so the session gate keeps validating the same contract.
    """
    workflow = build_workflow_evidence(paths, plan_id, authorization_token)
    state_path = paths.vibe / "state.json"
    if state_path.is_symlink():
        raise ValueError("state.json may not be a symlink")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(state, dict):
        raise ValueError("state.json must contain an object")
    state["task_workflow"] = workflow
    state_path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return workflow
