import unittest

from vibe_guide.binding_lifecycle import (
    ProviderRuntimeBinding,
    RequestedBindingPolicy,
    verify_binding,
)
from vibe_guide.task_registry import v4_runtime_binding_gate


BASE = "b" * 40
HEAD = "c" * 40


def policy(**overrides):
    values = dict(
        project_root="/project",
        issue_id="V4-INTERFACE-CONTRACT",
        developer_task="developer-task",
        reviewer_task="reviewer-task",
        provider="codex-app-visible",
        mode="visible",
        worktree="/project/.vibe/worktrees/v4-interface-contract",
        branch="codex/v4-interface-contract",
        base_sha=BASE,
        allowlist=("vibe_guide/contracts.py",),
        plan_revision=1,
        writer="developer-task",
    )
    values.update(overrides)
    return RequestedBindingPolicy(**values)


def observed(**overrides):
    values = dict(
        task_id="provider-task",
        host="local",
        mode="visible",
        project_root="/project",
        checkout_worktree="/project/.vibe/worktrees/v4-interface-contract",
        branch="codex/v4-interface-contract",
        base_sha=BASE,
        head_sha=HEAD,
        allowlist=("vibe_guide/contracts.py",),
        developer_task_id="developer-task",
        reviewer_task_id="reviewer-task",
        lease="lease-1",
        cursor="cursor-1",
        evidence_refs=("provider:wait_threads:1",),
    )
    values.update(overrides)
    return ProviderRuntimeBinding(**values)


class V4BindingLifecycleTests(unittest.TestCase):
    def test_exact_match_is_verified_and_keeps_project_separate_from_checkout(self):
        result = verify_binding(policy(), observed(), prior_task_id="provider-task")
        self.assertEqual(result.status, "binding_verified")
        self.assertTrue(result.business_write_allowed)
        self.assertNotEqual(policy().project_root, observed().checkout_worktree)

    def test_detached_branch_is_drift_not_verified(self):
        result = verify_binding(policy(), observed(branch="detached"), prior_task_id="provider-task")
        self.assertEqual(result.status, "binding_drift")
        self.assertIn("branch", result.conflicts)

    def test_ancestor_base_is_not_exact_match(self):
        result = verify_binding(policy(), observed(base_sha="a" * 40), prior_task_id="provider-task")
        self.assertEqual(result.status, "binding_drift")
        self.assertIn("base_sha", result.conflicts)

    def test_missing_lease_or_cursor_is_blocked_unknown(self):
        result = verify_binding(policy(), observed(lease=None, cursor=None), prior_task_id="provider-task")
        self.assertEqual(result.status, "blocked_unknown")
        self.assertFalse(result.business_write_allowed)
        self.assertEqual(set(result.missing), {"lease", "cursor"})

    def test_rebind_requires_same_task_identity(self):
        result = verify_binding(policy(), observed(), prior_task_id="old-task")
        self.assertEqual(result.status, "blocked_unknown")
        self.assertIn("task_id", result.conflicts)

    def test_developer_and_reviewer_tasks_must_be_distinct(self):
        with self.assertRaises(ValueError):
            policy(developer_task="same", reviewer_task="same")

    def test_existing_task_registry_gate_keeps_v4_unknown_fail_closed(self):
        result = v4_runtime_binding_gate({"requested_binding_policy": policy().__dict__})
        self.assertEqual(result.status, "dispatch_pending")
        self.assertFalse(result.business_write_allowed)


if __name__ == "__main__":
    unittest.main()
