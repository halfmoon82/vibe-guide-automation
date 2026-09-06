import unittest

from vibe_guide.model_router import (
    IssueComplexity,
    LocalModel,
    ModelRouter,
    WorkerUnavailable,
)


class ModelRouterTests(unittest.TestCase):
    def setUp(self):
        self.models = [
            LocalModel("fast", ["shell"], 16_000, ["normal", "deep"], True),
            LocalModel("deep", ["shell", "security"], 64_000, ["normal", "deep"], True),
        ]

    def test_issue_complexity_drives_reasoning_without_project_s1(self):
        issue = IssueComplexity(
            "I-small", "spec:I-small", 2, 1, 1, 1, 1, "small", [], "simple", "evidence:I-small"
        )
        profile = ModelRouter().select(issue, ["shell"], self.models)
        self.assertEqual(profile.model, "fast")
        self.assertEqual(profile.reasoning, "normal")
        self.assertEqual(profile.selection_basis["issue_complexity_ref"], "I-small")
        self.assertNotIn("s1_score", profile.selection_basis)

    def test_security_and_migration_escalate_reasoning(self):
        issue = IssueComplexity(
            "I-risk", "spec:I-risk", 4, 2, 3, 4, 2, "large", ["migration", "security"], "complex", "evidence:I-risk"
        )
        profile = ModelRouter().select(issue, ["shell", "security"], self.models)
        self.assertEqual(profile.model, "deep")
        self.assertEqual(profile.reasoning, "deep")
        self.assertEqual(profile.selection_basis["risk_tags"], ["migration", "security"])

    def test_unavailable_primary_uses_recorded_fallback(self):
        unavailable = LocalModel("deep", ["shell", "security"], 64_000, ["deep"], False)
        issue = IssueComplexity(
            "I-risk", "spec:I-risk", 4, 2, 3, 4, 2, "large", ["security"], "complex", "evidence:I-risk"
        )
        profile = ModelRouter().select(issue, ["shell"], [unavailable, self.models[0]])
        self.assertEqual(profile.model, "fast")
        self.assertEqual(profile.fallbacks[0]["model"], "deep")

    def test_unknown_probe_is_blocked(self):
        issue = IssueComplexity(
            "I-risk", "spec:I-risk", 4, 2, 3, 4, 2, "large", ["security"], "complex", "evidence:I-risk"
        )
        with self.assertRaises(WorkerUnavailable) as raised:
            ModelRouter().select(issue, ["security"], [LocalModel("deep", ["security"], 64_000, ["deep"], None)])
        self.assertEqual(raised.exception.status, "blocked_unknown")

    def test_project_s1_cannot_replace_issue_complexity(self):
        with self.assertRaises(TypeError):
            ModelRouter().select({"score": 16}, ["shell"], self.models)


if __name__ == "__main__":
    unittest.main()
