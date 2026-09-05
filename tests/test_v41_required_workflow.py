import unittest

from vibe_guide.workflow_gate import (
    REQUIRED_COMPLEX_WORKFLOW,
    create_task_workflow,
    record_workflow_node,
    skip_workflow_node,
    verify_workflow,
)
from vibe_guide.planner import TaskContext, route_task
from vibe_guide.authorization import remote_git_actions_allowed
from vibe_guide.authorization import build_authorization_card, authorize, AuthorizationRecord
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


class RequiredWorkflowTests(unittest.TestCase):
    def test_each_task_gets_fresh_s0_s1_and_only_complex_projects_chain(self):
        first = create_task_workflow("t1", TaskContext(1, 1, 1, 1, 1))
        second = create_task_workflow("t2", TaskContext(5, 5, 5, 5, 5))
        self.assertEqual(first["task_id"], "t1")
        self.assertEqual(first["nodes"], ["s0", "s1"])
        self.assertEqual(second["nodes"], REQUIRED_COMPLEX_WORKFLOW)
        self.assertNotEqual(first.get("route"), second.get("route"))

    def test_complex_missing_or_out_of_order_and_self_report_without_evidence_blocks(self):
        workflow = create_task_workflow("t", TaskContext(5, 5, 5, 5, 5))
        record_workflow_node(workflow, "s0", {"request": "x"}, {"route": "s1"}, {"ref": "s0"})
        record_workflow_node(workflow, "s1", {"score": 25}, {"route": "complex"}, {"ref": "s1"})
        record_workflow_node(workflow, "prd", {}, {"self_report": "done"}, {})
        self.assertEqual(verify_workflow(workflow)["status"], "blocked_by_required_node")

    def test_explicit_skip_is_not_pass_and_authorization_skip_does_not_authorize(self):
        workflow = create_task_workflow("t", TaskContext(5, 5, 5, 5, 5))
        skip_workflow_node(workflow, "s0", "skip s0", "not needed", "changes evidence", False)
        node = workflow["node_records"]["s0"]
        self.assertEqual(node["status"], "skipped_by_user")
        self.assertNotEqual(node["status"], "passed")
        self.assertFalse(workflow["authorization_granted"])

    def test_node_records_are_structured_and_monitor_checks_lineage(self):
        workflow = create_task_workflow("t", TaskContext(5, 5, 5, 5, 5))
        record_workflow_node(workflow, "s0", {"request": "x"}, {"route": "s1"}, {"ref": "s0"})
        record_workflow_node(workflow, "s1", {"score": 25}, {"route": "complex"}, {"ref": "s1"})
        self.assertEqual(verify_workflow(workflow)["status"], "blocked_by_required_node")
        self.assertIn("input", workflow["node_records"]["s0"])

    def test_remote_git_actions_is_single_switch_and_deploy_is_independent(self):
        self.assertTrue(remote_git_actions_allowed({"remote_git_actions": "allow"}, "commit"))
        self.assertFalse(remote_git_actions_allowed({"remote_git_actions": "deny"}, "merge"))
        self.assertFalse(remote_git_actions_allowed({"remote_git_actions": "allow"}, "deploy"))

    def test_authorization_card_persists_switch_and_authorized_record_verifies_it(self):
        node = DAGNode("n1", "node", [], [], "g", {"worker": "w1"}, "planned")
        plan = Plan("p", 1, "prd", ["n1"], "confirmed_pending_authorization", nodes=[node])
        caps = AgentCapabilities("agent", False, False, False, False, False, "guide")
        card = build_authorization_card(plan, [node], caps, remote_git_actions="allow")
        self.assertEqual(card.remote_git_actions, "allow")
        record = authorize(card, "AUTHORIZE")
        self.assertTrue(remote_git_actions_allowed(record, "push"))

    def test_authorization_record_roundtrip_accepts_persisted_remote_switch(self):
        node = DAGNode("n1", "node", [], [], "g", {"worker": "w1"}, "planned")
        plan = Plan("p2", 1, "prd", ["n1"], "confirmed_pending_authorization", nodes=[node])
        caps = AgentCapabilities("agent", False, False, False, False, False, "guide")
        record = authorize(build_authorization_card(plan, [node], caps, remote_git_actions="allow"), "AUTHORIZE")
        restored = AuthorizationRecord.from_dict(record.to_dict())
        self.assertEqual(restored.remote_git_actions, "allow")


if __name__ == "__main__":
    unittest.main()
