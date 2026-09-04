import tempfile
import unittest
from pathlib import Path

from vibe_guide.models import DAGNode, Plan, V4ExecutionPolicy
from vibe_guide.planner import build_v4_execution_policy
from vibe_guide.sdd_runtime import prepare_issue_workspace, validate_sdd_write_gate


class V4SddWriteGateTests(unittest.TestCase):
    def policy(self):
        return V4ExecutionPolicy(
            capabilities={"installation": True},
            hard_gates={name: False for name in V4ExecutionPolicy.CANONICAL_HARD_GATES},
            node_ids=["i"], project_root=str(self.root),
            worktree=str(self.root / ".vibe" / "worktrees" / "i"),
            allowlist=["vibe_guide/sdd_runtime.py"], writer="dev-1", issue_id="i",
        )

    def node(self, **contract):
        base = {"project_root": contract.pop("project_root", self.root), "worktree": contract.pop("worktree", self.root / ".vibe" / "worktrees" / "i"), "allowlist": contract.pop("allowlist", ["vibe_guide/sdd_runtime.py"]), "run_id": "run-1", "role": "developer"}
        base.update(contract)
        return DAGNode("i", "Issue", [], [], "g", base, "ready", writer="dev-1", reviewer="rev-1")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self):
        self.tmp.cleanup()

    def test_in_root_workspace_created(self):
        path = prepare_issue_workspace(self.root, "i", self.root / ".vibe" / "worktrees")
        self.assertTrue(path.is_dir())
        self.assertEqual(path.parent, (self.root / ".vibe" / "worktrees").resolve())

    def test_out_of_root_workspace_rejected(self):
        with self.assertRaises(ValueError):
            prepare_issue_workspace(self.root, "i", self.root.parent / "outside")

    def test_issue_symlink_escape_rejected_without_external_write(self):
        managed = self.root / ".vibe" / "worktrees"
        managed.mkdir(parents=True)
        outside = self.root.parent / (self.root.name + "-outside")
        outside.mkdir()
        try:
            (managed / "i").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                prepare_issue_workspace(self.root, "i", managed)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            (managed / "i").unlink(missing_ok=True)
            outside.rmdir()

    def test_project_root_worktree_rejected(self):
        result = validate_sdd_write_gate(self.policy(), self.node(worktree=self.root), {})
        self.assertFalse(result.valid)
        self.assertIn("host_checkout_write", result.reasons)

    def test_invalid_project_root_is_structured_failure(self):
        result = validate_sdd_write_gate(self.policy(), self.node(project_root=7), {})
        self.assertFalse(result.valid)
        self.assertIn("workspace_invalid", result.reasons)

    def test_duplicate_writer_rejected(self):
        result = validate_sdd_write_gate(self.policy(), self.node(), {"i": "another"})
        self.assertFalse(result.valid)
        self.assertIn("duplicate_writer", result.reasons)

    def test_allowlist_escape_rejected(self):
        result = validate_sdd_write_gate(self.policy(), self.node(allowlist=["../secret"]), {})
        self.assertFalse(result.valid)
        self.assertIn("allowlist_escape", result.reasons)

    def test_reviewer_write_rejected(self):
        result = validate_sdd_write_gate(self.policy(), self.node(role="reviewer"), {})
        self.assertFalse(result.valid)
        self.assertIn("reviewer_write", result.reasons)

    def test_reviewer_rejection_does_not_claim_writer(self):
        validate_sdd_write_gate(self.policy(), self.node(role="reviewer"), {})
        result = validate_sdd_write_gate(self.policy(), self.node(), {})
        self.assertTrue(result.valid)

    def test_missing_provider_lease_and_cursor_are_advisory(self):
        result = validate_sdd_write_gate(self.policy(), self.node(), {})
        self.assertTrue(result.valid)
        self.assertIn("missing_provider_lease", result.observations)
        self.assertIn("missing_provider_cursor", result.observations)

    def test_policy_contract_mismatch_is_rejected_with_structured_reasons(self):
        policy = V4ExecutionPolicy(
            capabilities={"installation": True},
            hard_gates={name: False for name in V4ExecutionPolicy.CANONICAL_HARD_GATES},
            node_ids=["i"],
            project_root=str(self.root),
            worktree=str(self.root / ".vibe" / "worktrees" / "i"),
            allowlist=["vibe_guide/sdd_runtime.py"],
            writer="dev-1",
            issue_id="i",
            plan_revision=2,
        )
        node = self.node(project_root=str(self.root / "other"), writer="dev-1")
        result = validate_sdd_write_gate(policy, node, {})
        self.assertFalse(result.valid)
        self.assertIn("policy_contract_mismatch:project_root", result.reasons)

    def test_frozen_policy_field_missing_from_node_contract_is_rejected(self):
        policy = V4ExecutionPolicy(
            capabilities={"installation": True},
            hard_gates={name: False for name in V4ExecutionPolicy.CANONICAL_HARD_GATES},
            node_ids=["i"], project_root=str(self.root),
            worktree=str(self.root / ".vibe" / "worktrees" / "i"),
            allowlist=["vibe_guide/sdd_runtime.py"], writer="dev-1", issue_id="i",
        )
        node = self.node()
        node.contract.pop("project_root", None)
        result = validate_sdd_write_gate(policy, node, {})
        self.assertFalse(result.valid)
        self.assertIn("policy_contract_mismatch:project_root", result.reasons)

    def test_empty_policy_ownership_cannot_be_completed_by_node_contract(self):
        # Regression: a V4 policy without frozen ownership must not let the
        # node self-authorize its project/worktree/allowlist/writer scope.
        policy = V4ExecutionPolicy(
            capabilities={"installation": True},
            hard_gates={name: False for name in V4ExecutionPolicy.CANONICAL_HARD_GATES},
            node_ids=["i"],
        )
        result = validate_sdd_write_gate(policy, self.node(), {})
        self.assertFalse(result.valid)
        self.assertIn("policy_ownership_missing", result.reasons)

    def test_writer_is_unique_across_issues_but_same_issue_can_resume(self):
        run_id = "cross-issue-writer"
        first = self.node(run_id=run_id, issue_id="i")
        policy_i = V4ExecutionPolicy(
            capabilities={"installation": True},
            hard_gates={name: False for name in V4ExecutionPolicy.CANONICAL_HARD_GATES},
            node_ids=["i"], project_root=str(self.root),
            worktree=str(self.root / ".vibe" / "worktrees" / "i"),
            allowlist=["vibe_guide/sdd_runtime.py"], writer="dev-1", issue_id="i",
        )
        second = DAGNode(
            "j", "Other", [], [], "g", {
                "project_root": str(self.root),
                "worktree": str(self.root / ".vibe" / "worktrees" / "j"),
                "allowlist": ["vibe_guide/sdd_runtime.py"],
                "run_id": run_id, "role": "developer", "writer": "dev-1", "issue_id": "j",
            }, "ready", writer="dev-1", reviewer="rev-2")
        policy_j = V4ExecutionPolicy(
            capabilities={"installation": True},
            hard_gates={name: False for name in V4ExecutionPolicy.CANONICAL_HARD_GATES},
            node_ids=["j"], project_root=str(self.root),
            worktree=str(self.root / ".vibe" / "worktrees" / "j"),
            allowlist=["vibe_guide/sdd_runtime.py"], writer="dev-1", issue_id="j",
        )
        self.assertTrue(validate_sdd_write_gate(policy_i, first, {}).valid)
        result = validate_sdd_write_gate(policy_j, second, {})
        self.assertFalse(result.valid)
        self.assertIn("duplicate_writer", result.reasons)
        self.assertTrue(validate_sdd_write_gate(policy_i, first, {}).valid)

    def test_legacy_active_writer_map_cannot_override_reverse_claim(self):
        from vibe_guide.task_registry import claim_v4_writer, release_v4_writer
        run_id = "legacy-map-reconcile"
        self.assertTrue(claim_v4_writer(run_id, "other", "dev-1"))
        try:
            node = self.node(run_id=run_id)
            result = validate_sdd_write_gate(self.policy(), node, {"dev-1": "other"})
            self.assertFalse(result.valid)
            self.assertIn("duplicate_writer", result.reasons)
        finally:
            release_v4_writer(run_id, "other", "dev-1")


if __name__ == "__main__":
    unittest.main()
