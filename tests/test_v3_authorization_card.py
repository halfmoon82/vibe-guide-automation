import unittest

from vibe_guide.authorization import (
    AuthorizationCard,
    authorize,
    build_authorization_card,
    is_action_authorized,
    require_action_authorized,
    is_authorization_integrity_valid,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


def _node(node_id="n1"):
    return DAGNode(
        node_id,
        "Node " + node_id,
        [],
        [],
        "authorization",
        {"files": ["vibe_guide/authorization.py"], "worker": "developer"},
        "ready",
    )


class V3AuthorizationCardTests(unittest.TestCase):
    def setUp(self):
        self.plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "authorized")
        self.nodes = [_node()]
        self.capabilities = AgentCapabilities(
            "codex", True, True, True, True, True, "full"
        )

    def test_card_explicitly_allows_git_actions_and_excludes_deploy(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)
        record = authorize(card, "AUTHORIZE")
        self.assertEqual(
            set(("commit", "push", "create_mr", "merge")),
            set(card.allowed_actions) & {"commit", "push", "create_mr", "merge"},
        )
        self.assertIn("deploy", card.excluded_actions)
        self.assertNotIn("deploy", card.allowed_actions)
        self.assertIn("commit", card.render())
        self.assertIn("deploy", card.render())
        self.assertTrue(is_action_authorized(record, "merge"))
        self.assertTrue(is_authorization_integrity_valid(record))

    def test_default_card_can_be_refreshed_without_changing_action_meaning(self):
        from vibe_guide.authorization import refresh_authorization_card

        card = build_authorization_card(self.plan, self.nodes, self.capabilities)
        refreshed = refresh_authorization_card(self.plan, self.nodes, card)
        record = authorize(refreshed, "AUTHORIZE")
        self.assertEqual(refreshed.allowed_actions, card.allowed_actions)
        self.assertTrue(is_action_authorized(record, "merge"))

    def test_pending_card_cannot_authorize_merge_execution(self):
        from vibe_guide.change_requests import ChangeRequest, merge_remote

        card = build_authorization_card(
            self.plan, self.nodes, self.capabilities,
            merge_scope={"issue_id": "V3-2", "source_sha": "a" * 40, "target_branch": "main", "change_request_id": "MR-1"},
        )
        self.assertEqual(card.confirmation_status, "pending_user_authorization")
        self.assertFalse(is_action_authorized(card, "merge"))
        request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, "verified_remote", "", "V3-2", "MR-1")
        with self.assertRaises(PermissionError):
            merge_remote(request, card, {
                "issue_id": "V3-2", "source_sha": "a" * 40, "target_branch": "main", "change_request_id": "MR-1",
                "remote_merge_verified": True, "remote_mutated": True,
                "merge_base": "c" * 40, "merge_commit": "d" * 40, "merge_tree": "e" * 40,
                "tests": ["python -m unittest"],
            })

    def test_published_v3_card_digest_is_confirmable(self):
        import json
        from pathlib import Path
        published = Path("/Users/smy/Desktop/CFO/黑客松/开发辅助/.vibe/plans/vibe-guide-v3-spec-issue-dag/authorization-card.json")
        card = AuthorizationCard.from_dict(json.loads(published.read_text(encoding="utf-8")))
        self.assertEqual(authorize(card, "AUTHORIZE").confirmation_status, "confirmed")

    def test_action_scope_change_invalidates_digest_and_needs_fresh_confirmation(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)
        record = authorize(card, "AUTHORIZE")
        changed = build_authorization_card(
            self.plan,
            self.nodes,
            self.capabilities,
            allowed_actions=tuple(
                action for action in card.allowed_actions if action != "accept"
            ),
            merge_scope={
                "issue_id": "V3-2",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request_id": "MR-1",
            },
        )
        self.assertNotEqual(card.digest, changed.digest)
        with self.assertRaises(ValueError):
            authorize(dict(changed.to_dict(), digest=card.digest), "AUTHORIZE")
        self.assertTrue(is_action_authorized(record, "push"))

    def test_unlisted_and_deploy_actions_are_denied_without_inference(self):
        card = build_authorization_card(
            self.plan,
            self.nodes,
            self.capabilities,
            allowed_actions=("commit", "push", "create_mr", "merge"),
            merge_scope={
                "issue_id": "V3-2",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request_id": "MR-1",
            },
        )
        record = authorize(card, "AUTHORIZE")
        self.assertTrue(is_action_authorized(record, "commit"))
        self.assertFalse(is_action_authorized(record, "deploy"))
        self.assertFalse(is_action_authorized(record, "unknown"))
        with self.assertRaises(PermissionError):
            require_action_authorized(record, "deploy")

    def test_v2_explicit_card_retains_closed_excluded_action_set(self):
        card = build_authorization_card(
            plan_id="plan-v2",
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
        self.assertEqual(tuple(card["excluded_actions"]), ("create_mr", "deploy", "merge", "push"))


if __name__ == "__main__":
    unittest.main()
