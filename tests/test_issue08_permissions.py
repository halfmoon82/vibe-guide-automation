import unittest
from vibe_guide.authorization import validate_remote_git_permissions, validate_authorization_card_consistency


class Issue08PermissionTests(unittest.TestCase):
    def test_allow_requires_complete_remote_git_scope(self):
        with self.assertRaises(ValueError):
            validate_remote_git_permissions("allow", ("develop", "commit"))

    def test_deny_rejects_remote_git_permissions(self):
        for action in ("commit", "push", "create_pr", "create_mr", "merge", "pr", "mr"):
            with self.assertRaises(ValueError):
                validate_remote_git_permissions("deny", ("develop", action))

    def test_sensitive_actions_are_always_rejected(self):
        with self.assertRaises(ValueError):
            validate_remote_git_permissions("allow", ("commit", "push", "create_pr", "create_mr", "merge", "deploy"))

    def test_consistency_rejects_release_and_external_communication(self):
        for action in ("release", "external_communication", "deploy", "production_write", "credentials"):
            with self.assertRaises(ValueError):
                validate_authorization_card_consistency({"remote_git_actions": "deny", "allowed_actions": [action]})


if __name__ == "__main__":
    unittest.main()
