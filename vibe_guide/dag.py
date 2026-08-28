"""DAG validation, ready-node scheduling, and plan artifact rendering."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
from typing import Dict, List, Mapping, Sequence, Tuple

from .models import DAGNode, Plan


@dataclass(frozen=True)
class DAGValidation:
    valid: bool
    errors: Tuple[str, ...]


@dataclass(frozen=True)
class PlanArtifacts:
    dag_path: Path
    plan_path: Path


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


def render_plan_artifacts(plan: Plan, output_dir: Path) -> PlanArtifacts:
    """Publish a new machine-readable DAG and plain-language plan without overwriting."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dag_path = output_dir / "dag.yaml"
    plan_path = output_dir / "plan.md"

    collisions = [path for path in (dag_path, plan_path) if os.path.lexists(str(path))]
    if collisions:
        raise FileExistsError("plan artifact already exists: {}".format(", ".join(str(path) for path in collisions)))

    dag_data = {
        "plan_id": plan.plan_id,
        "version": plan.version,
        "status": plan.status,
        "prd": plan.prd_path,
        "nodes": [{"id": node_id} for node_id in plan.node_ids],
    }
    dag_content = json.dumps(dag_data, ensure_ascii=False, indent=2) + "\n"
    node_lines = "\n".join("- {}".format(node_id) for node_id in plan.node_ids) or "- 暂无节点"
    plan_content = "# 开发计划：{}\n\n版本：{}\n\n状态：{}\n\nPRD：{}\n\n## 节点\n\n{}\n".format(
        plan.plan_id,
        plan.version,
        plan.status,
        plan.prd_path,
        node_lines,
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
