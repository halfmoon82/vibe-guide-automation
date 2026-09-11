import unittest

from vibe_guide.monitor import classify_provider_failure


class EngineeringRecoveryClassificationTests(unittest.TestCase):
    def test_five_recovery_classes(self):
        cases = {
            "dirty checkout": "repairable",
            "detached HEAD": "repairable",
            "timeout": "retryable",
            "disconnect": "retryable",
            "task creation failed": "retryable",
            "429 capacity": "capacity_wait",
            "writer identity conflict": "binding_unknown",
            "credential required": "external_decision",
            "product scope change": "external_decision",
            "deploy required": "external_decision",
            "irreversible security action": "external_decision",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                self.assertEqual(classify_provider_failure({"reason": reason})["kind"], expected)

    def test_unknown_fails_closed_as_binding_unknown(self):
        self.assertEqual(classify_provider_failure({"reason": "unexplained"})["kind"], "binding_unknown")

    def test_structured_and_external_decision_markers(self):
        for value in (
            {"credential_required": True}, {"login_required": True},
            {"permission_required": True}, {"reason": "external decision"},
            {"reason": "authorization required"},
        ):
            with self.subTest(value=value):
                self.assertEqual(classify_provider_failure(value)["kind"], "external_decision")


if __name__ == "__main__":
    unittest.main()
