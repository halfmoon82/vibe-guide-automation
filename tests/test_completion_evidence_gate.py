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

    def test_self_report_receipt_waives_only_the_cursor(self):
        artifacts = {"thread_status": "complete", "completion_marker": "DONE", "delivery_path": "/out"}
        bound = {"task_id": "t1", "host": "h1", "worktree": "/wt", "branch": "b"}
        self.assertEqual(
            evaluate_delivery_evidence({"status": "DELIVERED"}, bound, artifacts).reasons,
            ["current cursor is missing"],
        )
        self.assertEqual(
            evaluate_delivery_evidence(
                {"status": "DELIVERED"}, bound, artifacts, self_report_receipt=True
            ).status,
            "DELIVERED",
        )
        for field, reason in (
            ("task_id", "task identity is missing"),
            ("host", "host identity is missing"),
            ("worktree", "worktree or branch is missing"),
            ("branch", "worktree or branch is missing"),
        ):
            with self.subTest(field=field):
                result = evaluate_delivery_evidence(
                    {"status": "DELIVERED"}, dict(bound, **{field: None}), artifacts,
                    self_report_receipt=True,
                )
                self.assertEqual(result.reasons, [reason])
                self.assertEqual(result.classification, "blocked_unknown")


if __name__ == "__main__":
    unittest.main()
