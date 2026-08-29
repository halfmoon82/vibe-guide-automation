"""Fail-closed DAG validation and revision-bound audit artifacts."""

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
    status: str
    ready_nodes: List[str]
    blocked_nodes: List[str]
    reasons: Dict[str, List[str]]
    parallel_groups: Dict[str, List[str]]
    plan_revision: int = 1
    node_ids: Tuple[str, ...] = ()
    digest: str = ""
    plan_id: str = ""

    def __post_init__(self):
        if self.status not in {"ready", "blocked_dag", "blocked_design", "blocked_unknown"}:
            raise ValueError("unsupported DAG audit status")
        object.__setattr__(self, "ready_nodes", list(self.ready_nodes))
        object.__setattr__(self, "blocked_nodes", list(self.blocked_nodes))
        object.__setattr__(self, "reasons", {str(k): list(v) for k, v in self.reasons.items()})
        object.__setattr__(self, "parallel_groups", {str(k): list(v) for k, v in (self.parallel_groups or {}).items()})
        object.__setattr__(self, "node_ids", tuple(self.node_ids))

    @property
    def audit_digest(self):
        return self.digest

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "ready_nodes": list(self.ready_nodes),
                "blocked_nodes": list(self.blocked_nodes), "reasons": self.reasons,
                "parallel_groups": self.parallel_groups, "plan_revision": self.plan_revision,
                "node_ids": list(self.node_ids), "digest": self.digest, "plan_id": self.plan_id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DAGAuditResult":
        if not isinstance(data, Mapping):
            raise TypeError("DAG audit must be an object")
        status = data.get("audit_status", data.get("status", "blocked_dag"))
        if status == "reviewed":
            status = "ready" if not data.get("blocked_nodes") else "blocked_dag"
        return cls(status, data.get("ready_nodes", []), data.get("blocked_nodes", []),
                   data.get("reasons", data.get("blocked_reasons", {})), data.get("parallel_groups", {}),
                   int(data.get("plan_revision", 1)), tuple(data.get("node_ids", ())),
                   str(data.get("digest", "")), str(data.get("plan_id", "")))


_REQUIRED = (("input", ("input", "inputs")), ("output", ("output", "outputs")),
             ("error_behavior", ("error_behavior", "errors")),
             ("acceptance_example", ("acceptance_example", "acceptance_examples")))


def _content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, Mapping):
        return bool(value) and all(_content(k) and _content(v) for k, v in value.items())
    if isinstance(value, (list, tuple)):
        return bool(value) and all(_content(item) for item in value)
    return bool(value)


def _value(node: DAGNode, name: str) -> Any:
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    if contract.get(name) not in (None, "", []):
        return contract[name]
    profile = contract.get("worker_profile")
    if isinstance(profile, Mapping) and profile.get(name) not in (None, "", []):
        return profile[name]
    return getattr(node, name, None)


def _contract_errors(node: DAGNode, expected_adapter: str) -> List[str]:
    contract = node.contract if isinstance(node.contract, Mapping) else {}
    errors: List[str] = []
    for canonical, aliases in _REQUIRED:
        if not any(_content(contract.get(key)) for key in aliases):
            errors.append("node %s contract is missing %s" % (node.id, canonical))
    if contract.get("design_change") or contract.get("status") == "blocked_design":
        errors.append("node %s is blocked by a design change" % node.id)
    if contract.get("adapter_id") != expected_adapter:
        errors.append("node %s adapter_id must equal %s" % (node.id, expected_adapter))
    if contract.get("provider") != "codex-app-visible":
        errors.append("node %s provider must equal codex-app-visible" % node.id)
    for name in ("risk_tags", "writer", "reviewer", "worktree", "branch", "allowlist"):
        value = _value(node, name)
        if name in ("risk_tags", "allowlist"):
            if not isinstance(value, list) or not value or not all(isinstance(item, str) and item.strip() for item in value):
                errors.append("node %s contract is missing %s" % (node.id, name))
        elif not isinstance(value, str) or not value.strip():
            errors.append("node %s contract is missing %s" % (node.id, name))
    allowlist = _value(node, "allowlist")
    if isinstance(allowlist, list):
        for item in allowlist:
            path = Path(item)
            if path.is_absolute() or ".." in path.parts:
                errors.append("node %s allowlist escapes the project" % node.id)
    for name in ("writer", "reviewer", "worktree", "branch", "allowlist", "risk_tags"):
        explicit = getattr(node, name, None)
        nested = contract.get(name)
        profile = contract.get("worker_profile")
        profile_value = profile.get(name) if isinstance(profile, Mapping) else None
        populated = [value for value in (explicit, nested, profile_value) if value not in (None, "", [])]
        if len({json.dumps(value, sort_keys=True, default=str) for value in populated}) > 1:
            errors.append("node %s %s mismatch" % (node.id, name))
    return list(dict.fromkeys(errors))


