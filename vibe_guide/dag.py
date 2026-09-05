"""DAG validation, ready-node scheduling, and plan artifact rendering."""

from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .models import DAGNode, Plan


INTEGRATION_REVIEW_NODE_ID = "integration-review"
INTEGRATION_REVIEWER_ID = "integration-reviewer"


def is_integration_review_node(node: DAGNode) -> bool:
    """Return whether *node* is the reserved final integration reviewer node."""
    return isinstance(node, DAGNode) and node.id == INTEGRATION_REVIEW_NODE_ID


def _integration_nodes(plan: Plan) -> List[DAGNode]:
    return [node for node in (getattr(plan, "nodes", []) or []) if is_integration_review_node(node)]


def append_integration_review_node(plan: Plan) -> Plan:
    """Append the deterministic, read-only integration review node to complex plans."""
    if not isinstance(plan, Plan):
        raise TypeError("plan is required")
    if plan.complexity_band != "complex":
        return plan
    existing = _integration_nodes(plan)
    if existing:
        raise ValueError("plan already contains an integration review node")
    # Import lazily to keep the planner/DAG modules independently importable.
    from .planner import build_integration_acceptance_contract

    business_nodes = [node for node in plan.nodes if not is_integration_review_node(node)]
    projected_contract = build_integration_acceptance_contract(plan)
    contract = dict(projected_contract)
    contract.update({
        "input": "all business deliveries, review/rework evidence, and aggregate diff",
        "output": "integration review report with P0/P1/P2 clearance and evidence references",
        "error_behavior": "unknown, out-of-scope changes, or uncleared findings block acceptance",
        "acceptance_example": "all required evidence is present and P0/P1/P2 clearance is zero",
        "risk_tags": ["integration", "read-only"],
        "read_only": True,
        "reviewer": INTEGRATION_REVIEWER_ID,
        "allowlist": [],
    })
    integration = DAGNode(
        INTEGRATION_REVIEW_NODE_ID,
        "Final integration review",
        [node.id for node in business_nodes],
        [],
        "integration",
        contract,
        "planned",
        risk_tags=["integration", "read-only"],
        reviewer=INTEGRATION_REVIEWER_ID,
        owned_paths=[],
        allowlist=[],
    )
    return replace(
        plan,
        node_ids=list(plan.node_ids) + [integration.id],
        nodes=list(plan.nodes) + [integration],
    )


def validate_integration_review_node(plan: Plan) -> "DAGValidation":
    """Validate the integration node's uniqueness, scope, lineage and reviewer isolation."""
    if not isinstance(plan, Plan):
        raise TypeError("plan is required")
    if plan.complexity_band != "complex":
        return DAGValidation(True, ())
    nodes = list(plan.nodes or [])
    integration = _integration_nodes(plan)
    errors: List[str] = []
    if len(integration) != 1:
        errors.append("complex plan must contain exactly one integration review node")
        return DAGValidation(False, tuple(errors))
    node = integration[0]
    business = [item for item in nodes if not is_integration_review_node(item)]
    business_ids = [item.id for item in business]
    contract_error = _contract_error(node)
    if contract_error:
        errors.append(contract_error)
    if node.depends_on != business_ids:
        errors.append("integration review depends_on must equal all business node IDs in plan order")
    if node.owned_paths:
        errors.append("integration review node must not own business paths")
    if node.allowlist:
        errors.append("integration review node must have an empty write allowlist")
    if node.contract.get("allowlist"):
        errors.append("integration review contract must have an empty write allowlist")
    explicit_reviewer = node.reviewer
    contract_reviewer = node.contract.get("reviewer")
    if explicit_reviewer and contract_reviewer and explicit_reviewer != contract_reviewer:
        errors.append("integration review reviewer mismatch between node and contract")
    reviewer = explicit_reviewer or contract_reviewer
    business_reviewers = {item.reviewer or item.contract.get("reviewer") for item in business}
    business_writers = {item.writer or item.contract.get("writer") for item in business}
    if reviewer != INTEGRATION_REVIEWER_ID:
        errors.append("integration review reviewer must be the independent integration reviewer")
    if reviewer in business_reviewers or reviewer in business_writers:
        errors.append("integration review reviewer must not be reused by a business node")
    if node.contract.get("read_only") is not True:
        errors.append("integration review contract must be read-only")
    try:
        from .planner import build_integration_acceptance_contract
        expected = build_integration_acceptance_contract(plan)
        if node.contract.get("digest") != expected.get("digest"):
            errors.append("integration review contract digest mismatch")
    except (TypeError, ValueError) as exc:
        errors.append("integration review contract is invalid: {}".format(exc))
    return DAGValidation(not errors, tuple(dict.fromkeys(errors)))


