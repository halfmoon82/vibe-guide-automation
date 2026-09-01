"""Small runner interfaces shared by later implementation nodes."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping
from pathlib import PurePosixPath

from .models import _identifier, _json_safe
# V2 boundary contracts are re-exported here for callers that use the shared
# contracts module as their public import surface.
from .diagnostics import (
    Proposal, SkillDiagnostic, ContractCheck, PlanningGate, SessionGate,
    diagnose_skill, build_skill_reference_proposal, check_agents_contract,
    build_agentsmd_proposal, assert_planning_gate, require_execution_ready,
    screen_session, require_session_screened, validate_child_session_binding,
)
from .capability_contract import (
    CAPABILITY_STATUSES,
    CapabilityContract,
    CapabilityFact,
    build_contract,
    capability_status,
    contract_path,
    load_contract,
    save_contract,
)
from .workflow_gate import require_capability_contract, session_contract_prompt


@dataclass(frozen=True)
class RunHandle:
    run_id: str

    def __post_init__(self):
        _identifier(self.run_id, "run id")

    def to_dict(self) -> Dict[str, Any]:
        return {"run_id": self.run_id}


@dataclass(frozen=True)
class RunEvent:
    event: str
    data: Dict[str, Any]

    def __post_init__(self):
        if not isinstance(self.event, str) or not self.event:
            raise ValueError("event must be a non-empty string")
        if not isinstance(self.data, dict):
            raise TypeError("event data must be a dictionary")
        object.__setattr__(self, "data", _json_safe(self.data))

    def to_dict(self) -> Dict[str, Any]:
        return {"event": self.event, "data": _json_safe(self.data)}


class Runner:
    def start(self, contract: dict, worktree: Path) -> RunHandle:
        raise NotImplementedError

    def poll(self, handle: RunHandle) -> List[RunEvent]:
        raise NotImplementedError

    def stop(self, handle: RunHandle) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class IssueContract:
    issue_id: str
    goal: str
    non_goals: List[str]
    owned_paths: List[str]
    read_paths: List[str]
    call_chain: List[str]
    invariants: List[Dict[str, Any]]
    expected_red: str
    risk_notes: List[str]
    base_sha: str
    plan_revision: int
    execution_epoch: int
    evidence_ref: str
    allowlist: List[str] = field(default_factory=list)
    ownership: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IssueContract":
        required = {"issue_id", "goal", "non_goals", "owned_paths", "read_paths",
                    "call_chain", "invariants", "expected_red", "risk_notes", "base_sha",
                    "plan_revision", "execution_epoch", "evidence_ref"}
        optional = {"allowlist", "ownership"}
        if not isinstance(data, Mapping) or not required.issubset(set(data)) or set(data) - required - optional:
            raise ValueError("issue contract schema is invalid")
        values = dict(data)
        values.setdefault("allowlist", [])
        values.setdefault("ownership", {})
        return cls(**values)


@dataclass(frozen=True)
class ContractClosureResult:
    closed: bool
    missing: List[str]
    evidence_ref: str = ""


def check_contract_closure(issue: IssueContract, project_root: Path) -> ContractClosureResult:
    if not isinstance(issue, IssueContract):
        raise TypeError("issue contract is required")
    project_root = Path(project_root).resolve()
    missing = []
    for field_name in ("owned_paths", "read_paths", "allowlist"):
        for value in getattr(issue, field_name, []) or []:
            candidate = PurePosixPath(value) if isinstance(value, str) else None
            if candidate is None or candidate.is_absolute() or ".." in candidate.parts:
                missing.append(field_name + " path outside project: " + str(value))
    if not issue.owned_paths:
        missing.append("owned_paths")
    if not issue.call_chain:
        missing.append("call_chain")
    declared_allowlist = list(issue.allowlist or [])
    if declared_allowlist:
        allowed = set(declared_allowlist)
        for path in issue.owned_paths + issue.read_paths:
            if path not in allowed:
                missing.append("allowlist missing: " + path)
        for item in issue.call_chain:
            path = item.split(":", 1)[0] if isinstance(item, str) else ""
            if path and path not in allowed:
                missing.append("allowlist missing call_chain: " + path)
    if issue.ownership:
        if not isinstance(issue.ownership, Mapping):
            missing.append("ownership missing")
        elif "owner" in issue.ownership or "paths" in issue.ownership:
            owner = issue.ownership.get("owner")
            paths = issue.ownership.get("paths", [])
            if owner not in {issue.issue_id, "V38-2", "developer_worker"}:
                missing.append("ownership owner missing")
            if not isinstance(paths, (list, tuple)):
                missing.append("ownership paths missing")
            elif set(issue.owned_paths) - set(paths):
                missing.append("ownership paths missing: " + ",".join(sorted(set(issue.owned_paths) - set(paths))))
        else:
            for path in issue.owned_paths:
                if issue.ownership.get(path) not in {issue.issue_id, "V38-2", "developer_worker"}:
                    missing.append("ownership missing: " + path)
    invariant_ids = [item.get("id") for item in issue.invariants if isinstance(item, Mapping)]
    if len(invariant_ids) != len(set(invariant_ids)):
        missing.append("invariants duplicate id")
    for index, invariant in enumerate(issue.invariants):
        for field_name in ("id", "entrypoint", "positive_case", "negative_case", "test_command"):
            if not isinstance(invariant.get(field_name), str) or not invariant[field_name].strip():
                missing.append("invariants[%d].%s" % (index, field_name))
        entrypoint = invariant.get("entrypoint", "")
        relative = entrypoint.split(":", 1)[0] if isinstance(entrypoint, str) else ""
        if relative:
            candidate = project_root / PurePosixPath(relative)
            try:
                candidate.relative_to(project_root)
            except ValueError:
                missing.append("entrypoint outside project")
            else:
                if not candidate.is_file():
                    missing.append("entrypoint missing: " + relative)
    return ContractClosureResult(not missing, missing, issue.evidence_ref)
