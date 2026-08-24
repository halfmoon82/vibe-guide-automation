from dataclasses import replace
import unittest

from vibe_guide.authorization import (
    authorize,
    build_authorization_card,
    is_authorization_valid,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


def node(node_id, files, worker="worker-1"):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "parallel",
        {"files": files, "worker": worker, "worktree": ".worktrees/" + node_id},
        "ready",
    )


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.plan = Plan("plan-1", 3, "docs/prd.md", ["n1", "n2"], "draft")
        self.nodes = [node("n1", ["b.py", "a.py"]), node("n2", ["c.py"], "worker-2")]
        self.capabilities = AgentCapabilities(
            "codex", True, True, True, True, True, "full"
        )

    def test_card_lists_actions_scope_and_explicitly_excludes_deploy(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)

        self.assertEqual(
            card.allowed_actions,
            ("accept", "commit", "develop", "review", "rework", "test"),
        )
        self.assertEqual(card.excluded_actions, ("create_mr", "deploy", "merge", "push"))
        self.assertEqual(card.node_ids, ("n1", "n2"))
        self.assertEqual(card.file_scope, ("a.py", "b.py", "c.py"))
        self.assertEqual(card.worker_scope, ("worker-1", "worker-2"))
        self.assertEqual(card.plan_version, 3)
        self.assertNotIn("token", card.to_dict())

    def test_authorization_binds_canonical_plan_and_invalidates_on_change(self):
        first = build_authorization_card(self.plan, self.nodes, self.capabilities)
        reordered = build_authorization_card(
            Plan("plan-1", 3, "docs/prd.md", ["n2", "n1"], "draft"),
            list(reversed(self.nodes)),
            self.capabilities,
        )
        self.assertEqual(first.digest, reordered.digest)

        record = authorize(first, "AUTHORIZE")
        self.assertTrue(is_authorization_valid(record, self.plan))
        self.assertFalse(
            is_authorization_valid(
                record, Plan("plan-1", 4, "docs/prd.md", ["n1", "n2"], "draft")
            )
        )
        self.assertFalse(
            is_authorization_valid(
                record, Plan("plan-1", 3, "docs/prd.md", ["n1"], "draft")
            )
        )

    def test_confirmation_must_be_explicit(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)
        with self.assertRaises(ValueError):
            authorize(card, "yes")

    def test_authorization_rejects_tampered_scope_or_actions(self):
        card = build_authorization_card(self.plan, self.nodes, self.capabilities)
        record = authorize(card, "AUTHORIZE")

        self.assertFalse(
            is_authorization_valid(
                replace(record, allowed_actions=record.allowed_actions + ("push",)),
                self.plan,
            )
        )
        self.assertFalse(
            is_authorization_valid(
                replace(record, file_scope=record.file_scope + ("outside.py",)),
                self.plan,
            )
        )


if __name__ == "__main__":
    unittest.main()
