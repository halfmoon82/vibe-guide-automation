import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.planner import S0Result, S1Score, TaskContext, build_routing_decision, render_routing_decision, route_task, score_s1


class V3RoutingVisibilityTests(unittest.TestCase):
    def test_route_boundaries_are_visible_and_have_artifact_policy(self):
        expected = ((8, "simple"), (9, "light_plan"), (15, "light_plan"), (16, "complex"))
        for total, route in expected:
            with self.subTest(total=total):
                score = S1Score(total, 1, 1, 1, 1, 1)
                self.assertEqual(route_task(score), route)

    def test_routing_decision_has_screen_next_action_evidence_and_state_policy(self):
        for total, route in ((8, "simple"), (9, "light_plan"), (16, "complex")):
            with self.subTest(route=route):
                score = S1Score(total, 1, 1, 1, 1, 1)
                screen = S0Result(route == "simple", route != "simple", route, "test")
                decision = build_routing_decision(screen, score)
                self.assertEqual(decision["screen"], "s0" if route == "simple" else "s1")
                self.assertTrue(decision["next_action"])
                self.assertTrue(decision["evidence_ref"])
                self.assertIn(decision["next_action"], render_routing_decision(decision))
                policy = decision["artifact_policy"]
                if route == "simple":
                    self.assertEqual(policy["monitor"], "not_available")
                    self.assertEqual(policy["plan.json"], "not_generated")
                elif route == "light_plan":
                    self.assertEqual(set(policy.values()), {"not_generated", "unavailable_without_complex_replan"})
                else:
                    self.assertEqual(policy["plan.json"], "generated_after_plan_id_and_node_spec")
                    self.assertEqual(policy["nodes.json"], "generated_after_plan_id_and_node_spec")
                    self.assertEqual(policy["authorization-card.json"], "generated_after_plan_confirmation")

    def test_light_plan_is_explicitly_non_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            result = run_cli(["plan", "--request", "设计一个小功能", "--s1", "2,2,2,2,1", "--json"], root)
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.payload["route"], "light_plan")
            decision = result.payload["routing_decision"]
            self.assertEqual(decision["screen"], "s1")
            self.assertIn("complex", decision["next_action"])
            self.assertEqual(decision["artifact_policy"]["monitor"], "unavailable_without_complex_replan")
            self.assertFalse((root / ".vibe" / "plans").exists())

    def test_complex_missing_inputs_keep_routing_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            result = run_cli(["plan", "--request", "设计并实现一个复杂系统", "--s1", "4,4,4,4,0", "--json"], root)
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["route"], "complex")
            decision = result.payload["routing_decision"]
            self.assertIn("plan_id", decision["next_action"])
            self.assertEqual(decision["artifact_policy"]["plan.json"], "unavailable_without_complex_replan")

    def test_v2_fallback_complex_error_keeps_unavailable_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            result = run_cli(["plan", "--request", "设计并实现一个复杂系统", "--s1", "4,4,4,4,0", "--json"], root)
            self.assertEqual(result.payload["route"], "complex")
            self.assertIn("routing_decision", result.payload)
            self.assertEqual(result.payload["routing_decision"]["artifact_policy"]["monitor"], "unavailable_without_complex_replan")

    def test_invalid_s1_is_usage_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            result = run_cli(["plan", "--request", "设计一个小功能", "--s1", "1,2", "--json"], root)
            self.assertEqual(result.exit_code, 2)
            self.assertEqual(result.payload["status"], "usage_error")

    def test_missing_plan_returns_planning_required_without_provider_side_effect(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            result = run_cli(["monitor", "--plan", "missing", "--authorize", "AUTHORIZE", "--json"], root)
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["status"], "planning_required")
            diagnostic = result.payload["diagnostic"]
            self.assertEqual(diagnostic["reason"], "plan_artifact_missing")
            self.assertEqual(diagnostic["route_hint"], "light_plan_or_unpublished_complex_plan")
            self.assertFalse(diagnostic["run_created"])
            self.assertFalse(diagnostic["worker_created"])
            self.assertFalse(diagnostic["provider_action_created"])
            self.assertIn("plan_artifact_missing", result.text)
            self.assertIn("run_created=False", result.text)
            self.assertIn("plan", diagnostic["missing"])
            self.assertFalse((root / ".vibe" / "runs").exists())


if __name__ == "__main__":
    unittest.main()
