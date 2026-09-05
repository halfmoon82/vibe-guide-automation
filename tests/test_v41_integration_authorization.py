import unittest

from vibe_guide.authorization import build_authorization_card, refresh_authorization_card
from vibe_guide.workflow_gate import verify_workflow, REQUIRED_COMPLEX_WORKFLOW
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


class IntegrationAuthorizationTests(unittest.TestCase):
    def _plan_nodes(self):
        nodes = [
            DAGNode(id="issue", title="Issue", status="planned", depends_on=[], integration_after=[], parallel_group="g", contract={"worker": "w", "files": ["a.py"]}),
            DAGNode(id="integration-review", title="Integration Review", status="planned", depends_on=["issue"], integration_after=[], parallel_group="integration", contract={"worker": "w2", "reviewer": "reviewer", "files": ["a.py"], "read_only": True}),
        ]
        plan = Plan("p", 1, "prd.md", [n.id for n in nodes], "draft", complexity_band="complex", integration_contract={"prd_ref": "prd.md", "spec_ref": "spec.md", "plan_revision": 1, "scope": ["issue"], "acceptance": ["P0-P2=0"], "reviewer": "reviewer"}, nodes=nodes)
        return plan, nodes

    def test_card_binds_integration_chain_contract_and_remote_switch(self):
        plan, nodes = self._plan_nodes()
        card = build_authorization_card(plan, nodes, AgentCapabilities("codex", True, True, True, True, True, "full"), remote_git_actions="deny")
        self.assertIn("integration-review", card.node_ids)
        self.assertIn("integration-review", card.integration_node_id)
        self.assertTrue(any("P0" in item and "P2" in item for item in card.integration_review_scope))
        self.assertEqual(card.remote_git_actions, "deny")
        self.assertIn("deploy", card.excluded_actions)
        self.assertIn("merge", card.excluded_actions)

    def test_explicit_skip_is_recorded(self):
        plan, nodes = self._plan_nodes()
        with self.assertRaises(ValueError):
            build_authorization_card(plan, nodes, AgentCapabilities("codex", False, False, False, False, False, "guide"), skipped_nodes=("issue",), workflow={"task_id": "t", "nodes": ["issue"], "node_records": {"issue": {"status": "skipped_by_user"}}})

    def test_malformed_workflow_cannot_complete(self):
        for workflow in ({"task_id": "t", "node_records": {}}, {"task_id": "t", "nodes": [], "node_records": {}}, {"task_id": "t", "nodes": ["ghost"], "node_records": {}}):
            self.assertEqual(verify_workflow(workflow)["status"], "blocked_by_required_node")

    def test_workflow_nodes_must_be_exact_route_sequence(self):
        self.assertEqual(verify_workflow({"task_id": "t", "nodes": ["s0"], "node_records": {}})["status"], "blocked_by_required_node")

    def test_completed_record_lineage_tamper_is_blocked(self):
        sequence = list(REQUIRED_COMPLEX_WORKFLOW)
        workflow = {"task_id": "t", "route": "complex", "nodes": sequence, "node_records": {}}
        for index, node in enumerate(sequence, 1):
            workflow["node_records"][node] = {"task_id": "t", "node_id": node, "status": "completed", "input": {"v": 1}, "output": {"v": 1}, "evidence": {"v": 1}, "sequence": index}
        workflow["node_records"]["s0"]["task_id"] = "tampered"
        self.assertEqual(verify_workflow(workflow)["status"], "blocked_by_required_node")

    def test_complex_workflow_requires_authorization_granted(self):
        sequence = list(REQUIRED_COMPLEX_WORKFLOW)
        workflow = {"task_id": "t", "route": "complex", "nodes": sequence, "node_records": {node: {"task_id": "t", "node_id": node, "status": "completed", "input": {"v": 1}, "output": {"v": 1}, "evidence": {"v": 1}, "sequence": index} for index, node in enumerate(sequence, 1)}}
        self.assertEqual(verify_workflow(workflow)["status"], "blocked_by_required_node")

    def test_complex_refresh_requires_preserved_complete_workflow(self):
        plan, nodes = self._plan_nodes()
        card = build_authorization_card(plan, nodes, AgentCapabilities("codex", False, False, False, False, False, "guide"))
        with self.assertRaises(ValueError):
            refresh_authorization_card(plan, nodes, card)
        sequence = ("s0", "s1", "requirements", "product_decision", "prd", "spec_issue", "dag_audit", "plan_confirmation", "authorization_card", "user_authorization")
        workflow = {"task_id": "t", "route": "complex", "nodes": list(sequence), "authorization_granted": True, "node_records": {node: {"task_id": "t", "node_id": node, "status": "completed", "input": {"v": 1}, "output": {"v": 1}, "evidence": {"v": 1}, "sequence": index} for index, node in enumerate(sequence, 1)}}
        self.assertEqual(refresh_authorization_card(plan, nodes, card, workflow=workflow).plan_id, plan.plan_id)


if __name__ == "__main__":
    unittest.main()