@dataclass(frozen=True)
class DAGValidation:
    valid: bool
    errors: Tuple[str, ...]


@dataclass(frozen=True)
class PlanArtifacts:
    dag_path: Path
    plan_path: Path


@dataclass(frozen=True)
class DAGAuditResult:
    """Evidence-bounded result of auditing a plan's executable DAG."""

    status: str
    ready_nodes: List[str]
    blocked_nodes: List[str]
    reasons: Dict[str, List[str]]
    parallel_groups: Optional[Dict[str, List[str]]] = None

    def __post_init__(self):
        if self.status not in {"ready", "blocked_design", "blocked_dag", "blocked_unknown"}:
            raise ValueError("unsupported DAG audit status")
        object.__setattr__(self, "ready_nodes", list(self.ready_nodes))
        object.__setattr__(self, "blocked_nodes", list(self.blocked_nodes))
        object.__setattr__(self, "reasons", {
            str(key): list(value) for key, value in self.reasons.items()
        })
        object.__setattr__(self, "parallel_groups", {
            str(key): list(value) for key, value in (self.parallel_groups or {}).items()
        })

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ready_nodes": list(self.ready_nodes),
            "blocked_nodes": list(self.blocked_nodes),
            "reasons": {key: list(value) for key, value in self.reasons.items()},
            "parallel_groups": {key: list(value) for key, value in self.parallel_groups.items()},
        }


_CONTRACT_FIELDS = (
    ("input", "inputs"),
    ("output", "outputs"),
    ("error_behavior", "errors"),
    ("acceptance_example", "acceptance_examples"),
)


