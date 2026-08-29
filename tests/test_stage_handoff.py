import json
import tempfile
import unittest

from vibe_guide.models import PRD, StageHandoff
from vibe_guide.stage_handoff import build_stage_handoff, handoff_for_prd, load_stage_handoff, save_stage_handoff


class StageHandoffTests(unittest.TestCase):
    def test_builder_defaults_unknown_status_without_authorizing(self):
        handoff = build_stage_handoff(stage="spec_issue_dag")
        self.assertEqual(handoff.status, "blocked_unknown")
        self.assertEqual(handoff.required_user_action, "answer_question")
        self.assertFalse(handoff.authorizes)

    def test_renderer_exposes_next_stage_readiness_and_non_authorizing_boundary(self):
        handoff = build_stage_handoff(
            stage="prd_approved",
            status="approved",
            plan_id="plan-1",
            plan_revision=3,
            evidence_refs=["prd:vibe-guide@3"],
        )
        self.assertEqual(handoff.to_stage, "spec_issue_dag")
        self.assertEqual(handoff.readiness, "ready")
        rendered = handoff.render()
        self.assertIn("下一阶段：spec_issue_dag", rendered)
        self.assertIn("就绪：ready", rendered)
        self.assertIn("create_run", handoff.forbidden_automatic_actions)
        self.assertIn("archive", handoff.forbidden_automatic_actions)
        self.assertFalse(handoff.authorizes)
        self.assertFalse(handoff.creates_worker)

    def test_blocked_design_prd_requires_an_answer_not_continue_planning(self):
        handoff = build_stage_handoff(
            stage="prd_approved",
            status="blocked_design",
            plan_id="plan-1",
            plan_revision=3,
            open_questions=["确认产品方向"],
        )
        self.assertEqual(handoff.required_user_action, "answer_question")
        self.assertEqual(handoff.readiness, "blocked_design")

    def test_blocked_prd_handoff_preserves_design_block_and_requires_answer(self):
        handoff = handoff_for_prd(
            PRD("Draft", "Objective", status="blocked_design"),
            open_questions=["确认产品方向"],
        )
        self.assertEqual(handoff.status, "blocked_design")
        self.assertEqual(handoff.required_user_action, "answer_question")

    def test_blocked_handoffs_without_questions_synthesize_one_recoverable_question(self):
        for status in ("blocked_design", "blocked_unknown"):
            with self.subTest(status=status):
                handoff = build_stage_handoff(
                    stage="spec_issue_dag",
                    status=status,
                )
                self.assertEqual(handoff.required_user_action, "answer_question")
                self.assertEqual(len(handoff.open_questions), 1)
                self.assertTrue(handoff.open_questions[0])
        handoff = handoff_for_prd(PRD("Draft", "Objective", status="blocked_design"))
        self.assertEqual(handoff.required_user_action, "answer_question")
        self.assertEqual(len(handoff.open_questions), 1)

    def test_direct_stage_handoff_constructor_is_fail_closed_for_blocked_status(self):
        for status in ("blocked_design", "blocked_unknown"):
            with self.subTest(status=status):
                handoff = StageHandoff(status=status)
                self.assertEqual(handoff.required_user_action, "answer_question")
                self.assertEqual(len(handoff.open_questions), 1)

    def test_custom_forbidden_actions_cannot_remove_mandatory_non_authorizing_actions(self):
        handoff = build_stage_handoff(
            stage="prd_approved",
            status="approved",
            forbidden_automatic_actions=["create_spec"],
        )
        mandatory = {
            "authorize", "monitor", "create_worker", "create_run", "archive", "deploy"
        }
        self.assertTrue(mandatory.issubset(set(handoff.forbidden_automatic_actions)))

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
