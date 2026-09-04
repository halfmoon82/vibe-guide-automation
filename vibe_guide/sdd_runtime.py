"""V4 node-local safe write gates (provider evidence remains advisory)."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

from .models import DAGNode, V4ExecutionPolicy
from .path_ownership import normalize_allowlist
from .task_registry import claim_v4_writer


@dataclass(frozen=True)
class WriteGateResult:
    valid: bool
    reasons: List[str] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"valid": self.valid, "reasons": list(self.reasons), "observations": list(self.observations)}


def prepare_issue_workspace(project_root: Path, issue_id: str, requested_root: Path) -> Path:
    """Create an Issue workspace below a project-owned managed root."""
    root = Path(project_root).resolve()
    managed = Path(requested_root).resolve()
    try:
        managed.relative_to(root)
    except ValueError as exc:
        raise ValueError("workspace must be inside project root") from exc
    if managed == root:
        raise ValueError("workspace managed root cannot be project root")
    if not issue_id or issue_id in {".", ".."} or "/" in issue_id or "\\" in issue_id:
        raise ValueError("issue_id is invalid")
    target = managed / issue_id
    # Never follow a pre-existing Issue symlink; doing so could redirect
    # worker writes outside the controlled managed root.
    if target.is_symlink():
        raise ValueError("workspace symlink escape")
    target.mkdir(parents=True, exist_ok=True)
    resolved = target.resolve()
    try:
        resolved.relative_to(managed)
    except ValueError as exc:
        raise ValueError("workspace symlink escape") from exc
    if resolved == root or resolved == managed:
        raise ValueError("workspace cannot be project root")
    return resolved


def validate_sdd_write_gate(policy: V4ExecutionPolicy, node: DAGNode, active_writers: Dict[str, str]) -> WriteGateResult:
    reasons: List[str] = []
    observations: List[str] = []
    if not isinstance(policy, V4ExecutionPolicy) or not isinstance(node, DAGNode):
        return WriteGateResult(False, ["invalid_contract"], observations)
    contract = node.contract if isinstance(node.contract, dict) else {}
    frozen = policy.node_contracts.get(node.id, {}) if isinstance(policy.node_contracts, dict) else {}
    ownership_fields = ("project_root", "worktree", "allowlist", "writer", "issue_id")
    top_level_complete = all(getattr(policy, field, "") not in (None, "", []) for field in ownership_fields)
    frozen_complete = all(frozen.get(field) not in (None, "", []) for field in ownership_fields)
    if not top_level_complete and not frozen_complete:
        reasons.append("policy_ownership_missing")
    # A policy is frozen at authorization time.  Any explicit claim present in
    # both policy and node contract must match before a writer can be reached.
    checks = {
        "project_root": (policy.project_root or frozen.get("project_root"), contract.get("project_root")),
        "worktree": (policy.worktree or frozen.get("worktree"), contract.get("worktree", node.worktree)),
        "allowlist": (policy.allowlist or frozen.get("allowlist"), contract.get("allowlist", node.allowlist)),
        "writer": (policy.writer or frozen.get("writer"), node.writer or contract.get("writer")),
        "issue_id": (policy.issue_id or frozen.get("issue_id"), contract.get("issue_id", node.id)),
        # plan_revision is only a frozen field when explicitly projected in a
        # node scope; the dataclass default preserves legacy callers.
        "plan_revision": (frozen.get("plan_revision"), contract.get("plan_revision")),
    }
    for field, (expected, actual) in checks.items():
        if expected not in (None, "", []) and actual in (None, "", []):
            reasons.append("policy_contract_mismatch:" + field)
            continue
        if expected in (None, "", []) or actual in (None, "", []):
            continue
        if field in {"project_root", "worktree"}:
            try:
                if str(Path(expected).resolve()) != str(Path(actual).resolve()):
                    reasons.append("policy_contract_mismatch:" + field)
            except (TypeError, ValueError, OSError):
                reasons.append("policy_contract_mismatch:" + field)
        elif field == "allowlist":
            try:
                if tuple(normalize_allowlist(expected)) != tuple(normalize_allowlist(actual)):
                    reasons.append("policy_contract_mismatch:allowlist")
            except (TypeError, ValueError):
                reasons.append("policy_contract_mismatch:allowlist")
        elif str(expected) != str(actual):
            reasons.append("policy_contract_mismatch:" + field)
    project_root = contract.get("project_root")
    worktree = contract.get("worktree", node.worktree)
    if not project_root or not worktree:
        reasons.append("workspace_missing")
    else:
        try:
            project_path = Path(project_root).resolve()
            worktree_path = Path(worktree).resolve()
            if worktree_path == project_path:
                reasons.append("host_checkout_write")
            else:
                worktree_path.relative_to(project_path)
        except (TypeError, ValueError):
            # Malformed roots are a structured safety failure, never an
            # exception that could accidentally reach a writer.
            reasons.append("workspace_invalid")
    try:
        normalize_allowlist(contract.get("allowlist", node.allowlist))
    except (TypeError, ValueError):
        reasons.append("allowlist_escape")
    role = contract.get("role", "developer")
    if role == "reviewer" or contract.get("writer_role") == "reviewer":
        reasons.append("reviewer_write")
    run_id = contract.get("run_id", "")
    writer = node.writer or contract.get("writer")
    if not writer:
        reasons.append("writer_missing")
    elif active_writers.get(node.id) not in (None, writer):
        reasons.append("duplicate_writer")
    elif any(issue != node.id and owner == writer for issue, owner in (active_writers or {}).items()):
        reasons.append("duplicate_writer")
    # Legacy callers have also supplied the inverse shape ``writer -> issue``.
    # Reconcile both forms before allowing a claim, so an old snapshot cannot
    # override the durable reverse claim maintained by the V4 registry.
    elif active_writers.get(writer) not in (None, node.id):
        reasons.append("duplicate_writer")
    # Claims are acquired only after every local hard gate passes and only by
    # developer nodes.  In particular, reviewer attempts must not poison the
    # claim registry used by a subsequent developer attempt.
    elif not reasons and role == "developer" and run_id and not claim_v4_writer(run_id, node.id, writer):
        reasons.append("duplicate_writer")
    if not contract.get("lease"):
        observations.append("missing_provider_lease")
    if not contract.get("cursor"):
        observations.append("missing_provider_cursor")
    return WriteGateResult(not reasons, reasons, observations)
