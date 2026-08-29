"""Fail-closed V2 entry checks plus the V3 phase/planning gate."""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple
import json

try:  # V2 modules are optional in the V3 baseline checkout.
    from .diagnostics import screen_session, require_session_screened
except ImportError:  # pragma: no cover - exercised only by minimal installs
    screen_session = require_session_screened = None

from .capability_contract import CapabilityContract, capability_status, load_contract
from .models import Action, Phase, PRD


def require_capability_contract(paths) -> CapabilityContract:
    """Load the only capability fact source for an initialized V2 project."""
    try:
        return load_contract(paths)
    except (FileNotFoundError, OSError, TypeError, ValueError) as error:
        raise PermissionError(
            "capability_contract_unknown: " + type(error).__name__
        ) from error


def session_contract_prompt(contract: CapabilityContract, now=None) -> str:
    """Return a bounded prompt fragment with statuses, never raw evidence."""
    if not isinstance(contract, CapabilityContract):
        raise TypeError("contract must be a CapabilityContract")
    # Never expose an expired positive observation as still available.  The
    # effective status is evaluated at prompt construction time so both the
    # supervisor and child worker see the same stale/unknown boundary.
    statuses = {
        name: capability_status(contract, name, now=now)
        for name in sorted(contract.capabilities)
    }
    payload = {
        "contract_digest": contract.contract_digest,
        "scope": contract.scope,
        "capabilities": statuses,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    if len(encoded) > 4096:
        raise ValueError("capability contract prompt exceeds the size bound")
    return "Capability contract: " + encoded


def require_entry(paths, session_id, request, origin="user_entry"):
    state = paths.vibe / "state.json"
    if not state.is_file():
        raise PermissionError("session_gate_blocked: V2 state.json is missing")
    try:
        value = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise PermissionError("session_gate_blocked: state.json invalid") from error
    if not isinstance(value, dict) or value.get("workflow_version") != 2 or value.get("session_gate") != "s0_required":
        raise PermissionError("session_gate_blocked: V2 state metadata invalid")
    # Every V2 entry is contract-bound.  The boolean flag was introduced for
    # migration, but treating a missing flag as opt-out would let an older
    # state file bypass the evidence contract entirely.
    if value.get("workflow_version") == 2:
        require_capability_contract(paths)
    if screen_session is None or require_session_screened is None:
        raise PermissionError("session_gate_blocked: V2 diagnostics unavailable")
    gate = screen_session(paths, session_id, request, origin)
    require_session_screened(gate)
    return gate

def require_child_origin(origin):
    if origin != "worker_dispatch":
        raise PermissionError("child session must use worker_dispatch origin")


_PHASE_ORDER = (
    Phase.PRD_APPROVED,
    Phase.SPEC_ISSUE_DAG,
    Phase.DEVELOPMENT_PLAN_CONFIRMATION,
    Phase.AUTHORIZATION,
    Phase.MONITOR,
)
_LEGAL_ACTIONS = {
    Phase.PRD_APPROVED: (Action.CONTINUE_PLANNING,),
    Phase.SPEC_ISSUE_DAG: (Action.CONTINUE_PLANNING,),
    Phase.DEVELOPMENT_PLAN_CONFIRMATION: (Action.CONFIRM_PLAN,),
    Phase.AUTHORIZATION: (Action.AUTHORIZE_EXECUTION,),
    Phase.MONITOR: (),
}


def _phase(value):
    return value if isinstance(value, Phase) else Phase(value)


def _action(value):
    return value if isinstance(value, Action) else Action(value)


def legal_actions(phase):
    """Return the only user actions legal at ``phase``."""
    return _LEGAL_ACTIONS[_phase(phase)]


LEGAL_ACTIONS = {
    phase.value: tuple(action.value for action in actions)
    for phase, actions in _LEGAL_ACTIONS.items()
}


def next_phase(phase, action):
    """Validate and map one explicit action to the next phase."""
    phase, action = _phase(phase), _action(action)
    if action not in _LEGAL_ACTIONS[phase]:
        raise ValueError("action %s is not legal in phase %s" % (action.value, phase.value))
    return _PHASE_ORDER[_PHASE_ORDER.index(phase) + 1]


map_action = next_phase
allowed_actions = legal_actions


@dataclass(frozen=True)
class PlanningGate:
    status: str
    plan_id: str
    plan_revision: int
    missing: Tuple[str, ...] = ()
    evidence_refs: Tuple[str, ...] = ()
    run_created: bool = False
    worker_created: bool = False
    handoff: Any = None
    context: Dict[str, Any] = field(default_factory=dict)

    @property
    def ready(self):
        return self.status in {"ready_for_plan_confirmation", "ready_for_authorization", "monitor_ready"}

    @property
    def missing_artifacts(self):
        return self.missing

    def to_dict(self):
        return {
            "status": self.status,
            "plan_id": self.plan_id,
            "plan_revision": self.plan_revision,
            "missing": list(self.missing),
            "evidence_refs": list(self.evidence_refs),
            "run_created": self.run_created,
            "worker_created": self.worker_created,
            "context": self.context,
        }


def _artifact_value(source, name):
    if isinstance(source, Mapping):
        return source.get(name)
    path = Path(source)
    candidates = {
        "prd": ("prd.md",),
        "spec": ("spec.md", "specs"),
        "issue": ("issue.md", "issues"),
        "dag_audit": ("dag-audit.json",),
        "plan_confirmation": ("plan-confirmation.json",),
    }[name]
    for candidate in candidates:
        target = path / candidate
        if target.is_file() or target.is_dir():
            if target.is_file():
                try:
                    return json.loads(target.read_text(encoding="utf-8")) if target.suffix == ".json" else target.read_text(encoding="utf-8")
                except (OSError, ValueError, json.JSONDecodeError):
                    return None
            try:
                # A directory's mere existence/non-emptiness is not evidence.
                # Only readable Markdown artifacts can satisfy the gate.
                contents = []
                for item in sorted(target.iterdir()):
                    if item.is_file() and item.suffix.lower() == ".md":
                        try:
                            contents.append(item.read_text(encoding="utf-8"))
                        except (OSError, UnicodeDecodeError):
                            return None
                return contents
            except OSError:
                return None
    return None


def _published_reviewed(value):
    if isinstance(value, list):
        return bool(value) and all(_published_reviewed(item) for item in value)
    if isinstance(value, Mapping):
        return value.get("status") in {"published", "reviewed", "approved"} and value.get("reviewed", True) is not False
    if not isinstance(value, str):
        return bool(value)
    text = value.lower()
    return ("status:" in text or "状态：" in value or "状态:" in value) and "published" in text and ("review" in text or "审核：" in value or "审核:" in value)


def evaluate_planning_gate(prd: PRD, artifacts=None, plan_id="unplanned", plan_revision=1, for_confirmation=False, execution_context=None):
    """Read planning evidence and fail closed before any run/Worker side effect."""
    if not isinstance(prd, PRD):
        raise TypeError("prd must be a PRD")
    from .stage_handoff import build_stage_handoff
    if prd.status != "approved":
        handoff = build_stage_handoff(Phase.PRD_APPROVED.value, "planning_required", plan_id, plan_revision, ("prd",), required_user_action="answer_question", open_questions=("PRD 必须先获批",), context=execution_context)
        return PlanningGate("planning_required", plan_id, plan_revision, ("prd.approved",), ("prd",), handoff=handoff, context=dict(execution_context or {}))
    missing: List[str] = []
    values = {name: _artifact_value(artifacts, name) if artifacts is not None else None for name in ("prd", "spec", "issue", "dag_audit", "plan_confirmation")}
    if artifacts is not None and values["prd"] is None:
        # A PRD object supplied by the caller is itself the approved PRD evidence.
        values["prd"] = True
    for name in ("spec", "issue"):
        value = values[name]
        if not _published_reviewed(value):
            missing.append(name)
    audit = values["dag_audit"]
    if not isinstance(audit, dict) or audit.get("status") not in {"reviewed", "audited", "pass", "passed"}:
        missing.append("dag-audit")
    elif str(audit.get("plan_revision", plan_revision)) != str(plan_revision):
        missing.append("dag-audit.plan_revision")
    confirmation = values["plan_confirmation"]
    if confirmation is None and for_confirmation and not missing:
        handoff = build_stage_handoff(Phase.DEVELOPMENT_PLAN_CONFIRMATION.value, "ready_for_plan_confirmation", plan_id, plan_revision, ("prd", "spec", "issue", "dag-audit"), required_user_action=Action.CONFIRM_PLAN.value, context=execution_context)
        return PlanningGate("ready_for_plan_confirmation", plan_id, plan_revision, (), ("prd", "spec", "issue", "dag-audit"), False, False, handoff, dict(execution_context or {}))
    if confirmation is None:
        missing.append("plan-confirmation")
    else:
        if not isinstance(confirmation, dict) or confirmation.get("status") != "confirmed":
            missing.append("plan-confirmation")
        elif str(confirmation.get("plan_revision", plan_revision)) != str(plan_revision):
            missing.append("plan-confirmation.plan_revision")
    if missing:
        missing = tuple(dict.fromkeys(missing))
        handoff = build_stage_handoff(Phase.SPEC_ISSUE_DAG.value, "planning_required", plan_id, plan_revision, ("prd",), open_questions=("补齐：" + ", ".join(missing),), context=execution_context)
        return PlanningGate("planning_required", plan_id, plan_revision, missing, ("prd",), False, False, handoff, dict(execution_context or {}))
    status = "ready_for_authorization" if confirmation is not None else "ready_for_plan_confirmation"
    refs = ("prd", "spec", "issue", "dag-audit", "plan-confirmation")
    handoff = build_stage_handoff(Phase.AUTHORIZATION.value, status, plan_id, plan_revision, refs, required_user_action=Action.AUTHORIZE_EXECUTION.value, context=execution_context)
    return PlanningGate(status, plan_id, plan_revision, (), refs, False, False, handoff, dict(execution_context or {}))


def require_planning_gate(*args, **kwargs):
    result = evaluate_planning_gate(*args, **kwargs)
    if result.status == "planning_required":
        raise PermissionError("planning_required: " + ", ".join(result.missing))
    return result


planning_gate = evaluate_planning_gate