def _structural_errors(nodes: Sequence[DAGNode]) -> List[str]:
    errors: List[str] = []
    ids = [node.id for node in nodes]
    if len(ids) != len(set(ids)):
        errors.append("DAG node IDs must be unique")
    known = set(ids)
    graph = {node.id: list(node.depends_on) for node in nodes}
    for node in nodes:
        missing = [dep for dep in node.depends_on if dep not in known]
        if missing:
            errors.append("node %s has unknown hard dependencies: %s" % (node.id, ", ".join(missing)))
    visiting, visited = set(), set()

    def visit(current):
        if current in visiting:
            return True
        if current in visited:
            return False
        visiting.add(current)
        found = any(dep in graph and visit(dep) for dep in graph.get(current, []))
        visiting.remove(current)
        visited.add(current)
        return found

    if any(visit(node_id) for node_id in graph if node_id not in visited):
        errors.append("DAG hard dependencies contain a cycle")
    return errors


def validate_dag(nodes: List[DAGNode], expected_adapter: str = "codex") -> DAGValidation:
    errors = _structural_errors(nodes)
    errors.extend(error for node in nodes for error in _contract_errors(node, expected_adapter))
    writers: Dict[str, List[DAGNode]] = {}
    for node in nodes:
        writer = _value(node, "writer")
        if isinstance(writer, str) and writer.strip():
            writers.setdefault(writer, []).append(node)
    for writer, grouped in writers.items():
        if len(grouped) > 1:
            errors.append("duplicate writer %s" % writer)
    allowlist_owners: Dict[str, List[str]] = {}
    for node in nodes:
        for path in (_value(node, "allowlist") or []):
            allowlist_owners.setdefault(path, []).append(node.id)
    for path, owners in allowlist_owners.items():
        if len(owners) > 1:
            errors.append("allowlist conflict on %s: %s" % (path, ", ".join(owners)))
    return DAGValidation(not errors, tuple(dict.fromkeys(errors)))


def ready_nodes(nodes: List[DAGNode], expected_adapter: str = "codex") -> List[DAGNode]:
    if _structural_errors(nodes) or not validate_dag(nodes, expected_adapter).valid:
        return []
    by_id = {node.id: node for node in nodes}
    return [node for node in nodes if node.status in {"planned", "ready"}
            and all(by_id[dep].status in {"accepted", "delivered"} for dep in node.depends_on)]


def _cycle_nodes(nodes: Sequence[DAGNode]) -> set:
    graph = {node.id: list(node.depends_on) for node in nodes}
    visiting, visited, result = [], set(), set()

    def visit(node_id):
        if node_id in visiting:
            result.update(visiting[visiting.index(node_id):])
            return
        if node_id in visited:
            return
        visiting.append(node_id)
        for dep in graph.get(node_id, []):
            if dep in graph:
                visit(dep)
        visiting.pop()
        visited.add(node_id)

    for node_id in graph:
        visit(node_id)
    return result


