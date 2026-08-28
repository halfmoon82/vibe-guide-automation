"""DAG validation, ready-node scheduling, and plan artifact rendering."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from .models import DAGNode, Plan


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
    parallel_groups: Dict[str, List[str]] = None

    def __post_init__(self):
        if self.status not in {"ready", "blocked_design", "blocked_dag", "blocked_deploy", "blocked_unknown"}:
            raise ValueError("unsupported DAG audit status")
        object.__setattr__(self, "ready_nodes", list(self.ready_nodes))
        object.__setattr__(self, "blocked_nodes", list(self.blocked_nodes))
        object.__setattr__(self, "reasons", {key: list(value) for key, value in self.reasons.items()})
        object.__setattr__(self, "parallel_groups", {
            key: list(value) for key, value in (self.parallel_groups or {}).items()
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


def _node_metadata(node: DAGNode, name: str) -> Any:
    """Read identity metadata from explicit, legacy, or authoritative fields."""
    sources = _metadata_sources(node, name)
    for _source, value in sources:
        if value not in (None, "", []):
            return value
    return None


def _metadata_sources(node: DAGNode, name: str) -> List[Tuple[str, Any]]:
    """Return all supported metadata locations in precedence order.

    Revision-5 ``nodes.json`` puts writer/allowlist in
    ``contract.worker_profile`` while older V1/V2 fixtures may keep them as
    explicit DAGNode fields or direct contract keys.  Keeping every source
    lets the audit reject conflicting identity claims instead of silently
    choosing one.
    """
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    profile = contract.get("worker_profile")
    profile_value = profile.get(name) if isinstance(profile, Mapping) else None
    return [
        ("explicit", getattr(node, name, None)),
        ("contract", contract.get(name)),
        ("worker_profile", profile_value),
    ]


def _audit_contract_errors(node: DAGNode) -> List[str]:
    errors = []
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
    risk_tags = _node_metadata(node, "risk_tags")
    if not isinstance(risk_tags, list) or not risk_tags or not all(
        isinstance(item, str) and item.strip() for item in risk_tags
    ):
        errors.append("node {} contract is missing risk_tags".format(node.id))
    profile = contract.get("worker_profile")
    if profile is not None and not isinstance(profile, Mapping):
        errors.append("node {} worker_profile must be a mapping".format(node.id))
        profile = {}
    if isinstance(profile, Mapping):
        for field_name in ("writer", "allowlist"):
            if field_name not in profile or not _has_content(profile[field_name]):
                errors.append("node {} worker_profile is missing {}".format(node.id, field_name))

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

    # Explicit, legacy direct-contract, and authoritative worker_profile
    # claims must agree whenever more than one is present.
    for name in ("risk_tags", "writer", "worktree", "allowlist"):
        populated = [(source, value) for source, value in _metadata_sources(node, name) if value not in (None, "", [])]
        for index, (left_source, left_value) in enumerate(populated):
            for right_source, right_value in populated[index + 1:]:
                if left_value != right_value:
                    errors.append("node {} {} mismatch ({} vs {})".format(node.id, name, left_source, right_source))
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
    graph = {node.id: list(node.depends_on) for node in nodes}
    active = set()
    complete = set()
    found = set()

    def visit(node_id: str) -> None:
        if node_id in active:
            found.update(active)
            return
        if node_id in complete:
            return
        active.add(node_id)
        for dependency in graph.get(node_id, []):
            if dependency in graph:
                visit(dependency)
        active.discard(node_id)
        complete.add(node_id)

    for node_id in graph:
        visit(node_id)
    return sorted(found)


def audit_dag(plan: Plan) -> DAGAuditResult:
    """Audit executable readiness while keeping integration edges non-blocking."""
    nodes = list(getattr(plan, "nodes", []) or [])
    reasons: Dict[str, List[str]] = {node.id: [] for node in nodes}
    if not nodes:
        return DAGAuditResult(
            "blocked_dag", [], list(plan.node_ids),
            {"__dag__": ["plan has no executable DAG nodes"]}, {}
        )

    by_id: Dict[str, DAGNode] = {}
    duplicate_ids = set()
    for node in nodes:
        if node.id in by_id:
            duplicate_ids.add(node.id)
        by_id[node.id] = node
    if duplicate_ids:
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
    if cycles:
        for node_id in cycles:
            reasons[node_id].append("hard dependencies contain a cycle")

    writers: Dict[Tuple[str, str], List[str]] = {}
    for node in nodes:
        for error in _audit_contract_errors(node):
            reasons[node.id].append(error)
        writer = _node_metadata(node, "writer")
        worktree = _node_metadata(node, "worktree")
        if isinstance(writer, str) and writer.strip() and isinstance(worktree, str) and worktree.strip():
            writers.setdefault((writer, worktree), []).append(node.id)
    for (writer, worktree), node_ids in writers.items():
        if len(node_ids) > 1:
            allowlists = [tuple(_node_metadata(by_id[node_id], "allowlist") or ()) for node_id in node_ids]
            reason = (
                "writer {} allowlist mismatch".format(writer)
                if len(set(allowlists)) > 1
                else "duplicate writer {}".format(writer)
            )
            for node_id in node_ids:
                reasons[node_id].append(reason)

    ready = []
    for node in nodes:
        if node.status not in ("planned", "ready") or reasons[node.id]:
            continue
        unmet = [dependency for dependency in node.depends_on if dependency not in by_id]
        unmet.extend(
            dependency for dependency in node.depends_on
            if dependency in by_id and by_id[dependency].status not in ("accepted", "delivered")
        )
        if unmet:
            reasons[node.id].append(
                "hard dependencies not complete (accepted or delivered): {}".format(", ".join(dict.fromkeys(unmet)))
            )
            continue
        # integration_after is deliberately not consulted here.
        ready.append(node.id)

    for node in nodes:
        reasons[node.id] = list(dict.fromkeys(reasons[node.id]))

    blocked = [
        node.id for node in nodes
        if reasons[node.id] or (node.status in ("blocked_design", "blocked_dag", "blocked_deploy", "blocked_unknown"))
    ]
    blocked.extend(missing for missing in sorted(plan_ids - actual_ids) if missing not in blocked)
    has_design = any("design change" in reason for values in reasons.values() for reason in values)
    # A pending hard dependency is an ordinary not-yet-ready state, not a
    # malformed DAG.  Only structural, contract, identity, or design errors
    # make the audit itself blocked.
    fatal_reasons = [
        reason for values in reasons.values() for reason in values
        if not reason.startswith("hard dependencies not complete")
    ]
    has_dag_error = bool(fatal_reasons) or bool(structural)
    explicit_unknown = any(node.status == "blocked_unknown" for node in nodes)
    explicit_design = any(node.status == "blocked_design" for node in nodes)
    explicit_deploy = any(node.status == "blocked_deploy" for node in nodes)
    if explicit_deploy:
        for node in nodes:
            if node.status == "blocked_deploy":
                reasons[node.id].append("deploy authorization is required")
    if has_design or explicit_design:
        status = "blocked_design"
    elif has_dag_error:
        status = "blocked_dag"
    elif explicit_deploy:
        status = "blocked_deploy"
    elif explicit_unknown:
        status = "blocked_unknown"
    else:
        status = "ready"
    groups: Dict[str, List[str]] = {}
    for node in nodes:
        if node.id in ready and _node_metadata(node, "parallel_group"):
            groups.setdefault(str(_node_metadata(node, "parallel_group")), []).append(node.id)
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
