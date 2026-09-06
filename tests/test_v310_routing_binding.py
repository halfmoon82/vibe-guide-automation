import unittest

from vibe_guide.models import Plan, TargetContract
from vibe_guide.planner import RouteResult, TaskContext, collect_target_contract, route_task


class V310RoutingBindingTests(unittest.TestCase):
    def test_s1_boundaries_and_persisted_route_result(self):
        for total, expected in ((8, "simple"), (9, "light_plan"), (15, "light_plan"), (16, "complex")):
            dimensions = [min(5, total), min(5, max(0, total - 5)), min(5, max(0, total - 10)), min(5, max(0, total - 15)), min(5, max(0, total - 20))]
            context = TaskContext(*dimensions)
            result = route_task(context)
            self.assertIsInstance(result, RouteResult)
            self.assertEqual(result.route, expected)
            self.assertEqual(result.complexity_band, expected)
            self.assertEqual(result.to_dict()["complexity_band"], expected)

    def test_forced_upgrade_flag_enters_complex(self):
        context = TaskContext(steps=1, domains=1, uncertainty=1, failure_cost=1, toolchain=1, force_upgrade_flags=["independent_review"])
        self.assertEqual(route_task(context).route, "complex")

    def test_target_contract_auto_fills_unique_environment_and_freezes(self):
        contract = collect_target_contract(
            {
                "provider": "gitlab",
                "repository": "acme/demo",
                "target_branch": "main",
                "issue_type": "MR",
                "source_branch": "feature/x",
                "file_scope": ["vibe_guide/planner.py"],
                "merge_method": "squash",
            },
            None,
        )
        self.assertTrue(contract.frozen)
        self.assertEqual(contract.repository, "acme/demo")
        self.assertEqual(len(contract.digest), 64)
        self.assertEqual(contract.to_dict()["digest"], contract.digest)

    def test_target_contract_requires_one_selection_when_ambiguous_then_reuses(self):
        pending = collect_target_contract(
            {"provider": "git", "repository": ["a", "b"], "target_branch": ["main", "release"]},
            None,
        )
        self.assertFalse(pending.frozen)
        self.assertIn("repository", pending.missing_fields)
        self.assertIn("target_branch", pending.missing_fields)
        frozen = collect_target_contract(
            {"provider": "git", "repository": ["a", "b"], "target_branch": ["main", "release"]},
            {"repository": "a", "target_branch": "main", "issue_type": "PR", "source_branch": "feature/x", "file_scope": ["a.py"], "merge_method": "merge"},
        )
        self.assertTrue(frozen.frozen)
        reused = collect_target_contract(frozen.to_dict(), None)
        self.assertEqual(reused.digest, frozen.digest)
        self.assertTrue(reused.frozen)

    def test_plan_can_persist_route_and_target_projection(self):
        route = route_task(TaskContext(1, 1, 1, 1, 1))
        target = collect_target_contract({"provider": "git", "repository": "demo", "target_branch": "main", "issue_type": "PR", "source_branch": "x", "file_scope": ["a.py"], "merge_method": "merge"}, None)
        plan = Plan("p310", 1, "prd.md", [], "draft", routing_result=route.to_dict(), target_contract=target.to_dict(), target_contract_digest=target.digest)
        restored = Plan.from_dict(plan.to_dict())
        self.assertEqual(restored.routing_result["complexity_band"], "simple")
        self.assertEqual(restored.target_contract_digest, target.digest)

    def test_issue_complexity_accepts_light_plan_boundaries(self):
        for total in (9, 15, 16):
            dims = [min(5, total), min(5, max(0, total - 5)), min(5, max(0, total - 10)), min(5, max(0, total - 15)), min(5, max(0, total - 20))]
            issue = __import__("vibe_guide.planner", fromlist=["classify_v310_task"]).classify_v310_task("s1", TaskContext(*dims))
            self.assertIn(issue.complexity_band, ("light", "complex"))

    def test_frozen_contract_cannot_be_empty_or_have_conflicting_aliases(self):
        with self.assertRaises(ValueError):
            TargetContract(frozen=True, status="frozen")
        pending = collect_target_contract({"provider": "git", "repository": "a", "repo": "b", "target_branch": "main", "issue_type": "PR", "source_branch": "x", "file_scope": ["a.py"], "merge_method": "merge"}, None)
        self.assertFalse(pending.frozen)
        self.assertIn("repository", pending.missing_fields)

    def test_plan_rejects_conflicting_route_projection(self):
        with self.assertRaises(ValueError):
            Plan("p310-conflict", 1, "prd.md", [], "draft", routing_result={"complexity_band": "complex", "force_upgrade_flags": []}, complexity_band="simple")

    def test_frozen_contract_scope_is_immutable(self):
        contract = collect_target_contract({"provider": "git", "repository": "demo", "target_branch": "main", "issue_type": "PR", "source_branch": "x", "file_scope": ["a.py"], "merge_method": "merge"}, None)
        original = contract.digest
        with self.assertRaises(AttributeError):
            contract.file_scope.append("b.py")
        self.assertEqual(contract.digest, original)

    def test_plan_rejects_conflicting_routing_alias_payloads(self):
        with self.assertRaises(ValueError):
            Plan("p310-alias", 1, "prd.md", [], "draft", routing_result={"route": "complex", "complexity_band": "complex", "score": 16, "force_upgrade_flags": []}, route_result={"route": "simple", "complexity_band": "simple", "score": 8, "force_upgrade_flags": []})

    def test_plan_rejects_bogus_route_projection(self):
        with self.assertRaises(ValueError):
            Plan("p310-bogus", 1, "prd.md", [], "draft", routing_result={"route": "bogus", "complexity_band": "bogus", "force_upgrade_flags": []})

    def test_plan_rejects_score_route_force_inconsistency(self):
        bad = [
            {"route": "simple", "complexity_band": "simple", "score": 16, "force_upgrade_flags": []},
            {"route": "simple", "complexity_band": "simple", "score": 1, "force_upgrade_flags": ["review"]},
            {"route": "light_plan", "complexity_band": "light_plan", "score": 8, "force_upgrade_flags": []},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    Plan("p310-score", 1, "prd.md", [], "draft", routing_result=payload)

    def test_route_result_rejects_duplicate_force_flags(self):
        with self.assertRaises(ValueError):
            RouteResult("complex", "complex", score=16, force_upgrade_flags=["review", "review"])


if __name__ == "__main__":
    unittest.main()