def audit_dag(plan: Plan, expected_adapter: str = "codex") -> DAGAuditResult:
    nodes = list(plan.nodes or [])
    node_ids = tuple(plan.node_ids)
    reasons: Dict[str, List[str]] = {node.id: [] for node in nodes}
    if not nodes:
        reasons["__dag__"] = ["plan has no executable DAG nodes"]
    by_id: Dict[str, DAGNode] = {}
    for node in nodes:
        if node.id in by_id:
            reasons[node.id].append("DAG node IDs must be unique")
        by_id[node.id] = node
    for missing in sorted(set(node_ids) - set(by_id)):
        reasons.setdefault(missing, []).append("plan references missing node")
    for extra in sorted(set(by_id) - set(node_ids)):
        reasons[extra].append("node is outside the plan node_ids")
    structural = _structural_errors(nodes)
    cycles = _cycle_nodes(nodes)
    for error in structural:
        if error.startswith("node "):
            reasons.setdefault(error.split(" ", 2)[1], []).append(error)
        else:
            for target in cycles or by_id:
                reasons[target].append(error)
    for node in nodes:
        reasons[node.id].extend(_contract_errors(node, expected_adapter))
    writer_nodes: Dict[str, List[str]] = {}
    for node in nodes:
        writer = _value(node, "writer")
        if isinstance(writer, str) and writer.strip():
            writer_nodes.setdefault(writer, []).append(node.id)
    for writer, ids in writer_nodes.items():
        if len(ids) > 1:
            for item in ids:
                reasons[item].append("duplicate writer %s" % writer)
    allowlist_owners: Dict[str, List[str]] = {}
    for node in nodes:
        for path in (_value(node, "allowlist") or []):
            allowlist_owners.setdefault(path, []).append(node.id)
    for path, ids in allowlist_owners.items():
        if len(ids) > 1:
            for item in ids:
                reasons[item].append("allowlist conflict on %s" % path)
    ready: List[str] = []
    for node in nodes:
        if node.status not in {"planned", "ready"} or reasons[node.id]:
            continue
        unmet = [dep for dep in node.depends_on if dep not in by_id or by_id[dep].status not in {"accepted", "delivered"}]
        if unmet:
            reasons[node.id].append("hard dependencies not complete: " + ", ".join(unmet))
        else:
            ready.append(node.id)
    reasons = {key: list(dict.fromkeys(value)) for key, value in reasons.items()}
    blocked = [node.id for node in nodes if reasons[node.id] or node.status.startswith("blocked")]
    blocked.extend(item for item in sorted(set(node_ids) - set(by_id)) if item not in blocked)
    fatal = [reason for values in reasons.values() for reason in values if not reason.startswith("hard dependencies not complete")]
    if any("design change" in reason for values in reasons.values() for reason in values):
        status = "blocked_design"
    elif fatal or structural or not nodes:
        status = "blocked_dag"
    elif any(node.status == "blocked_unknown" for node in nodes):
        status = "blocked_unknown"
    else:
        status = "ready"
    groups: Dict[str, List[str]] = {}
    for node in nodes:
        if node.id in ready and node.parallel_group:
            groups.setdefault(node.parallel_group, []).append(node.id)
    basis = {"plan_id": plan.plan_id, "plan_revision": plan.version, "node_ids": list(node_ids),
             "nodes": [{"id": n.id, "depends_on": sorted(n.depends_on), "integration_after": sorted(n.integration_after),
                        "parallel_group": n.parallel_group, "status": n.status, "contract": n.contract,
                        "risk_tags": n.risk_tags, "writer": n.writer, "worktree": n.worktree, "allowlist": n.allowlist}
                       for n in sorted(nodes, key=lambda x: x.id)], "ready_nodes": ready,
             "blocked_nodes": blocked, "reasons": reasons, "parallel_groups": groups}
    digest = hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return DAGAuditResult(status, ready, blocked, reasons, groups, plan.version, node_ids, digest, plan.plan_id)


def _write_atomic(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, str(path))
    finally:
        if os.path.exists(name):
            os.unlink(name)


def render_plan_artifacts(plan: Plan, output_dir: Path) -> PlanArtifacts:
    output_dir = Path(output_dir)
    if output_dir.exists() and output_dir.is_symlink():
        raise ValueError("plan artifact directory may not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    dag_path, plan_path = output_dir / "dag-audit.json", output_dir / "plan.md"
    if dag_path.exists() or plan_path.exists() or dag_path.is_symlink() or plan_path.is_symlink():
        raise FileExistsError("plan artifact already exists")
    audit = audit_dag(plan)
    payload = audit.to_dict()
    payload.update({"schema_version": 1, "status": "reviewed" if audit.status == "ready" else audit.status,
                    "audit_status": audit.status, "plan_id": plan.plan_id, "node_count": len(plan.nodes),
                    "node_contract_digest": hashlib.sha256(json.dumps([n.to_dict() for n in plan.nodes], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                    "blocked_reasons": payload["reasons"]})
    _write_atomic(dag_path, json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    _write_atomic(plan_path, "# 开发计划：%s\n\n版本：%s\n\nDAG 审计：%s\n\n可启动节点：%s\n" %
                  (plan.plan_id, plan.version, audit.status, ", ".join(audit.ready_nodes) or "暂无"))
    return PlanArtifacts(dag_path, plan_path)


__all__ = ["DAGValidation", "DAGAuditResult", "PlanArtifacts", "validate_dag", "ready_nodes", "audit_dag", "render_plan_artifacts"]
