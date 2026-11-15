"""V6 P2: three-tier entry (direct / lean / run) and the routing replay.

A9 (design): a high-score single node and a long serial node are not split by
force, and a continue message is not re-planned.  B12/B13 (PRD): history and
synthetic boundaries are reported separately, grouped by root task, with no
future information and no filled-in unknowns; a risk or authorization omission
is its own veto.
"""
import json
import unittest
from pathlib import Path

from vibe_guide.planner import (
    OrchestrationFacts, SAFEGUARDS, decide_execution_tier, parse_s1_context,
)
from vibe_guide.session_entry import entry_route, replay_routing

FIXTURES = Path(__file__).parent / "fixtures" / "v6_routing_replay"


def _load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _decide(score, **facts):
    return decide_execution_tier(score, OrchestrationFacts(**facts))


class TierDecisionTests(unittest.TestCase):
    def test_design_boundaries(self):
        cases = [
            (12, dict(nodes=1, parallel=1, duration="session"), "direct"),
            (14, dict(nodes=3, parallel=3, duration="session"), "lean"),
            (18, dict(nodes=2, parallel=2, duration="session"), "lean"),
            (19, dict(nodes=8, parallel=4, duration="multiday"), "run"),
            (20, dict(nodes=1, parallel=1, duration="multiday"), "lean"),
            (16, dict(nodes=4, duration="resume", coordination=True), "run"),
        ]
        for score, facts, tier in cases:
            with self.subTest(score=score, facts=facts):
                self.assertEqual(_decide(score, **facts).tier, tier)

    def test_high_score_single_node_is_not_split(self):
        decision = _decide(19, nodes=1, parallel=1, duration="session")
        self.assertEqual(decision.tier, "direct")
        self.assertTrue(decision.reason)

    def test_score_contradicting_size_gives_reason_not_hard_mapping(self):
        decision = _decide(8, nodes=10, parallel=3, duration="multiday")
        self.assertNotEqual(decision.tier, "direct")
        self.assertIn("矛盾", decision.contradiction or "")
        self.assertIn("评分", decision.reason)

    def test_unknown_facts_stay_unknown_and_do_not_pick_run(self):
        decision = _decide(17)
        self.assertEqual(decision.tier, "lean")
        self.assertIn("nodes", decision.unknown_facts)
        self.assertNotEqual(_decide(7).tier, "run")

    def test_explicit_full_flow_request_selects_run(self):
        self.assertEqual(_decide(5, nodes=1, explicit_run=True).tier, "run")

    def test_node_count_alone_does_not_force_run(self):
        self.assertEqual(_decide(14, nodes=12, parallel=2, duration="session").tier, "lean")

    def test_safeguards_are_identical_across_tiers(self):
        tiers = {_decide(s, **f).tier: _decide(s, **f).safeguards for s, f in [
            (3, dict(nodes=1)), (14, dict(nodes=3, parallel=3)),
            (19, dict(nodes=8, parallel=4, duration="multiday"))]}
        self.assertEqual(set(tiers), {"direct", "lean", "run"})
        for safeguards in tiers.values():
            self.assertEqual(safeguards, SAFEGUARDS)

    def test_s1_extras_carry_the_facts(self):
        ctx = parse_s1_context("5,3,3,3,3;parallel=2;resume=yes;nodes=3;duration=multiday;coordination=no")
        self.assertEqual(ctx.orchestration.nodes, 3)
        self.assertEqual(ctx.orchestration.duration, "multiday")
        self.assertIs(ctx.orchestration.coordination, False)
        self.assertEqual(ctx.parallel_nodes, 2)
        self.assertIsNone(parse_s1_context("5,3,3,3,3;duration=forever"))


