import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path

from vibe_guide.dag import append_integration_review_node
from vibe_guide.evidence import evaluate_v41_closeout, record_integration_review
from vibe_guide.models import DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding


def _business_node(node_id):
    return DAGNode(
        node_id,
        "Business " + node_id,
        [],
        [],
        "business",
        {
            "input": "request",
            "output": "delivery",
            "error_behavior": "return an error",
            "acceptance_example": "accepted",
            "risk_tags": ["business"],
            "writer": "worker-" + node_id,
            "reviewer": "reviewer-" + node_id,
        },
        "accepted",
        writer="worker-" + node_id,
        reviewer="reviewer-" + node_id,
    )


def _complex_plan():
    nodes = [_business_node(node_id) for node_id in ("api", "worker", "ui")]
    return Plan(
        "fixture-plan",
        1,
        "docs/prd.md",
        [node.id for node in nodes],
        "authorized",
        spec_path="docs/spec.md",
        complexity_band="complex",
        nodes=nodes,
        integration_contract={
            "iteration_context": {"kind": "iteration", "based_on": "V4"},
            "compatibility_scope": ["V4 API"],
            "agentsmd_acceptance_refs": ["AGENTS.md#8"],
            "integration_acceptance_contract": {"checks": ["contracts", "clearance"]},
            "unverified_or_excluded": ["real provider lifecycle"],
        },
    )


def _snapshot(plan):
    nodes = {
        node.id: {
            "status": node.status,
            "contract_digest": ("a" if node.id == "api" else "b" if node.id == "worker" else "c") * 64,
            "writer": node.writer,
            "reviewer": node.reviewer,
        }
        for node in plan.nodes
    }
    integration = next(node for node in plan.nodes if node.id == "integration-review")
    nodes[integration.id] = {
        "status": integration.status,
        "contract_digest": integration.contract.get("digest", "d" * 64),
        "reviewer": integration.reviewer,
        "review_clearance": {"p0": 0, "p1": 0, "p2": 0},
        "evidence": [],
    }
    return SimpleNamespace(
        run_id="fixture-run",
        plan_id=plan.plan_id,
        plan_version=plan.version,
        status="running",
        nodes=nodes,
        authorization_digest="e" * 64,
        node_contract_digest="f" * 64,
        prd_digest="1" * 64,
        spec_digest="2" * 64,
        integration_review_evidence={},
    )


def _evidence(snapshot, findings, clearance):
    return {
        "schema_version": 1,
        "run_id": snapshot.run_id,
        "plan_id": snapshot.plan_id,
        "plan_revision": snapshot.plan_version,
        "prd_digest": snapshot.prd_digest,
        "spec_digest": snapshot.spec_digest,
        "authorization_digest": snapshot.authorization_digest,
        "node_contract_digest": snapshot.node_contract_digest,
        "aggregated_scope": {"nodes": ["api", "worker", "ui"]},
        "iteration_compatibility": {"status": "verified", "evidence": ["fixture-v4"]},
        "agentsmd_acceptance_refs": [{"ref": "AGENTS.md#8", "evidence": "fixture"}],
        "test_runtime_delivery": {"status": "verified", "evidence": ["fixture-tests"]},
        "unverified_or_excluded": ["real provider lifecycle"],
        "findings": findings,
        "clearance": clearance,
    }


