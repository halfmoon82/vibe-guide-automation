import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.dag import DAGAuditResult, audit_dag, ready_nodes, render_plan_artifacts, validate_dag
from vibe_guide.models import DAGNode, Plan


def node(node_id, depends=None, integration=None, group=None, status="planned", contract=None):
    default_contract = {
        "input": "request",
        "output": "result",
        "error_behavior": "return an error",
        "acceptance_example": "example passes",
    }
    return DAGNode(node_id, node_id, depends or [], integration or [], group, default_contract if contract is None else contract, status)


def audited_node(node_id, depends=None, integration=None, group=None, status="planned", contract=None, writer=None, allowlist=None):
    base = {
        "input": "request",
        "output": "result",
        "error_behavior": "return an error",
        "acceptance_examples": ["example passes"],
        "risk_tags": ["scheduling"],
        "writer": writer or "writer-" + node_id,
        "worktree": ".vibe/worktrees/" + node_id,
        "allowlist": allowlist or ["vibe_guide/" + node_id + ".py"],
    }
    if contract is not None:
        base = dict(contract)
    return DAGNode(
        node_id, node_id, depends or [], integration or [], group, base, status,
        writer=writer or "", allowlist=allowlist or [],
    )


class DAGTests(unittest.TestCase):
    def test_authoritative_worker_profile_identity_unlocks_nodes_after_v2_0(self):
        def authoritative(node_id, depends=None, status="planned"):
            contract = {
                "input": "request",
                "output": "result",
                "error_behavior": "return blocked_dag",
                "acceptance_example": "ready set is observable",
                "risk_tags": ["scheduling"],
                "worker_profile": {
                    "writer": "codex-app-visible-developer",
                    "allowlist": ["vibe_guide/dag.py", "tests/test_dag.py"],
                },
            }
            return DAGNode(
                node_id, node_id, depends or [], [], "specialized", contract, status,
                worktree=".vibe/worktrees/" + node_id,
            )

        nodes = [
            authoritative("V2-0", status="delivered"),
            authoritative("V2-1", ["V2-0"]),
            authoritative("V2-2", ["V2-0"]),
            authoritative("V2-3", ["V2-0"]),
            authoritative("V2-4", ["V2-0"]),
            authoritative("V2-5", ["V2-0"]),
            authoritative("V2-6", ["V2-0"]),
            authoritative("V2-8", ["V2-0"]),
            authoritative("V2-7", ["V2-1", "V2-2", "V2-3", "V2-4", "V2-5", "V2-6", "V2-8"]),
        ]
        result = audit_dag(Plan("v2", 5, "prd.md", [node.id for node in nodes], "authorized", nodes=nodes))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.ready_nodes, ["V2-1", "V2-2", "V2-3", "V2-4", "V2-5", "V2-6", "V2-8"])
        self.assertEqual(result.blocked_nodes, ["V2-7"])

    def test_authoritative_worker_profile_missing_or_inconsistent_identity_blocks(self):
        base = {
            "input": "request",
            "output": "result",
            "error_behavior": "return blocked_dag",
            "acceptance_example": "ready set is observable",
            "risk_tags": ["scheduling"],
        }
        missing = dict(base, worker_profile={"writer": "writer"})
        inconsistent = dict(base, writer="top-writer", worker_profile={"writer": "nested-writer", "allowlist": ["same.py"]})
        nodes = [
            DAGNode("missing", "missing", [], [], None, missing, "planned", worktree=".vibe/worktrees/missing"),
            DAGNode("inconsistent", "inconsistent", [], [], None, inconsistent, "planned", worktree=".vibe/worktrees/inconsistent"),
        ]
        result = audit_dag(Plan("p1", 1, "prd.md", [node.id for node in nodes], "authorized", nodes=nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertTrue(any("allowlist" in reason for reason in result.reasons["missing"]))
        self.assertTrue(any("writer mismatch" in reason for reason in result.reasons["inconsistent"]))
    def test_integration_after_does_not_block_readiness(self):
        n = node("n1", integration=["later"])
        self.assertEqual(ready_nodes([n]), [n])

    def test_incomplete_hard_dependency_blocks(self):
        dep = node("dep", status="running")
        child = node("child", depends=["dep"])
        self.assertEqual(ready_nodes([dep, child]), [])

    def test_only_accepted_hard_dependency_unlocks_dependent_node(self):
        child = node("child", depends=["dep"])

        self.assertEqual(ready_nodes([node("dep", status="delivered"), child]), [])
        self.assertEqual(
            ready_nodes([node("dep", status="accepted"), child]),
            [child],
        )

    def test_duplicate_ids_and_cycles_fail_validation(self):
        duplicate = validate_dag([node("n1"), node("n1")])
        self.assertFalse(duplicate.valid)
        cycle = validate_dag([node("a", depends=["b"]), node("b", depends=["a"])])
        self.assertFalse(cycle.valid)

    def test_independent_parallel_group_nodes_are_ready_together(self):
        a, b = node("a", group="g"), node("b", group="g")
        self.assertEqual(ready_nodes([a, b]), [a, b])

    def test_empty_contract_is_not_ready(self):
        self.assertEqual(ready_nodes([node("n", contract={})]), [])

    def test_incomplete_placeholder_contract_is_not_ready(self):
        self.assertEqual(ready_nodes([node("n", contract={"input": "request"})]), [])

    def test_invalid_contract_does_not_suppress_independent_ready_node(self):
        bad = node("bad", contract={})
        good = node("good")

        self.assertEqual(ready_nodes([bad, good]), [good])

    def test_whitespace_and_empty_nested_contract_fields_are_not_ready(self):
        contracts = (
            {
                "input": "   ",
                "output": "result",
                "error_behavior": "return an error",
                "acceptance_example": "example passes",
            },
            {
                "inputs": {"request": {"fields": ["account_id", "   "]}},
                "outputs": ["result"],
                "errors": ["return an error"],
                "acceptance_examples": ["example passes"],
            },
            {
                "inputs": {"request": {}},
                "outputs": ["result"],
                "errors": ["return an error"],
                "acceptance_examples": ["example passes"],
            },
        )

        for contract in contracts:
            with self.subTest(contract=contract):
                self.assertEqual(ready_nodes([node("n", contract=contract)]), [])

    def test_design_change_blocks_node(self):
        blocked_contract = {
            "input": "request",
            "output": "result",
            "error_behavior": "return an error",
            "acceptance_example": "example passes",
            "design_change": True,
        }
        self.assertEqual(ready_nodes([node("n", contract=blocked_contract)]), [])

    def test_render_plan_artifacts(self):
        fixture = Path(__file__).parent / "fixtures" / "plans" / "basic-plan.json"
        plan = Plan.from_dict(json.loads(fixture.read_text(encoding="utf-8")))
        with tempfile.TemporaryDirectory() as d:
            artifacts = render_plan_artifacts(plan, Path(d))
            self.assertTrue(artifacts.dag_path.exists())
            self.assertTrue(artifacts.plan_path.exists())

    def test_render_plan_artifacts_rejects_collision_without_overwrite(self):
        plan = Plan("p1", 1, "prd.md", ["n1"], "draft")
        with tempfile.TemporaryDirectory() as d:
            output_dir = Path(d)
            dag_path = output_dir / "dag.yaml"
            dag_path.write_text("prior evidence\n", encoding="utf-8")

            with self.assertRaises(FileExistsError):
                render_plan_artifacts(plan, output_dir)

            self.assertEqual(dag_path.read_text(encoding="utf-8"), "prior evidence\n")
            self.assertFalse((output_dir / "plan.md").exists())

    def test_audit_returns_blocked_dag_for_missing_contract(self):
        plan = Plan("p1", 1, "prd.md", ["n1"], "authorized", nodes=[audited_node("n1", contract={"output": "result"})])
        result = audit_dag(plan)
        self.assertIsInstance(result, DAGAuditResult)
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(result.ready_nodes, [])
        self.assertIn("n1", result.blocked_nodes)
        self.assertTrue(result.reasons["n1"])

    def test_audit_keeps_integration_after_non_blocking_and_reports_parallel_ready_set(self):
        nodes = [
            audited_node("V2-0", status="accepted"),
            audited_node("V2-1", depends=["V2-0"], integration=["V2-9"], group="specialized"),
            audited_node("V2-2", depends=["V2-0"], group="specialized"),
            audited_node("V2-3", depends=["V2-0"], group="specialized"),
            audited_node("V2-4", depends=["V2-0"], group="specialized"),
            audited_node("V2-5", depends=["V2-0"], group="specialized"),
            audited_node("V2-6", depends=["V2-0"], group="specialized"),
            audited_node("V2-8", depends=["V2-0"], group="specialized"),
            audited_node("V2-7", depends=["V2-1", "V2-2", "V2-3", "V2-4", "V2-5", "V2-6", "V2-8"], group="integration"),
        ]
        result = audit_dag(Plan("v2", 1, "prd.md", [n.id for n in nodes], "authorized", nodes=nodes))
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.ready_nodes, ["V2-1", "V2-2", "V2-3", "V2-4", "V2-5", "V2-6", "V2-8"])
        self.assertEqual(result.blocked_nodes, ["V2-7"])
        self.assertIn("hard dependencies", " ".join(result.reasons["V2-7"]))

    def test_audit_blocks_hard_cycle_and_missing_dependency(self):
        nodes = [audited_node("a", depends=["b"]), audited_node("b", depends=["a"]), audited_node("c", depends=["missing"])]
        result = audit_dag(Plan("p1", 1, "prd.md", ["a", "b", "c"], "authorized", nodes=nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertEqual(set(result.blocked_nodes), {"a", "b", "c"})
        self.assertTrue(any("cycle" in reason for reason in result.reasons["a"]))
        self.assertTrue(any("unknown hard dependenc" in reason for reason in result.reasons["c"]))

    def test_audit_blocks_duplicate_writer_and_allowlist_mismatch(self):
        nodes = [
            audited_node("a", writer="same-writer", allowlist=["same.py"], contract={"worktree": ".vibe/worktrees/shared"}),
            audited_node("b", writer="same-writer", allowlist=["same.py"], contract={"worktree": ".vibe/worktrees/shared"}),
            audited_node("c", contract={"allowlist": ["c.py"], "writer": "writer-c"}, allowlist=["other.py"]),
        ]
        result = audit_dag(Plan("p1", 1, "prd.md", ["a", "b", "c"], "authorized", nodes=nodes))
        self.assertEqual(result.status, "blocked_dag")
        self.assertTrue(any("duplicate writer" in reason for reason in result.reasons["a"]))
        self.assertTrue(any("allowlist" in reason for reason in result.reasons["c"]))

    def test_render_includes_audit_contract_and_identity_evidence(self):
        n1 = audited_node("n1", status="accepted")
        n2 = audited_node("n2", depends=["n1"])
        plan = Plan("p1", 1, "prd.md", ["n1", "n2"], "authorized", nodes=[n1, n2])
        with tempfile.TemporaryDirectory() as d:
            artifacts = render_plan_artifacts(plan, Path(d))
            data = json.loads(artifacts.dag_path.read_text(encoding="utf-8"))
            self.assertEqual(data["audit"]["status"], "ready")
            rendered = {item["id"]: item for item in data["nodes"]}
            self.assertEqual(rendered["n2"]["writer"], "writer-n2")
            self.assertEqual(rendered["n2"]["allowlist"], ["vibe_guide/n2.py"])
            self.assertEqual(rendered["n2"]["depends_on"], ["n1"])

    def test_render_plan_artifacts_rolls_back_partial_publish(self):
        plan = Plan("p1", 1, "prd.md", ["n1"], "draft")
        real_link = os.link
        calls = 0

        def fail_second_link(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated second publish failure")
            return real_link(source, destination)

        with tempfile.TemporaryDirectory() as d:
            output_dir = Path(d)
            with patch("os.link", side_effect=fail_second_link):
                with self.assertRaisesRegex(OSError, "second publish failure"):
                    render_plan_artifacts(plan, output_dir)

            self.assertFalse((output_dir / "dag.yaml").exists())
            self.assertFalse((output_dir / "plan.md").exists())


if __name__ == "__main__":
    unittest.main()
