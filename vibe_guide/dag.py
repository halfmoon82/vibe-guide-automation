"""DAG validation, ready-node scheduling, and plan artifact rendering."""

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .models import DAGNode, Plan


# V4's public lifecycle uses ``developing`` while V3 snapshots used
# ``running``.  Both spellings are accepted at this boundary so old snapshots
# remain replayable without creating a second task or writer.
_LIFECYCLE_TRANSITIONS = {
    "planned": {"ready"},
    "ready": {"running"},
    "running": {"delivered", "review"},
    "delivered": {"review"},
    "review": {"rework", "accepted"},
    "rework": {"review"},
    "accepted": {"archived"},
    "archived": set(),
}


def transition_node_status(node: Any, target: str) -> Any:
    """Apply one legal SDD lifecycle transition in place.

    ``node`` may be a snapshot mapping or a :class:`DAGNode`.  The function
    deliberately performs no provider calls and never allocates task IDs;
    rework therefore remains on the same developer/reviewer pair.
    """
    # ``developing`` was used in an early V4 draft; persist the existing
    # model's ``running`` status while accepting that spelling at the API
    # boundary for replay compatibility.
    target = "running" if target == "developing" else target
    if target not in _LIFECYCLE_TRANSITIONS:
        raise ValueError("unsupported lifecycle status: %s" % target)
    current = node.get("status") if isinstance(node, Mapping) else getattr(node, "status", None)
    current = "running" if current == "developing" else current
    if current not in _LIFECYCLE_TRANSITIONS or target not in _LIFECYCLE_TRANSITIONS[current]:
        raise ValueError("invalid lifecycle transition: %s -> %s" % (current, target))
    if isinstance(node, Mapping):
        node["status"] = target
    else:
        node.status = target
    return node


def _field(node: Any, name: str, default: Any = None) -> Any:
    return node.get(name, default) if isinstance(node, Mapping) else getattr(node, name, default)


def schedule_ready_nodes(nodes: Sequence[Any], active_pairs: Optional[int] = None,
                         capacity: int = 5) -> List[str]:
    """Start the next independent ready nodes, up to five active pairs.

    Hard dependencies must be accepted.  ``integration_after`` is never
    consulted for admission.  Isolated nodes are skipped without consuming a
    global slot, allowing unrelated nodes to refill the queue.
    """
    if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity < 1:
        raise ValueError("capacity must be a positive integer")
    capacity = min(capacity, 5)
    by_id = {_field(node, "id"): node for node in nodes if _field(node, "id") is not None}
    if active_pairs is None:
        active_pairs = sum(
            _field(node, "status") in {"developing", "running", "delivered", "review", "rework"}
            for node in nodes
            if not _field(node, "isolated", False)
        )
    if isinstance(active_pairs, bool) or not isinstance(active_pairs, int) or active_pairs < 0:
        raise ValueError("active_pairs must be a non-negative integer")
    slots = max(0, capacity - active_pairs)
    # Build the whole admission batch first.  Validation must be atomic: a
    # later bad identity/writer may not leave an earlier node already started.
    candidates: List[Tuple[Any, str]] = []
    for node in nodes:
        if _field(node, "status") != "ready" or _field(node, "isolated", False):
            continue
        dependencies = list(_field(node, "depends_on", []) or [])
        if any(dep not in by_id or _field(by_id[dep], "status") != "accepted" for dep in dependencies):
            continue
        candidates.append((node, _field(node, "id")))

    from .evidence import validate_task_pair
    active_writers = {
        _field(item, "writer", _field(item, "developer_task_id"))
        for item in nodes
        if _field(item, "status") in {"running", "delivered", "review", "rework"}
        and not _field(item, "isolated", False)
    }
    batch_writers = set(active_writers)
    # Only nodes admitted in this batch are validated here.  Nodes beyond the
    # available capacity remain queued and must not block unrelated work; they
    # will be validated when a later refill actually attempts to start them.
    for node, _node_id in candidates[:slots]:
        developer = _field(node, "developer_task_id", _field(node, "developer_task"))
        reviewer = _field(node, "reviewer_task_id", _field(node, "reviewer_task"))
        # New V4 scheduling requires both visible task identities.  This is
        # intentionally stricter than replaying legacy ready_nodes snapshots.
        validate_task_pair(developer, reviewer)
        writer = _field(node, "writer", developer)
        if writer and writer in batch_writers:
            raise ValueError("duplicate active writer: %s" % writer)
        if writer:
            batch_writers.add(writer)

    started: List[str] = []
    for node, node_id in candidates[:slots]:
        transition_node_status(node, "running")
        started.append(node_id)
    return started


# Short names used by integrations that treat the scheduler as an orchestration
# primitive.  Keeping aliases here avoids a second implementation (and hence a
# second place where capacity or isolation rules could drift).
advance_node = transition_node_status
schedule_nodes = schedule_ready_nodes


def active_pair_count(nodes: Sequence[Any]) -> int:
    """Count non-isolated pairs occupying a scheduler slot."""
    return sum(
        _field(node, "status") in {"developing", "running", "delivered", "review", "rework"}
        for node in nodes
        if not _field(node, "isolated", False)
    )


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
        unmet.extend(
            dependency for dependency in node.depends_on
            if dependency in by_id and by_id[dependency].status not in ("accepted", "delivered")
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
