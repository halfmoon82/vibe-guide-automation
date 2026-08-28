import unittest

from vibe_guide.authorization import (
    authorize,
    build_authorization_card,
    is_action_authorized,
    require_action_authorized,
)


class AuthorizationTests(unittest.TestCase):
    def test_merge_local_is_opt_in_and_merge_push_deploy_are_excluded(self):
        card = build_authorization_card(
            plan_id="plan-1",
            plan_version=1,
            node_ids=("n1",),
            file_scope=("safe.py",),
            worker_scope=("developer",),
            allowed_actions=("develop", "test", "review", "merge_local"),
            merge_scope={
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "MR-42",
            },
        )
        record = authorize(card, "AUTHORIZE")
        self.assertIn("merge_local", record["allowed_actions"])
        self.assertEqual(record["excluded_actions"], ("create_mr", "deploy", "merge", "push"))

    def test_git_actions_are_explicit_and_merge_requires_named_scope(self):
        with self.assertRaises(ValueError):
            build_authorization_card(
                plan_id="plan-1",
                plan_version=1,
                node_ids=("n1",),
                file_scope=("safe.py",),
                worker_scope=("developer",),
                allowed_actions=("develop", "merge"),
            )

        card = build_authorization_card(
            plan_id="plan-1",
            plan_version=1,
            node_ids=("n1",),
            file_scope=("safe.py",),
            worker_scope=("developer",),
            allowed_actions=("develop", "commit", "merge", "push"),
            merge_scope={
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "MR-42",
            },
        )
        record = authorize(card, "AUTHORIZE")
        self.assertTrue(is_action_authorized(record, "commit"))
        self.assertTrue(is_action_authorized(record, "push"))
        self.assertTrue(is_action_authorized(record, "merge"))
        self.assertFalse(is_action_authorized(record, "create_mr"))
        self.assertFalse(is_action_authorized(record, "deploy"))
        with self.assertRaises(PermissionError):
            require_action_authorized(record, "create_mr")

        explicitly_named = build_authorization_card(
            plan_id="plan-1",
            plan_version=1,
            node_ids=("n1",),
            file_scope=("safe.py",),
            worker_scope=("developer",),
            allowed_actions=("create_mr",),
        )
        self.assertTrue(is_action_authorized(authorize(explicitly_named, "AUTHORIZE"), "create_mr"))

    def test_merge_scope_is_digest_bound_and_cannot_be_tampered(self):
        with self.assertRaises(ValueError):
            build_authorization_card(
                plan_id="plan-1",
                plan_version=1,
                node_ids=("n1",),
                file_scope=("safe.py",),
                worker_scope=("developer",),
                allowed_actions=("merge_local",),
            )

        card = build_authorization_card(
            plan_id="plan-1",
            plan_version=1,
            node_ids=("n1",),
            file_scope=("safe.py",),
            worker_scope=("developer",),
            allowed_actions=("merge",),
            merge_scope={
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "PR-7",
            },
        )
        tampered = dict(card)
        tampered["merge_scope"] = dict(tampered["merge_scope"])
        tampered["merge_scope"]["target_branch"] = "release"
        with self.assertRaises(ValueError):
            authorize(tampered, "AUTHORIZE")

    def test_local_merge_action_checks_runtime_scope(self):
        scope = {
            "issue_id": "V2-4",
            "source_sha": "a" * 40,
            "target_branch": "main",
            "change_request": "MR-42",
        }
        card = build_authorization_card(
            plan_id="plan-1",
            plan_version=1,
            node_ids=("n1",),
            file_scope=("safe.py",),
            worker_scope=("developer",),
            allowed_actions=("merge_local",),
            merge_scope=scope,
        )
        record = authorize(card, "AUTHORIZE")
        self.assertTrue(is_action_authorized(record, "merge_local", scope))
        mismatched = dict(scope, target_branch="release")
        self.assertFalse(is_action_authorized(record, "merge_local", mismatched))


if __name__ == "__main__":
    unittest.main()
