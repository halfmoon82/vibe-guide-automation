"""The integration review loop obeys the same three-round limit as a business node.

Recorded failure (rehearsal run-01202945, 2026-10-29): the integration reviewer
reported the same finding for 11 generations, because the integration branch of
``review_finding`` returned before ``review_loop_rounds`` was counted.  The run
never parked and never reached the PM.
"""
import unittest

from vibe_guide.authorization import authorize
from vibe_guide.models import INTEGRATION_REVIEW_NODE_ID
from vibe_guide.state import load_events
from tests import test_auto_scope_files as fx

FINDING = ("review_finding", {"finding": "docs/usage.md is still a placeholder", "severity": "P1"})


class IntegrationReviewLoopLimitTests(unittest.TestCase):
    setUp = fx.MonitorAutoScopeTests.setUp
    tearDown = fx.MonitorAutoScopeTests.tearDown
    _build_complex_plan = fx.MonitorAutoScopeTests._build_complex_plan

    def run_loop(self, ticks=14):
        _plan, _nodes, card, monitor, delivered = self._build_complex_plan([fx._node("n1")])
        runner = fx._BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [FINDING] * 20,
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(ticks):
            snapshot = monitor.tick(snapshot.run_id, runner)
        return runner, snapshot

    def test_integration_findings_park_the_node_after_three_rounds(self):
        runner, snapshot = self.run_loop()

        current = snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]
        self.assertEqual(current["status"], "blocked_unknown", current.get("reason"))
        self.assertEqual(current["recovery_action"], "change_reviewer_or_split_node")
        self.assertTrue(current["reviewer_replacement_required"])
        reviews = [call for call in runner.start_calls
                   if call["node_id"] == INTEGRATION_REVIEW_NODE_ID and call["role"] == "reviewer"]
        self.assertEqual(len(reviews), 3, "no review round may start after the limit")
        [parked] = [record for record in load_events(self.paths, snapshot.run_id)
                    if record["event"] == "review_loop_limit_reached"]
        self.assertEqual(parked["data"]["node_id"], INTEGRATION_REVIEW_NODE_ID)

    def test_integration_findings_are_still_recorded_for_closeout(self):
        _runner, snapshot = self.run_loop()
        findings = snapshot.nodes[INTEGRATION_REVIEW_NODE_ID].get("integration_findings", [])
        self.assertEqual(len(findings), 3)


if __name__ == "__main__":
    unittest.main()
