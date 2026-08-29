import json
import tempfile
import unittest

from vibe_guide.models import StageHandoff
from vibe_guide.stage_handoff import build_stage_handoff, load_stage_handoff, save_stage_handoff


class StageHandoffTests(unittest.TestCase):
    def test_handoff_is_json_safe_and_approved_prd_only_exposes_planning(self):
        handoff = build_stage_handoff(
            stage="prd_approved",
            status="approved",
            plan_id="plan-1",
            plan_revision=2,
            evidence_refs=["prd:plan-1@2"],
        )
        restored = StageHandoff.from_dict(json.loads(json.dumps(handoff.to_dict())))
        self.assertEqual(restored.stage, "prd_approved")
        self.assertEqual(restored.required_user_action, "continue_planning")
        self.assertIn("create_worker", restored.forbidden_automatic_actions)
        self.assertIn("authorize", restored.forbidden_automatic_actions)
        self.assertNotIn("authorize_execution", restored.render())

    def test_planning_required_handoff_keeps_open_questions(self):
        handoff = build_stage_handoff(
            stage="spec_issue_dag",
            status="planning_required",
            plan_id="plan-1",
            plan_revision=1,
            evidence_refs=[],
            open_questions=["补齐真实 DAG 审计"],
        )
        self.assertEqual(handoff.required_user_action, "continue_planning")
        self.assertEqual(handoff.open_questions, ["补齐真实 DAG 审计"])

    def test_handoff_round_trips_through_atomic_json_file(self):
        handoff = build_stage_handoff("prd_approved", "approved", "plan-1", 1, ["prd:1"])
        with tempfile.TemporaryDirectory() as directory:
            path = __import__("pathlib").Path(directory) / "handoff.json"
            save_stage_handoff(path, handoff)
            self.assertEqual(load_stage_handoff(path).to_dict(), handoff.to_dict())


if __name__ == "__main__":
    unittest.main()
