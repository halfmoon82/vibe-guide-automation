import unittest

from vibe_guide.evidence import ReworkDecision, ReviewResult, classify_rework


class V38ReworkEscalationTests(unittest.TestCase):
    def test_second_same_class_failure_escalates(self):
        decision = classify_rework([
            ReviewResult("P1", "same", "blocked"),
            ReviewResult("P1", "same", "blocked"),
        ])
        self.assertEqual(decision, ReworkDecision.CONTRACT_OR_CALL_CHAIN_REVIEW_REQUIRED)


if __name__ == "__main__":
    unittest.main()
