import unittest

from vibe_guide.dag import audit_dag
from vibe_guide.models import DAGNode, Plan
from vibe_guide.planner import (
    PlanConfirmation,
    build_plan_confirmation,
    confirm_plan,
    is_plan_confirmation_valid,
)


def node(node_id="n"):
    contract = {
        "input": "request", "output": "result", "error_behavior": "blocked",
        "acceptance_examples": ["works"], "risk_tags": ["integrity"],
        "adapter_id": "codex", "provider": "codex-app-visible", "writer": "w-" + node_id,
        "reviewer": "r-" + node_id, "worktree": ".vibe/worktrees/" + node_id,
        "branch": "codex/" + node_id, "allowlist": ["vibe_guide/" + node_id + ".py"],
    }
    return DAGNode(node_id, node_id, [], [], "planning", contract, "accepted",
                   writer="w-" + node_id, worktree=contract["worktree"], allowlist=contract["allowlist"])


class PlanConfirmationTests(unittest.TestCase):
    def test_confirmation_binds_exact_revision_nodes_and_audit_digest(self):
        plan = Plan("v3", 4, "prd.md", ["n"], "draft", nodes=[node()])
        audit = audit_dag(plan)
        confirmation = build_plan_confirmation(plan, audit, "CONFIRM_PLAN")
        self.assertIsInstance(confirmation, PlanConfirmation)
        self.assertTrue(is_plan_confirmation_valid(confirmation, plan, audit))
        self.assertEqual(confirmation.plan_revision, 4)
        self.assertEqual(confirmation.node_ids, ("n",))

    def test_confirmation_rejects_stale_revision_or_tampered_digest(self):
        plan = Plan("v3", 4, "prd.md", ["n"], "draft", nodes=[node()])
        audit = audit_dag(plan)
        confirmation = confirm_plan(plan, audit, "CONFIRM_PLAN")
        stale = Plan("v3", 3, "prd.md", ["n"], "draft", nodes=[node()])
        self.assertFalse(is_plan_confirmation_valid(confirmation, stale, audit_dag(stale)))
        payload = confirmation.to_dict()
        payload["digest"] = "0" * 64
        self.assertFalse(is_plan_confirmation_valid(PlanConfirmation.from_dict(payload), plan, audit))

    def test_confirmation_requires_exact_user_action_and_schema(self):
        plan = Plan("v3", 4, "prd.md", ["n"], "draft", nodes=[node()])
        audit = audit_dag(plan)
        with self.assertRaises(ValueError):
            build_plan_confirmation(plan, audit, "yes")
        payload = build_plan_confirmation(plan, audit, "CONFIRM_PLAN").to_dict()
        payload.pop("schema_version")
        with self.assertRaises(ValueError):
            PlanConfirmation.from_dict(payload)
        payload = build_plan_confirmation(plan, audit, "CONFIRM_PLAN").to_dict()
        payload["extra"] = True
        with self.assertRaises(ValueError):
            PlanConfirmation.from_dict(payload)


if __name__ == "__main__":
    unittest.main()
