import unittest

from vibe_guide.authorization import canonical_git_action, validate_git_action_target


class V38GitTargetTests(unittest.TestCase):
    def test_explicit_target_is_required_and_main_deploy_are_false(self):
        action = canonical_git_action({
            "commit": True, "push": True, "create_change_request": True,
            "merge_to_target_branch": True, "merge_target_branch": "codex/v38-7",
            "merge_to_main": False, "deploy": False,
        })
        validate_git_action_target(action)
        self.assertEqual(action["merge_target_branch"], "codex/v38-7")
        for bad in (
            {"merge": True},
            {"merge_to_target_branch": True, "merge_target_branch": "main"},
            {"merge_to_target_branch": True, "merge_target_branch": "codex/v38", "merge_to_main": True},
            {"merge_to_target_branch": True, "merge_target_branch": "codex/v38", "deploy": True},
        ):
            with self.assertRaises(ValueError):
                validate_git_action_target(bad)


if __name__ == "__main__":
    unittest.main()
