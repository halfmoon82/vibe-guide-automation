"""Fail-closed shared entry gate for V2 direct APIs."""
from .diagnostics import screen_session, require_session_screened
from .capability_contract import CapabilityContract, capability_status, load_contract
from .diagnostics import SessionGate
from .session_bypass import BypassError, consume_bypass, is_bypass_valid, load_challenge
import json


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


def require_entry(paths, session_id, request, origin="user_entry", now=None):
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
    if origin == "worker_dispatch" and isinstance(request, str) and request.startswith("BYPASS VIBE"):
        raise PermissionError("session_bypass_rejected: child session cannot request bypass")
    if origin == "user_entry":
        if isinstance(request, str) and request.startswith("BYPASS VIBE"):
            try:
                consume_bypass(paths, session_id, request, now=now, origin=origin)
            except (BypassError, OSError, TypeError, ValueError) as error:
                raise PermissionError("session_bypass_rejected") from error
            return SessionGate("wizard_bypassed", session_id, request, origin)
        try:
            existing = load_challenge(paths, session_id)
        except BypassError as error:
            raise PermissionError("session_bypass_rejected") from error
        if existing is not None and is_bypass_valid(existing, session_id, now=now):
            return SessionGate("wizard_bypassed", session_id, request, origin)
    gate = screen_session(paths, session_id, request, origin)
    require_session_screened(gate)
    return gate

def require_child_origin(origin, request=None):
    if origin != "worker_dispatch":
        raise PermissionError("child session must use worker_dispatch origin")
    if isinstance(request, str) and request.startswith("BYPASS VIBE"):
        raise PermissionError("session_bypass_rejected: child session cannot request bypass")