def run_v41_integration_fixture():
    """Run a provider-neutral V4.1 integration lifecycle and return evidence."""
    plan = append_integration_review_node(_complex_plan())
    snapshot = _snapshot(plan)
    # Persist the original developer/reviewer pair in the local registry so
    # rework lineage is read back from the same durable identity, rather than
    # inferred from display names.
    with tempfile.TemporaryDirectory() as registry_root:
        registry_paths = ProjectPaths(Path(registry_root))
        original_bindings = {}
        for node_id in ("api", "worker", "ui"):
            for role in ("developer", "reviewer"):
                task_id = "task-{}-{}".format(node_id, role)
                binding = TaskBinding(
                    provider="codex",
                    mode="visible",
                    issue_id=node_id,
                    role=role,
                    task_id=task_id,
                    host="local",
                    worktree=".worktrees/{}".format(node_id),
                    branch="codex/{}".format(node_id),
                    cursor="cursor-{}-{}".format(node_id, role),
                    run_id=snapshot.run_id,
                    generation=2,
                )
                save_task_binding(registry_paths, binding)
            original_bindings[node_id] = {
                "developer": load_task_binding(registry_paths, node_id, "developer", snapshot.run_id).to_dict(),
                "reviewer": load_task_binding(registry_paths, node_id, "reviewer", snapshot.run_id).to_dict(),
            }
        rework_bindings = {
            issue_id: {
                "developer": load_task_binding(registry_paths, issue_id, "developer", snapshot.run_id).to_dict(),
                "reviewer": load_task_binding(registry_paths, issue_id, "reviewer", snapshot.run_id).to_dict(),
            }
            for issue_id in ("worker", "ui")
        }
    first_findings = [
        {"severity": "P1", "status": "open", "issue_id": "worker", "rework_target": "worker"},
        {"severity": "P2", "status": "open", "issue_id": "ui", "rework_target": "ui"},
    ]
    first = _evidence(snapshot, first_findings, {"p0": 0, "p1": 1, "p2": 1})
    record_integration_review(snapshot, first)
    first_decision = evaluate_v41_closeout(snapshot).to_dict()
    first_integration_status = snapshot.nodes["integration-review"]["status"]

    second = _evidence(snapshot, [], {"p0": 0, "p1": 0, "p2": 0})
    record_integration_review(snapshot, second)
    second_decision = evaluate_v41_closeout(snapshot).to_dict()
    second_integration_status = snapshot.nodes["integration-review"]["status"]

    non_complex = {}
    for route in ("simple", "light_plan"):
        node = _business_node("only")
        simple_plan = Plan(route + "-fixture", 1, "docs/prd.md", [node.id], "authorized", complexity_band=route, nodes=[node])
        simple_snapshot = SimpleNamespace(
            run_id=route + "-run", plan_id=simple_plan.plan_id, plan_version=1,
            status="running", nodes={node.id: {"status": "accepted"}},
            integration_review_evidence={},
        )
        decision = evaluate_v41_closeout(simple_snapshot).to_dict()
        non_complex[route] = {
            "node_statuses": {node.id: node.status},
            "integration_evidence": None,
            "closeout_decision": decision,
        }

    current_evidence = {key: value for key, value in snapshot.integration_review_evidence.items() if key != "history"}
    lineage = {
        "run_id": snapshot.run_id,
        "plan_id": snapshot.plan_id,
        "plan_revision": snapshot.plan_version,
        "reviewer": "integration-reviewer",
        "rework_targets": [finding["rework_target"] for finding in first_findings],
        "rework": [],
        "original_bindings": original_bindings,
        "original_workers": {node_id: "worker-" + node_id for node_id in ("api", "worker", "ui")},
        "original_reviewers": {node_id: "reviewer-" + node_id for node_id in ("api", "worker", "ui")},
        "history_length": len(snapshot.integration_review_evidence.get("history", [])),
    }
    for finding in first_findings:
        issue_id = finding["issue_id"]
        original = original_bindings[issue_id]
        continuation = rework_bindings[issue_id]
        lineage["rework"].append({
            "issue_id": issue_id,
            "developer": continuation["developer"],
            "reviewer_binding": continuation["reviewer"],
            "same_task": continuation["developer"]["task_id"] == original["developer"]["task_id"],
        })
    node_statuses = {node_id: node["status"] for node_id, node in snapshot.nodes.items()}
    return {
        "route": "complex",
        "node_statuses": node_statuses,
        "business_node_statuses": {node_id: snapshot.nodes[node_id]["status"] for node_id in ("api", "worker", "ui")},
        "integration_node_id": "integration-review",
        "integration_node_status": snapshot.nodes["integration-review"]["status"],
        "first_review": {
            "evidence": first,
            "clearance": first["clearance"],
            "lineage": lineage,
            "integration_node_status": first_integration_status,
            "closeout_decision": first_decision,
        },
        "second_review": {
            "evidence": current_evidence,
            "clearance": second["clearance"],
            "lineage": lineage,
            "integration_node_status": second_integration_status,
            "closeout_decision": second_decision,
        },
        "non_complex": non_complex,
        "integration_evidence": current_evidence,
        "integration_evidence_history": snapshot.integration_review_evidence.get("history", []),
        "clearance": second["clearance"],
        "lineage": lineage,
        "closeout_decision": second_decision,
    }


class V41IntegrationEndToEndTests(unittest.TestCase):
    def test_complex_fixture_requires_integration_clearance_before_closeout(self):
        result = run_v41_integration_fixture()

        self.assertEqual(result["route"], "complex")
        self.assertEqual(
            result["business_node_statuses"],
            {"api": "accepted", "worker": "accepted", "ui": "accepted"},
        )
        self.assertEqual(result["integration_node_id"], "integration-review")
        self.assertEqual(result["first_review"]["closeout_decision"]["status"], "running")
        self.assertFalse(result["first_review"]["closeout_decision"]["allowed"])
        self.assertEqual(result["first_review"]["integration_node_status"], "rework")
        self.assertEqual(result["first_review"]["clearance"], {"p0": 0, "p1": 1, "p2": 1})
        self.assertEqual(len(result["first_review"]["evidence"]["findings"]), 2)
        self.assertEqual(result["first_review"]["lineage"]["rework_targets"], ["worker", "ui"])
        self.assertEqual(result["first_review"]["lineage"]["reviewer"], "integration-reviewer")
        self.assertEqual(result["second_review"]["clearance"], {"p0": 0, "p1": 0, "p2": 0})
        self.assertTrue(result["second_review"]["closeout_decision"]["allowed"])
        self.assertEqual(result["second_review"]["closeout_decision"]["status"], "complete")
        self.assertEqual(result["second_review"]["integration_node_status"], "accepted")
        self.assertTrue(all(item["same_task"] for item in result["lineage"]["rework"]))
        for item in result["lineage"]["rework"]:
            original = result["lineage"]["original_bindings"][item["issue_id"]]
            self.assertEqual(item["developer"], original["developer"])
            self.assertEqual(item["reviewer_binding"], original["reviewer"])
            for field in ("task_id", "generation", "worktree", "branch", "cursor"):
                self.assertEqual(item["developer"][field], original["developer"][field])
        self.assertEqual(
            result["integration_evidence_history"][0]["findings"],
            result["first_review"]["evidence"]["findings"],
        )
        self.assertEqual(
            result["integration_evidence_history"][0]["clearance"],
            {"p0": 0, "p1": 1, "p2": 1},
        )
        self.assertEqual(result["clearance"], {"p0": 0, "p1": 0, "p2": 0})
        self.assertEqual(result["closeout_decision"]["status"], "complete")
        self.assertEqual(result["lineage"]["history_length"], 2)

    def test_simple_and_light_plan_have_no_integration_node(self):
        result = run_v41_integration_fixture()
        for route in ("simple", "light_plan"):
            self.assertNotIn("integration-review", result["non_complex"][route]["node_statuses"])
            self.assertIsNone(result["non_complex"][route]["integration_evidence"])
            self.assertTrue(result["non_complex"][route]["closeout_decision"]["allowed"])


if __name__ == "__main__":
    unittest.main()
