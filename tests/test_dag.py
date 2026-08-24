import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.dag import ready_nodes, render_plan_artifacts, validate_dag
from vibe_guide.models import DAGNode, Plan


def node(node_id, depends=None, integration=None, group=None, status="pending", contract=None):
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


if __name__ == "__main__":
    unittest.main()
