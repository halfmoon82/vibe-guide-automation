import unittest

from vibe_guide.evidence import evaluate_delivery_evidence


class CompletionEvidenceGateTests(unittest.TestCase):
    def test_missing_marker_cannot_unlock_dependency(self):
        result = evaluate_delivery_evidence(
            {"status": "delivered"},
            {"task_id": "t1", "host": "h1", "worktree": "/wt", "branch": "b", "cursor": "c"},
            {"thread_status": "complete", "delivery_path": "/out"},
        )
        self.assertEqual(result.status, "blocked_unknown")

    def test_complete_evidence_is_delivered(self):
        result = evaluate_delivery_evidence(
            {"status": "DELIVERED"},
            {"task_id": "t1", "host": "h1", "worktree": "/wt", "branch": "b", "cursor": "c"},
            {"thread_status": "complete", "completion_marker": "DONE", "delivery_path": "/out"},
        )
        self.assertEqual(result.status, "DELIVERED")


if __name__ == "__main__":
    unittest.main()
