"""Small runner interfaces shared by later implementation nodes."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

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
