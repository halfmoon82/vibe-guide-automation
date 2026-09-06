"""Implementation Brief validation before a developer's first write."""

from dataclasses import MISSING, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping

from .manifest import RunManifest
from .models import DAGNode
from .path_ownership import normalize_project_path


@dataclass(frozen=True)
class ImplementationBrief:
    issue_id: str
    goal: str
    non_goals: List[str]
    owned_paths: List[str]
    read_paths: List[str]
    call_chain: List[str]
    invariants: List[Dict[str, Any]]
    base_sha: str
    plan_revision: int
    execution_epoch: int
    evidence_ref: str
    # The extended V3.8 brief binds the implementation to the exact task and
    # call-chain evidence.  Defaults preserve the compact legacy brief shape.
    plan_id: str = ""
    expected_red: str = ""
    risk_notes: List[str] = field(default_factory=list)
    authorization_digest: str = ""
    writer: str = ""
    task_id: str = ""
    worktree: str = ""
    branch: str = ""
    allowlist: List[str] = field(default_factory=list)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ImplementationBrief":
        fields = {
            "issue_id", "goal", "non_goals", "owned_paths", "read_paths", "call_chain",
            "invariants", "base_sha", "plan_revision", "execution_epoch", "evidence_ref",
            "plan_id", "expected_red", "risk_notes", "authorization_digest", "writer",
            "task_id", "worktree", "branch", "allowlist",
        }
        legacy_fields = fields - {
            "plan_id", "expected_red", "risk_notes", "authorization_digest", "writer",
            "task_id", "worktree", "branch", "allowlist",
        }
        if not isinstance(data, Mapping) or set(data) not in (legacy_fields, fields):
            raise ValueError("implementation brief schema is invalid")
        values = dict(data)
        for name in fields - legacy_fields:
            field_info = cls.__dataclass_fields__[name]
            if field_info.default_factory is not MISSING:
                values.setdefault(name, field_info.default_factory())
            else:
                values.setdefault(name, field_info.default)
        return cls(**values)


@dataclass(frozen=True)
class BriefValidation:
    valid: bool
    missing: List[str]
    evidence: Dict[str, Any] = field(default_factory=dict)


def validate_implementation_brief(brief: ImplementationBrief, manifest: RunManifest,
                                  contract: DAGNode, project_root: Path = None) -> BriefValidation:
    missing = []
    checks: Dict[str, Any] = {}
    if brief.issue_id != contract.id:
        missing.append("issue_id")
    if brief.plan_id and brief.plan_id != manifest.plan_id:
        missing.append("plan_id")
    if brief.base_sha.lower() != manifest.base_sha.lower():
        missing.append("base_sha")
    if brief.plan_revision != manifest.plan_revision:
        missing.append("plan_revision")
    if brief.execution_epoch != manifest.execution_epoch:
        missing.append("execution_epoch")
    allowed = {normalize_project_path(item) for item in contract.allowlist}
    owned = {normalize_project_path(item) for item in contract.owned_paths}
    for name, values in (("owned_paths", brief.owned_paths), ("call_chain", brief.call_chain)):
        for value in values:
            path = value.split(":", 1)[0]
            try:
                normalized = normalize_project_path(path)
            except ValueError:
                missing.append(name + ":invalid_path")
                continue
            if normalized not in allowed and normalized not in owned:
                missing.append(name + ":outside_allowlist")
    if set(brief.owned_paths) != set(contract.owned_paths):
        missing.append("owned_paths")
    if brief.allowlist and set(brief.allowlist) != set(contract.allowlist):
        missing.append("allowlist")
    # Extended task binding checks are optional for the compact legacy brief.
    expected_contract = contract.contract if isinstance(contract.contract, dict) else {}
    for name in ("task_id", "branch", "worktree", "writer"):
        value = getattr(brief, name)
        expected = expected_contract.get(name)
        if value and expected and value != expected:
            missing.append(name)
            checks["binding." + name] = {"expected": expected, "observed": value}
    checks["binding.branch"] = {
        "expected": expected_contract.get("branch", ""), "observed": brief.branch,
    }
    for index, invariant in enumerate(brief.invariants):
        for field in ("id", "entrypoint", "positive_case", "negative_case", "test_command"):
            if not isinstance(invariant.get(field), str) or not invariant[field].strip():
                missing.append("invariants[%d].%s" % (index, field))
    if not brief.invariants:
        missing.append("invariants")
    if project_root is not None:
        root = Path(project_root)
        for index, invariant in enumerate(brief.invariants):
            entrypoint = invariant.get("entrypoint", "")
            rel = entrypoint.split(":", 1)[0] if isinstance(entrypoint, str) else ""
            try:
                path = root / normalize_project_path(rel)
            except (ValueError, TypeError):
                path = None
            if path is None or not path.is_file() or path.is_symlink():
                missing.append("invariants[%d].entrypoint" % index)
            elif ":" in entrypoint:
                symbol = entrypoint.split(":", 1)[1]
                try:
                    source = path.read_text(encoding="utf-8")
                except OSError:
                    source = ""
                if ("def " + symbol.split(".", 1)[0]) not in source:
                    missing.append("invariants[%d].entrypoint" % index)
    status = "implementing" if not missing else "brief_pending"
    evidence = {
        "status": status,
        "issue_id": brief.issue_id,
        "invariants": brief.invariants,
        "checks": checks,
    }
    return BriefValidation(not missing, sorted(set(missing)), evidence)


def require_brief_before_write(node: DAGNode, manifest: RunManifest,
                               brief: ImplementationBrief) -> None:
    validation = validate_implementation_brief(brief, manifest, node)
    if not validation.valid:
        raise ValueError("brief_pending: " + ", ".join(validation.missing))
