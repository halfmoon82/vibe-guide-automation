from dataclasses import replace
from copy import deepcopy
import unittest

from vibe_guide.authorization import (
    authorize,
    build_authorization_card,
    is_authorization_valid,
    refresh_authorization_card,
    validate_runtime_contract,
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

    def test_refresh_preserves_explicit_local_merge_authorization(self):
        plan = Plan("plan-local", 1, "docs/prd.md", ["n1"], "draft")
        node_value = node("n1", ["safe.py"])
        card = build_authorization_card(
            plan,
            [node_value],
            self.capabilities,
            allowed_actions=("develop", "test", "review", "merge_local"),
        )
        refreshed = refresh_authorization_card(plan, [node_value], card)

        self.assertIn("merge_local", refreshed.allowed_actions)
        self.assertEqual(
            authorize(refreshed, "AUTHORIZE").allowed_actions,
            ("develop", "test", "review", "merge_local"),
        )

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

    def test_full_executable_contract_is_bound_and_excluded_actions_are_rejected(self):
        base = node("n1", ["safe.py"])
        base.contract.update(
            {
                "branch": "codex/safe",
                "provider": "codex",
                "mode": "visible",
                "hostId": "local",
                "developer_task_id": "thread-safe",
            }
        )
        plan = Plan("plan-contract", 1, "docs/prd.md", ["n1"], "draft")
        baseline = build_authorization_card(plan, [base], self.capabilities)

        mutations = {
            "files": ["outside.py"],
            "worker": "worker-evil",
            "worktree": "../outside",
            "branch": "codex/evil",
            "provider": "other-provider",
            "developer_task_id": "thread-other",
        }
        for key, value in mutations.items():
            with self.subTest(field=key):
                changed = deepcopy(base)
                changed.contract[key] = value
                card = build_authorization_card(plan, [changed], self.capabilities)
                self.assertNotEqual(card.digest, baseline.digest)

        excluded = deepcopy(base)
        excluded.contract["action"] = "deploy"
        with self.assertRaises(ValueError):
            build_authorization_card(plan, [excluded], self.capabilities)

    def test_authorization_rejects_raw_secret_fields(self):
        secret_node = node("n1", ["safe.py"])
        secret_node.contract["token"] = "raw-secret-sentinel"
        plan = Plan("plan-secret", 1, "docs/prd.md", ["n1"], "draft")

        with self.assertRaises(ValueError):
            build_authorization_card(plan, [secret_node], self.capabilities)

    def test_action_values_are_canonicalized_and_all_deploy_shapes_are_rejected(self):
        plan = Plan("plan-actions", 1, "docs/prd.md", ["n1"], "draft")
        canonical = node("n1", ["safe.py"])
        canonical.contract["action"] = "develop"
        decorated = deepcopy(canonical)
        decorated.contract["action"] = "  DeVeLoP  "

        self.assertEqual(
            build_authorization_card(plan, [canonical], self.capabilities).digest,
            build_authorization_card(plan, [decorated], self.capabilities).digest,
        )

        variants = (
            {"action": "DEPLOY"},
            {"actions": ["test", " Deploy "]},
            {"provider": {"requested_actions": ["commit", "dEpLoY"]}},
            {"provider": {"nested": {"allowed-actions": " DEPLOY "}}},
        )
        for contract_update in variants:
            with self.subTest(contract=contract_update):
                candidate = node("n1", ["safe.py"])
                candidate.contract.update(contract_update)
                with self.assertRaises(ValueError):
                    build_authorization_card(plan, [candidate], self.capabilities)

    def test_runtime_actions_are_closed_and_files_are_normalized_lists(self):
        for action in (
            "production-deploy",
            "install-skill",
            "grant-system-permission",
            "external-write",
        ):
            with self.subTest(action=action), self.assertRaises(ValueError):
                validate_runtime_contract(
                    {"action": action, "files": ["src/app.py"]}
                )

        for files in (
            "src/app.py",
            ["../outside.py"],
            ["/absolute.py"],
            ["src/app.py", "src/app.py"],
        ):
            with self.subTest(files=files), self.assertRaises(ValueError):
                validate_runtime_contract({"action": "test", "files": files})

        for actions in ([], [["test"]], ["test", 1]):
            with self.subTest(actions=actions), self.assertRaises(ValueError):
                validate_runtime_contract(
                    {"actions": actions, "files": ["src/app.py"]}
                )

        normalized = validate_runtime_contract(
            {"actions": ["TEST", "develop"], "files": ["src/./app.py"]},
            authorized_actions=("develop", "test"),
            authorized_files=("src/app.py",),
        )
        self.assertEqual(normalized["actions"], ["test", "develop"])
        self.assertEqual(normalized["files"], ["src/app.py"])

    def test_authorization_binds_active_pair_limit_and_normalized_file_scope(self):
        normalized_node = node("n1", ["src/./app.py"])
        normalized_node.contract["provider_scope"] = {
            "files": ["tests/./test_app.py"]
        }
        plan = Plan("plan-capacity", 1, "docs/prd.md", ["n1"], "draft")

        card = build_authorization_card(
            plan, [normalized_node], self.capabilities, active_pair_limit=1
        )

        self.assertEqual(
            card.file_scope, ("src/app.py", "tests/test_app.py")
        )
        self.assertEqual(card.active_pair_limit, 1)
        self.assertEqual(authorize(card, "AUTHORIZE").active_pair_limit, 1)


if __name__ == "__main__":
    unittest.main()
