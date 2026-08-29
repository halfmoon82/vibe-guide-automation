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
            "handoff": self.handoff.to_dict() if hasattr(self.handoff, "to_dict") else self.handoff,
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


def _explicit_revision(value):
    """Read a revision only when an artifact explicitly records one."""
    if isinstance(value, Mapping):
        return value.get("plan_revision", value.get("revision"))
    if isinstance(value, str):
        import re
        match = re.search(r"(?:plan[ _-]revision|revision|版本)\s*[:：@]?\s*(\d+)", value, re.I)
        return int(match.group(1)) if match else None
    return None


def _published_reviewed(value):
    if isinstance(value, list):
        return bool(value) and all(_published_reviewed(item) for item in value)
    if isinstance(value, Mapping):
        status = value.get("status")
        if not isinstance(status, str):
            return False
        status = status.strip().casefold()
        if status in {"approved", "已批准"}:
            return True
        if status in {"reviewed", "已审核"}:
            return True
        if status not in {"published", "已发布"}:
            return False
        # Published evidence must carry an explicit review marker.  A missing
        # marker is not equivalent to an affirmative one.
        reviewed = value.get("reviewed")
        return reviewed is True or (isinstance(reviewed, str) and reviewed.strip().casefold() in {"true", "yes", "reviewed", "已审核"})
    if not isinstance(value, str):
        # Evidence must carry a structured or textual status field; truthy
        # sentinels are not an acceptable substitute.
        return False
    import re
    status_match = re.search(r"(?im)^[ \t]*(?:status|状态)[ \t]*[:：][ \t]*([^\r\n]+?)[ \t]*$", value)
    if not status_match:
        return False
    status = status_match.group(1).strip().casefold()
    if status in {"approved", "已批准"}:
        return True
    if status in {"reviewed", "已审核"}:
        return True
    if status not in {"published", "已发布"}:
        return False
    # Match a dedicated marker line, rather than any occurrence of review or
    # approved in the document body.
    return bool(re.search(r"(?im)^[ \t]*(?:reviewed|review|审核)[ \t]*[:：][ \t]*(?:true|yes|reviewed|已审核)[ \t]*$", value))


def evaluate_planning_gate(prd: PRD, artifacts=None, plan_id="unplanned", plan_revision=1, for_confirmation=False, execution_context=None):
    """Read planning evidence and fail closed before any run/Worker side effect."""
    if not isinstance(prd, PRD):
        raise TypeError("prd must be a PRD")
    if isinstance(plan_revision, bool) or not isinstance(plan_revision, int) or plan_revision < 1:
        raise ValueError("plan_revision must be a positive integer")
    from .stage_handoff import build_stage_handoff
    if prd.status != "approved":
        handoff_status = prd.status if prd.status in {"blocked_design", "blocked_unknown"} else "planning_required"
        handoff = build_stage_handoff(Phase.PRD_APPROVED.value, handoff_status, plan_id, plan_revision, ("prd",), required_user_action="answer_question", open_questions=("PRD 必须先获批",), context=execution_context)
        return PlanningGate("planning_required", plan_id, plan_revision, ("prd.approved",), ("prd",), handoff=handoff, context=dict(execution_context or {}))
    missing: List[str] = []
    values = {name: _artifact_value(artifacts, name) if artifacts is not None else None for name in ("prd", "spec", "issue", "dag_audit", "plan_confirmation")}
    prd_artifact_present = artifacts is not None and values["prd"] is not None
    if artifacts is not None and values["prd"] is None:
        # A PRD object supplied by the caller is itself the approved PRD evidence.
        values["prd"] = True
    if prd.revision != plan_revision:
        missing.append("prd.revision")
    artifact_revision = _explicit_revision(values["prd"])
    if prd_artifact_present and artifact_revision is None:
        missing.append("prd.revision")
    elif artifact_revision is not None and artifact_revision != plan_revision:
        missing.append("prd.revision")
    if values["prd"] is not True and values["prd"] is not None and not _published_reviewed(values["prd"]):
        missing.append("prd")
    for name in ("spec", "issue"):
        value = values[name]
        if not _published_reviewed(value):
            missing.append(name)
    audit = values["dag_audit"]
    if not isinstance(audit, dict) or audit.get("status") not in {"reviewed", "audited", "pass", "passed"}:
        missing.append("dag-audit")
    elif "plan_revision" not in audit:
        missing.append("dag-audit.plan_revision")
    elif str(audit.get("plan_revision")) != str(plan_revision):
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
        elif "plan_revision" not in confirmation:
            missing.append("plan-confirmation.plan_revision")
        elif str(confirmation.get("plan_revision")) != str(plan_revision):
            missing.append("plan-confirmation.plan_revision")
    if missing:
        missing = tuple(dict.fromkeys(missing))
        handoff_stage = Phase.DEVELOPMENT_PLAN_CONFIRMATION.value if missing == ("plan-confirmation",) else Phase.SPEC_ISSUE_DAG.value
        handoff_action = Action.CONFIRM_PLAN.value if handoff_stage == Phase.DEVELOPMENT_PLAN_CONFIRMATION.value else Action.CONTINUE_PLANNING.value
        handoff = build_stage_handoff(handoff_stage, "planning_required", plan_id, plan_revision, ("prd",), required_user_action=handoff_action, open_questions=("补齐：" + ", ".join(missing),), context=execution_context)
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

# Explicit names make the two independent layers discoverable to callers while
# preserving the historical aliases used by V2 integrations.
stage_action_map = LEGAL_ACTIONS
phase_action_map = LEGAL_ACTIONS
