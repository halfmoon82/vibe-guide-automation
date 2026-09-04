import unittest

from vibe_guide.authorization import evaluate_v4_closeout


class V4AuthorizationBoundaryTests(unittest.TestCase):
    def snapshot(self, statuses):
        return {"nodes": {"n%d" % i: {"status": status} for i, status in enumerate(statuses)}}

    def test_development_does_not_authorize_pr_or_mr(self):
        decision = evaluate_v4_closeout(self.snapshot(["accepted"]), {"allowed_actions": ["develop", "test", "review"]})
        self.assertFalse(decision.can_execute_external)
        self.assertIn("create_pr", decision.excluded_actions)

    def test_external_actions_require_separate_explicit_authorization_after_acceptance(self):
        decision = evaluate_v4_closeout(self.snapshot(["accepted"]), {"allowed_actions": ["create_pr"]})
        self.assertTrue(decision.can_execute_external)
        self.assertEqual(decision.allowed_actions, ("create_pr",))

    def test_deploy_credentials_and_system_permission_are_always_excluded(self):
        decision = evaluate_v4_closeout(self.snapshot(["accepted"]), {"allowed_actions": ["deploy", "credential", "system_permission_change"]})
        self.assertFalse(decision.can_execute_external)
        for action in ("deploy", "credential", "system_permission_change"):
            self.assertIn(action, decision.excluded_actions)

    def test_unaccepted_node_blocks_external_closeout(self):
        decision = evaluate_v4_closeout(self.snapshot(["accepted", "review"]), {"allowed_actions": ["create_mr"]})
        self.assertFalse(decision.can_execute_external)


if __name__ == "__main__":
    unittest.main()
