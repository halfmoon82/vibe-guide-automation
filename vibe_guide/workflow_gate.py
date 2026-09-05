"""Fail-closed shared entry gate for V2 direct APIs."""
from .diagnostics import screen_session, require_session_screened
from .capability_contract import CapabilityContract, capability_status, load_contract
import json
from copy import deepcopy

REQUIRED_COMPLEX_WORKFLOW = [
    "s0", "s1", "requirements", "product_decision", "prd", "spec_issue",
    "dag_audit", "plan_confirmation", "authorization_card", "user_authorization",
]
_REMOTE_GIT_ACTIONS = {"commit", "push", "pr", "mr", "create_pr", "create_mr", "merge"}


def create_task_workflow(task_id, context):
    """Create an isolated, task-scoped required workflow projection."""
    from .planner import route_task
    route = route_task(context)
    route_name = route.route if hasattr(route, "route") else route
    nodes = list(REQUIRED_COMPLEX_WORKFLOW if route_name == "complex" else ["s0", "s1"])
    return {
        "task_id": str(task_id), "route": route_name, "nodes": nodes,
        "node_records": {}, "authorization_granted": False,
    }


def record_workflow_node(workflow, node_id, input_data, output_data, evidence, status="completed", self_assessment=None):
    """Record one node; evidence is mandatory and self-reports are non-authoritative."""
    if node_id not in workflow.get("nodes", []):
        raise ValueError("node is not applicable to this task")
    if not isinstance(input_data, dict) or not isinstance(output_data, dict) or not isinstance(evidence, dict):
        raise ValueError("workflow node requires structured input, output and evidence")
    records = workflow.setdefault("node_records", {})
    if node_id in records:
        raise ValueError("workflow node record is immutable; append a new task revision")
    workflow["_sequence"] = int(workflow.get("_sequence", 0)) + 1
    records[node_id] = {"task_id": workflow["task_id"], "node_id": node_id, "status": status, "input": deepcopy(input_data), "output": deepcopy(output_data), "evidence": deepcopy(evidence), "sequence": workflow["_sequence"]}
    if self_assessment is not None:
        records[node_id]["self_assessment"] = str(self_assessment)
    if node_id == "user_authorization" and status in {"completed", "accepted", "passed"}:
        workflow["authorization_granted"] = True
    return records[node_id]


def skip_workflow_node(workflow, node_id, user_instruction, reason, impact, allow_successor):
    if node_id not in workflow.get("nodes", []):
        raise ValueError("node is not applicable to this task")
    if node_id in workflow.setdefault("node_records", {}):
        raise ValueError("workflow node record is immutable; append a new task revision")
    if not all(isinstance(value, str) and value.strip() for value in (user_instruction, reason, impact)):
        raise ValueError("skip requires original instruction, reason and impact")
    workflow["_sequence"] = int(workflow.get("_sequence", 0)) + 1
    workflow.setdefault("node_records", {})[node_id] = {
        "task_id": workflow["task_id"], "node_id": node_id, "status": "skipped_by_user", "user_instruction": user_instruction,
        "reason": reason, "impact": impact, "allow_successor": bool(allow_successor),
        "sequence": workflow["_sequence"],
    }
    if node_id in {"authorization_card", "user_authorization"}:
        workflow["authorization_granted"] = False
    return workflow["node_records"][node_id]


def verify_workflow(workflow):
    """Monitor-side hard gate for order, evidence, and explicit skips."""
    if not isinstance(workflow, dict) or not isinstance(workflow.get("task_id"), str) or not workflow.get("task_id", "").strip():
        return {"status": "blocked_by_required_node", "reason": "workflow identity is missing"}
    nodes = workflow.get("nodes", [])
    records = workflow.get("node_records", {})
    if not isinstance(nodes, list) or not nodes or not all(isinstance(node, str) and node.strip() for node in nodes):
        return {"status": "blocked_by_required_node", "reason": "workflow nodes are missing"}
    if not isinstance(records, dict) or set(records) - set(nodes):
        return {"status": "blocked_by_required_node", "reason": "workflow records are out of scope"}
    from .planner import required_workflow_nodes
    expected_nodes = required_workflow_nodes(workflow.get("route", "complex"))
    if nodes != expected_nodes:
        return {"status": "blocked_by_required_node", "reason": "workflow node sequence is invalid"}
    for index, node_id in enumerate(nodes):
        record = records.get(node_id)
        if not isinstance(record, dict):
            return {"status": "blocked_by_required_node", "node": node_id}
        if record.get("status") == "skipped_by_user":
            if record.get("task_id") != workflow.get("task_id") or record.get("node_id") != node_id or not all(isinstance(record.get(key), str) and record[key].strip() for key in ("user_instruction", "reason", "impact")):
                return {"status": "blocked_by_required_node", "node": node_id, "reason": "invalid skip credential"}
            if index < len(nodes) - 1 and not record.get("allow_successor"):
                return {"status": "blocked_by_required_node", "node": node_id}
            continue
        if record.get("status") not in {"completed", "accepted", "passed"}:
            return {"status": "blocked_by_required_node", "node": node_id}
        if record.get("task_id") != workflow.get("task_id") or record.get("node_id") != node_id:
            return {"status": "blocked_by_required_node", "node": node_id, "reason": "lineage mismatch"}
        if not all(isinstance(record.get(key), dict) and record[key] for key in ("input", "output", "evidence")):
            return {"status": "blocked_by_required_node", "node": node_id}
        if not isinstance(record.get("sequence"), int) or record.get("sequence") <= 0:
            return {"status": "blocked_by_required_node", "node": node_id, "reason": "invalid sequence"}
        if index and record.get("sequence", 0) <= records.get(nodes[index - 1], {}).get("sequence", 0):
            return {"status": "blocked_by_required_node", "node": node_id, "reason": "order violation"}
    if any(records.get(node, {}).get("status") == "skipped_by_user" for node in ("authorization_card", "user_authorization")):
        return {"status": "blocked_by_required_node", "reason": "authorization was skipped"}
    if workflow.get("route") == "complex" and not bool(workflow.get("authorization_granted")):
        return {"status": "blocked_by_required_node", "reason": "user authorization evidence is missing"}
    return {"status": "complete", "authorization_granted": bool(workflow.get("authorization_granted"))}


# Explicit names used by integrations and tests.
start_task_workflow = create_task_workflow
monitor_verify_workflow = verify_workflow


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
    gate = screen_session(paths, session_id, request, origin)
    require_session_screened(gate)
    return gate

def require_child_origin(origin):
    if origin != "worker_dispatch":
        raise PermissionError("child session must use worker_dispatch origin")
