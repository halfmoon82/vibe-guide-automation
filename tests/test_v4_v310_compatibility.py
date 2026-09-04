import unittest

from vibe_guide.models import Plan
from vibe_guide.planner import project_v310_capabilities


class V4310CompatibilityTests(unittest.TestCase):
    def test_inherits_v310_flags_and_overrides_only_binding_frontend(self):
        plan = Plan("v4-plan", 1, "prd.md", ["n1"], "draft",
                    route_result={"route": "complex", "complexity_band": "complex", "score": 16}, decisions=[])
        projection = project_v310_capabilities(plan)
        for key in ("installation", "upgrade", "capability", "s0_s1", "dag", "review", "release_evidence"):
            self.assertTrue(projection[key])
        self.assertFalse(projection["stage_a"])
        self.assertFalse(projection["stage_e"])
        self.assertFalse(projection["provider_hard_gate"])
        self.assertEqual(projection["workflow_version"], 4)
        self.assertEqual(projection["execution_mode"], "sdd_first")

    def test_unknown_plan_fields_are_read_only_preserved(self):
        plan = Plan("v4-plan", 1, "prd.md", ["n1"], "draft", decisions=[])
        plan.extra_v310 = {"custom": "value"}
        self.assertEqual(project_v310_capabilities(plan)["legacy_fields"]["extra_v310"], {"custom": "value"})

    def test_legacy_plan_from_dict_remains_compatible(self):
        plan = Plan.from_dict({"plan_id": "legacy", "version": 1, "prd_path": "prd.md",
                               "node_ids": [], "status": "draft"})
        self.assertEqual(plan.plan_id, "legacy")