def _has_content(value: object) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value) and all(_has_content(key) and _has_content(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_has_content(item) for item in value)
    return bool(value)


def _has_value(contract: Mapping[str, object], names: Sequence[str]) -> bool:
    return any(name in contract and _has_content(contract[name]) for name in names)


def _contract_error(node: DAGNode) -> str:
    contract = node.contract
    if not isinstance(contract, Mapping):
        return "node {} contract must be a mapping".format(node.id)
    if contract.get("design_change") or contract.get("status") == "blocked_design":
        return "node {} is blocked by a design change".format(node.id)
    missing = [names[0] for names in _CONTRACT_FIELDS if not _has_value(contract, names)]
    if missing:
        return "node {} contract is missing {}".format(node.id, ", ".join(missing))
    return ""


def _metadata_sources(node: DAGNode, name: str) -> List[Tuple[str, Any]]:
    """Return explicit, legacy, and authoritative metadata claims."""
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    profile = contract.get("worker_profile")
    profile_value = profile.get(name) if isinstance(profile, Mapping) else None
    return [
        ("explicit", getattr(node, name, None)),
        ("contract", contract.get(name)),
        ("worker_profile", profile_value),
    ]


def _node_metadata(node: DAGNode, name: str) -> Any:
    for _source, value in _metadata_sources(node, name):
        if value not in (None, "", []):
            return value
    return None


def _audit_contract_errors(node: DAGNode) -> List[str]:
    """Validate executable contract and its writer identity claims."""
    errors: List[str] = []
    contract_error = _contract_error(node)
    if contract_error:
        errors.append(contract_error)
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    aliases = {
        "input": ("input", "inputs"),
        "output": ("output", "outputs"),
        "error_behavior": ("error_behavior", "errors"),
        "acceptance_examples": ("acceptance_example", "acceptance_examples"),
    }
    for label, names in aliases.items():
        if not _has_value(contract, names):
            errors.append("node {} contract is missing {}".format(node.id, label))

    profile = contract.get("worker_profile")
    if profile is not None and not isinstance(profile, Mapping):
        errors.append("node {} worker_profile must be a mapping".format(node.id))
        profile = None
    if isinstance(profile, Mapping):
        for field_name in ("writer", "allowlist"):
            if field_name not in profile or not _has_content(profile[field_name]):
                errors.append("node {} worker_profile is missing {}".format(node.id, field_name))

    risk_tags = _node_metadata(node, "risk_tags")
    if not isinstance(risk_tags, list) or not risk_tags or not all(
        isinstance(item, str) and item.strip() for item in risk_tags
    ):
        errors.append("node {} contract is missing risk_tags".format(node.id))
    writer = _node_metadata(node, "writer")
    if not isinstance(writer, str) or not writer.strip():
        errors.append("node {} contract is missing writer".format(node.id))
    worktree = _node_metadata(node, "worktree")
    if not isinstance(worktree, str) or not worktree.strip():
        errors.append("node {} contract is missing worktree".format(node.id))
    allowlist = _node_metadata(node, "allowlist")
    if not isinstance(allowlist, list) or not allowlist or not all(
        isinstance(item, str) and item.strip() for item in allowlist
    ):
        errors.append("node {} contract is missing allowlist".format(node.id))
    elif any(Path(item).is_absolute() or ".." in Path(item).parts for item in allowlist):
        errors.append("node {} allowlist escapes the project".format(node.id))

    # If multiple representations are present, they must make the same claim.
    for name in ("risk_tags", "writer", "worktree", "allowlist"):
        populated = [
            (source, value) for source, value in _metadata_sources(node, name)
            if value not in (None, "", [])
        ]
        for index, (left_source, left_value) in enumerate(populated):
            for right_source, right_value in populated[index + 1:]:
                if left_value != right_value:
                    errors.append("node {} {} mismatch ({} vs {})".format(
                        node.id, name, left_source, right_source
                    ))
    if contract.get("design_change") or contract.get("status") == "blocked_design":
        errors.append("node {} is blocked by a design change".format(node.id))
    return list(dict.fromkeys(errors))


def _structural_errors(nodes: List[DAGNode]) -> List[str]:
    errors = []
    identifiers = [node.id for node in nodes]
    if len(identifiers) != len(set(identifiers)):
        errors.append("DAG node IDs must be unique")

    node_ids = set(identifiers)
    graph = {}
    for node in nodes:
        graph[node.id] = list(node.depends_on)
        missing = [dependency for dependency in node.depends_on if dependency not in node_ids]
        if missing:
            errors.append("node {} has unknown hard dependencies: {}".format(node.id, ", ".join(missing)))

    visiting = set()
    visited = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for dependency in graph.get(node_id, []):
            if dependency in graph and visit(dependency):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    if any(visit(node_id) for node_id in graph if node_id not in visited):
        errors.append("DAG hard dependencies contain a cycle")
    return errors


def validate_dag(nodes: List[DAGNode]) -> DAGValidation:
    errors = _structural_errors(nodes)
    errors.extend(error for node in nodes for error in (_contract_error(node),) if error)
    return DAGValidation(not errors, tuple(errors))


def ready_nodes(nodes: List[DAGNode]) -> List[DAGNode]:
    if _structural_errors(nodes):
        return []
    by_id: Dict[str, DAGNode] = {node.id: node for node in nodes}
    ready = []
    for node in nodes:
        if node.status not in ("planned", "ready"):
            continue
        if _contract_error(node):
            continue
        if all(by_id[dependency].status == "accepted" for dependency in node.depends_on):
            ready.append(node)
    return ready


def _cycle_nodes(nodes: List[DAGNode]) -> List[str]:
    """Return nodes participating in hard-dependency cycles."""
    graph = {node.id: list(node.depends_on) for node in nodes}
    state: Dict[str, int] = {}
    stack: List[str] = []
    found = set()

    def visit(node_id: str) -> None:
        state[node_id] = 1
        stack.append(node_id)
        for dependency in graph.get(node_id, []):
            if dependency not in graph:
                continue
            if state.get(dependency, 0) == 0:
                visit(dependency)
            elif state.get(dependency) == 1:
                try:
                    found.update(stack[stack.index(dependency):])
                except ValueError:
                    found.add(dependency)
        stack.pop()
        state[node_id] = 2

    for node_id in graph:
        if state.get(node_id, 0) == 0:
            visit(node_id)
    return sorted(found)


def audit_dag(plan: Plan) -> DAGAuditResult:
    """Audit executable readiness; only hard dependencies block startup."""
    nodes = list(getattr(plan, "nodes", []) or [])
    if not nodes:
        return DAGAuditResult(
            "blocked_dag", [], list(plan.node_ids),
            {"__dag__": ["plan has no executable DAG nodes"]}, {}
        )

    reasons: Dict[str, List[str]] = {node.id: [] for node in nodes}
    if plan.complexity_band == "complex":
        global_validation = validate_integration_review_node(plan)
        if not global_validation.valid:
            reasons["__dag__"] = list(global_validation.errors)
    by_id: Dict[str, DAGNode] = {}
    duplicate_ids = set()
    for node in nodes:
        if node.id in by_id:
            duplicate_ids.add(node.id)
        by_id[node.id] = node
    for node in nodes:
        if node.id in duplicate_ids:
            reasons[node.id].append("DAG node IDs must be unique")

    plan_ids = set(plan.node_ids)
    actual_ids = set(by_id)
    for missing in sorted(plan_ids - actual_ids):
        reasons.setdefault(missing, []).append("plan references missing node")
    for extra in sorted(actual_ids - plan_ids):
        reasons[extra].append("node is outside the plan node_ids")

    structural = _structural_errors(nodes)
    cycles = _cycle_nodes(nodes)
    for error in structural:
        if error.startswith("node "):
            node_id = error.split(" ", 2)[1]
            if node_id in reasons:
                reasons[node_id].append(error)
        else:
            for node_id in cycles or by_id:
                reasons[node_id].append(error)
    for node_id in cycles:
        reasons[node_id].append("hard dependencies contain a cycle")

    writer_bindings: Dict[Tuple[str, str], List[str]] = {}
    for node in nodes:
        if is_integration_review_node(node):
            # Integration reviewers are read-only and intentionally have no
            # writer/worktree/write allowlist; validate their dedicated
            # contract instead of applying the business-writer audit.
            reasons[node.id].extend(validate_integration_review_node(plan).errors)
        else:
            reasons[node.id].extend(_audit_contract_errors(node))
        writer = _node_metadata(node, "writer")
        worktree = _node_metadata(node, "worktree")
        if isinstance(writer, str) and writer.strip() and isinstance(worktree, str) and worktree.strip():
            writer_bindings.setdefault((writer, worktree), []).append(node.id)
    for (writer, _worktree), node_ids in writer_bindings.items():
        if len(node_ids) > 1:
            allowlists = [tuple(_node_metadata(by_id[node_id], "allowlist") or ()) for node_id in node_ids]
            reason = (
                "writer {} allowlist mismatch".format(writer)
                if len(set(allowlists)) > 1 else "duplicate writer {}".format(writer)
            )
            for node_id in node_ids:
                reasons[node_id].append(reason)

    ready: List[str] = []
    for node in nodes:
        if node.status not in ("planned", "ready") or reasons[node.id]:
            continue
        unmet = [dependency for dependency in node.depends_on if dependency not in by_id]
        required_statuses = ("accepted",) if is_integration_review_node(node) else ("accepted", "delivered")
        unmet.extend(
            dependency for dependency in node.depends_on
            if dependency in by_id and by_id[dependency].status not in required_statuses
        )
        if unmet:
            reasons[node.id].append(
                "hard dependencies not complete (accepted or delivered): {}".format(
                    ", ".join(dict.fromkeys(unmet))
                )
            )
            continue
        # integration_after is intentionally non-blocking for startup.
        ready.append(node.id)

    for node_id in list(reasons):
        reasons[node_id] = list(dict.fromkeys(reasons[node_id]))

    blocked = [
        node.id for node in nodes
        if reasons[node.id] or node.status in ("blocked_design", "blocked_dag", "blocked_unknown")
    ]
    blocked.extend(missing for missing in sorted(plan_ids - actual_ids) if missing not in blocked)
    has_design = any("design change" in reason for values in reasons.values() for reason in values)
    fatal = [
        reason for values in reasons.values() for reason in values
        if not reason.startswith("hard dependencies not complete")
    ]
    if has_design or any(node.status == "blocked_design" for node in nodes):
        status = "blocked_design"
    elif fatal or structural or any(node.status == "blocked_dag" for node in nodes):
        status = "blocked_dag"
    elif any(node.status == "blocked_unknown" for node in nodes):
        status = "blocked_unknown"
    else:
        status = "ready"

    groups: Dict[str, List[str]] = {}
    for node in nodes:
        group = _node_metadata(node, "parallel_group")
        if node.id in ready and group:
            groups.setdefault(str(group), []).append(node.id)
    return DAGAuditResult(status, ready, blocked, reasons, groups)


def render_plan_artifacts(plan: Plan, output_dir: Path) -> PlanArtifacts:
    """Publish a new machine-readable DAG and plain-language plan without overwriting."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dag_path = output_dir / "dag.yaml"
    plan_path = output_dir / "plan.md"

    collisions = [path for path in (dag_path, plan_path) if os.path.lexists(str(path))]
    if collisions:
        raise FileExistsError("plan artifact already exists: {}".format(", ".join(str(path) for path in collisions)))

    audit = audit_dag(plan)
    node_data = []
    for node in getattr(plan, "nodes", []) or []:
        node_data.append({
            "id": node.id,
            "title": node.title,
            "depends_on": list(node.depends_on),
            "integration_after": list(node.integration_after),
            "parallel_group": node.parallel_group,
            "contract": dict(node.contract),
            "risk_tags": _node_metadata(node, "risk_tags"),
            "writer": _node_metadata(node, "writer"),
            "worktree": _node_metadata(node, "worktree"),
            "allowlist": _node_metadata(node, "allowlist"),
            "status": node.status,
            "audit_reasons": audit.reasons.get(node.id, []),
        })
    dag_data = {
        "plan_id": plan.plan_id,
        "version": plan.version,
        "status": plan.status,
        "prd": plan.prd_path,
        "nodes": node_data or [{"id": node_id} for node_id in plan.node_ids],
        "audit": audit.to_dict(),
    }
    artifact_basis = json.dumps(dag_data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    dag_data["artifact_hashes"] = {
        "content_basis_sha256": hashlib.sha256(artifact_basis.encode("utf-8")).hexdigest()
    }
    dag_content = json.dumps(dag_data, ensure_ascii=False, indent=2) + "\n"
    node_lines = "\n".join(
        "- {} [{}]".format(node_id, "ready" if node_id in audit.ready_nodes else "blocked")
        for node_id in plan.node_ids
    ) or "- 暂无节点"
    reason_lines = []
    for node_id, node_reasons in audit.reasons.items():
        if node_reasons and node_id != "__dag__":
            reason_lines.append("- {}：{}".format(node_id, "；".join(node_reasons)))
    if audit.reasons.get("__dag__"):
        reason_lines.extend("- DAG：{}".format(reason) for reason in audit.reasons["__dag__"])
    rationale = "\n".join(reason_lines) or "- 无阻塞理由"
    plan_content = "# 开发计划：{}\n\n版本：{}\n\n状态：{}\n\nPRD：{}\n\nDAG 审计：{}\n\n可启动节点：{}\n\n## 节点\n\n{}\n\n## 审计理由\n\n{}\n".format(
        plan.plan_id,
        plan.version,
        plan.status,
        plan.prd_path,
        audit.status,
        ", ".join(audit.ready_nodes) or "暂无",
        node_lines,
        rationale,
    )

    published = []
    with tempfile.TemporaryDirectory(prefix=".plan-artifacts-", dir=str(output_dir)) as staging:
        staging_dir = Path(staging)
        staged_dag = staging_dir / dag_path.name
        staged_plan = staging_dir / plan_path.name
        staged_dag.write_text(dag_content, encoding="utf-8")
        staged_plan.write_text(plan_content, encoding="utf-8")
        try:
            for staged, destination in ((staged_dag, dag_path), (staged_plan, plan_path)):
                os.link(str(staged), str(destination))
                published.append(destination)
        except BaseException:
            for path in reversed(published):
                path.unlink()
            raise
    return PlanArtifacts(dag_path, plan_path)
