import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.dag import ready_nodes, render_plan_artifacts, validate_dag
from vibe_guide.models import DAGNode, Plan


def node(node_id, depends=None, integration=None, group=None, status="planned", contract=None):
    default_contract = {
        "input": "request",
        "output": "result",
        "error_behavior": "return an error",
        "acceptance_example": "example passes",
    }
    return DAGNode(node_id, node_id, depends or [], integration or [], group, default_contract if contract is None else contract, status)


class DAGTests(unittest.TestCase):
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
