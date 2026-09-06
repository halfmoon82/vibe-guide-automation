import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorization import build_authorization_card
from vibe_guide.cli import _publish_plan
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths


class V310PlanRepairTests(unittest.TestCase):
    def test_published_plan_keeps_executable_nodes_for_runtime_audit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "nodes.json"
            source.write_text(json.dumps({
                "title": "repair",
                "objective": "verify executable plan",
                "capabilities": {
                    "agent_id": "fixture",
                    "shell": True,
                    "subprocess": True,
                    "worktree": True,
                    "background": True,
                    "session_resume": True,
                    "level": "background",
                },
                "allowed_actions": ["develop", "test", "review", "merge_local", "merge_remote"],
                "nodes": [{
                    "id": "n1", "title": "N1", "depends_on": [],
                    "integration_after": [], "parallel_group": "core", "status": "planned",
                    "contract": {
                        "input": "request", "output": "delivery",
                        "error_behavior": "blocked_unknown", "acceptance_example": "pass",
                        "risk_tags": ["test"], "writer": "writer-n1",
                        "worktree": ".vibe/worktrees/n1", "branch": "codex/n1",
                        "files": ["n1.py"], "actions": ["develop", "test", "review"]
                    }
                }]
            }), encoding="utf-8")
            plan, nodes, card = _publish_plan(ProjectPaths(root), "repair-plan", source)
            self.assertEqual([node.id for node in plan.nodes], ["n1"])
            self.assertTrue(plan.authorization_required)
            self.assertIn("merge_local", card.allowed_actions)
            self.assertIn("merge_remote", card.allowed_actions)

    def test_v310_card_can_preauthorize_restricted_merge_without_push(self):
        node = DAGNode(
            "n1", "N1", [], [], "core",
            {"input": "request", "output": "delivery", "error_behavior": "blocked_unknown",
             "acceptance_example": "pass", "risk_tags": ["merge"], "writer": "writer",
             "worktree": ".vibe/worktrees/n1", "branch": "codex/n1", "files": ["n1.py"]},
            "planned",
        )
        plan = Plan("vibe-guide-v3.10", 1, "prd.md", ["n1"], "confirmed_pending_authorization")
        card = build_authorization_card(
            plan, [node], AgentCapabilities("fixture", True, True, True, True, True, "background"),
            allowed_actions=("develop", "test", "review", "rework", "merge_local", "merge_remote"),
        )
        self.assertIn("merge_local", card.allowed_actions)
        self.assertIn("merge_remote", card.allowed_actions)
        self.assertNotIn("push", card.allowed_actions)


if __name__ == "__main__":
    unittest.main()