class EntryRouteTierTests(unittest.TestCase):
    def test_new_request_carries_tier_and_keeps_legacy_route(self):
        result = entry_route("帮我做支付系统", "5,3,3,3,3;nodes=3;parallel=3;resume=yes")
        self.assertEqual(result["execution_tier"], "lean")
        self.assertEqual(result["route"], "complex")  # legacy meaning untouched
        self.assertTrue(result["tier_reason"])

    def test_legacy_light_plan_never_gets_a_lean_alias(self):
        result = entry_route("帮我重构登录模块", "3,2,2,2,2")
        self.assertEqual(result["route"], "light_plan")
        self.assertIn(result["execution_tier"], {"direct", "lean", "run"})

    def test_bound_events_take_the_fast_path_without_scoring(self):
        for kind, text in (("continue", "继续"), ("rework", "重构整个支付系统并部署"),
                           ("heartbeat", ""), ("handoff", "重构整个支付系统并部署")):
            with self.subTest(kind):
                result = entry_route(text, current_tier="lean", binding=kind)
                self.assertEqual(result["route"], "continue_current")
                self.assertEqual(result["fast_path"], kind)
                self.assertIsNone(result["score_line"])
                self.assertNotIn("execution_tier", result)

    def test_bound_new_goal_is_rerouted(self):
        result = entry_route("现在改做完全不同的新目标：设计并实现支付系统、迁移数据并部署", binding="continue")
        self.assertNotEqual(result["route"], "continue_current")
        self.assertIn("execution_tier", result)

    def test_bare_continue_without_binding_does_not_guess_a_task(self):
        result = entry_route("继续")
        self.assertEqual(result["route"], "continue_current")
        self.assertEqual(result["binding"], "none")
        self.assertIsNone(result["tier"])

    def test_unknown_binding_kind_is_rejected(self):
        with self.assertRaises(ValueError):
            entry_route("继续", binding="whatever")


class RoutingReplayTests(unittest.TestCase):
    def setUp(self):
        self.historical = _load("historical.json")
        self.synthetic = _load("synthetic.json")

    def test_fixtures_hold_only_entry_visible_facts(self):
        allowed = {"score", "nodes", "parallel", "duration", "coordination"}
        for fixture in (self.historical, self.synthetic):
            for sample in fixture["samples"]:
                self.assertLessEqual(set(sample["facts"]), allowed, sample["id"])

    def test_history_and_synthetic_are_reported_separately(self):
        report = replay_routing(self.historical["samples"], self.synthetic["samples"])
        self.assertEqual(set(report), {"historical", "synthetic", "vetoes"})
        self.assertEqual(report["historical"]["samples"], len(self.historical["samples"]))
        self.assertEqual(report["synthetic"]["samples"], len(self.synthetic["samples"]))
        self.assertEqual(report["historical"]["root_tasks"], 5)  # H7a/H7b share one root

    def test_every_sample_lands_in_an_allowed_tier(self):
        report = replay_routing(self.historical["samples"], self.synthetic["samples"])
        for part in ("historical", "synthetic"):
            self.assertEqual(report[part]["outside_allowed"], [], part)

    def test_no_risk_or_authorization_omission(self):
        report = replay_routing(self.historical["samples"], self.synthetic["samples"])
        self.assertEqual(report["vetoes"], [])

    def test_replay_never_passes_outcome_to_the_router(self):
        seen = []
        import vibe_guide.session_entry as module
        original = module.decide_execution_tier
        module.decide_execution_tier = lambda score, facts: (seen.append((score, facts)), original(score, facts))[1]
        try:
            replay_routing(self.historical["samples"], [])
        finally:
            module.decide_execution_tier = original
        self.assertTrue(seen)
        for _, facts in seen:
            self.assertIsInstance(facts, OrchestrationFacts)

    def test_unknown_stays_unknown_and_is_counted(self):
        report = replay_routing(self.historical["samples"], self.synthetic["samples"])
        self.assertGreaterEqual(report["historical"]["with_unknown_facts"], 1)

    def test_disputed_samples_are_listed_not_forced(self):
        report = replay_routing(self.historical["samples"], self.synthetic["samples"])
        self.assertIn("H4", report["historical"]["disputed"])

    def test_missing_safeguard_is_a_separate_veto(self):
        sample = {"id": "X", "root_task": "x", "facts": {"score": 12, "nodes": 1},
                  "allowed_tiers": ["direct"], "risk_requirements": ["未知护栏"]}
        report = replay_routing([], [sample])
        self.assertEqual([item["id"] for item in report["vetoes"]], ["X"])

    def test_report_compares_legacy_route_without_treating_it_as_truth(self):
        report = replay_routing(self.historical["samples"], self.synthetic["samples"])
        self.assertIn("legacy_disagreements", report["synthetic"])


if __name__ == "__main__":
    unittest.main()
