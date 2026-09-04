import unittest

from vibe_guide.monitor import heal_v4_node
from vibe_guide.state import RunSnapshot


class V4RecoveryCompatibilityTests(unittest.TestCase):
    def test_unknown_provider_evidence_keeps_legacy_snapshot_resumable(self):
        snapshot = {
            "run_id": "run-v4",
            "nodes": {
                "n1": {"status": "running", "task_id": "task-1", "cursor": "c1", "generation": 2},
                "n2": {"status": "planned", "task_id": "task-2"},
            },
            "healing_events": [],
        }
        result = heal_v4_node(snapshot, "n1", {"kind": "provider_timeout"})
        self.assertFalse(result.repaired)
        self.assertTrue(result.isolated)
        self.assertEqual(snapshot["nodes"]["n1"]["status"], "blocked_unknown")
        self.assertEqual(snapshot["nodes"]["n2"]["status"], "planned")
        self.assertEqual(snapshot["nodes"]["n1"]["cursor"], "c1")
        self.assertEqual(snapshot["nodes"]["n1"]["generation"], 2)

    def test_legacy_run_snapshot_schema_does_not_require_v4_binding_fields(self):
        payload = {
            "schema_version": 1,
            "run_id": "legacy",
            "plan_id": "plan",
            "plan_version": 1,
            "status": "running",
            "nodes": {"n1": {"status": "planned"}},
            "handles": {},
            "tasks": {},
            "authorization": {},
            "authorization_digest": "a" * 64,
            "node_contract_digest": "b" * 64,
            "event_sequence": 1,
        }
        # Parsing may reject the deliberately incomplete authorization record;
        # the compatibility assertion is that no V4 binding key is required by
        # the input schema before legacy validation runs.
        with self.assertRaises(Exception) as caught:
            RunSnapshot.from_dict(payload)
        self.assertNotIn("binding", str(caught.exception).lower())

    def test_healing_evidence_round_trips_in_snapshot_json(self):
        auth = {
            "schema_version": 2, "plan_id": "plan", "plan_version": 1,
            "node_ids": ["n1"], "file_scope": ["a.py"], "worker_scope": ["w"],
            "allowed_actions": ["develop"], "excluded_actions": ["deploy"],
            "node_contract_digest": "b" * 64, "decision_digest": "c" * 64,
            "active_pair_limit": 5, "digest": "a" * 64, "agent_id": "agent",
        }
        payload = {
            "schema_version": 1, "run_id": "run-v4", "plan_id": "plan", "plan_version": 1,
            "status": "running", "nodes": {"n1": {"status": "running"}}, "handles": {}, "tasks": {},
            "authorization": auth, "authorization_digest": "a" * 64,
            "node_contract_digest": "b" * 64, "event_sequence": 1,
            "healing_events": [{"node_id": "n1", "status": "degraded", "sequence": 1}],
        }
        restored = RunSnapshot.from_dict(payload)
        self.assertEqual(restored.to_dict()["healing_events"], payload["healing_events"])

    def test_binding_verification_allows_dynamic_managed_root_and_equivalent_evidence(self):
        from vibe_guide.binding_lifecycle import ProviderRuntimeBinding, RequestedBindingPolicy, verify_binding
        policy = RequestedBindingPolicy(
            project_root="/project", issue_id="n1", developer_task="dev", reviewer_task="rev",
            provider="codex", mode="visible", worktree="/project/.vibe/worktrees/n1",
            branch="codex/n1", base_sha="b" * 40, allowlist=("a.py",),
        )
        observed = ProviderRuntimeBinding(
            task_id="task-1", host="local", mode="visible", project_root="/project",
            checkout_worktree="/runtime/managed/n1", branch="codex/n1", base_sha=None,
            head_sha=None, allowlist=("a.py",), developer_task_id="dev", reviewer_task_id="rev",
            lease=None, cursor=None, ownership="verified", continuation="cursor-proof",
            baseline="b" * 40, managed_root="/runtime/managed",
        )
        result = verify_binding(policy, observed, prior_task_id="task-1")
        self.assertEqual(result.status, "binding_verified")

    def test_binding_provider_drift_is_reported(self):
        from vibe_guide.binding_lifecycle import ProviderRuntimeBinding, RequestedBindingPolicy, verify_binding
        policy = RequestedBindingPolicy(
            project_root="/project", issue_id="n1", developer_task="dev", reviewer_task="rev",
            provider="codex", mode="visible", worktree="/project/.vibe/worktrees/n1",
            branch="codex/n1", base_sha="b" * 40, allowlist=("a.py",),
        )
        observed = ProviderRuntimeBinding(
            task_id="task-1", host="local", provider="claude", mode="visible", project_root="/project",
            checkout_worktree="/project/.vibe/worktrees/n1", branch="codex/n1", base_sha="b" * 40,
            head_sha="c" * 40, allowlist=("a.py",), developer_task_id="dev", reviewer_task_id="rev",
        )
        result = verify_binding(policy, observed, prior_task_id="task-1")
        self.assertEqual(result.status, "binding_drift")
        self.assertIn("provider", result.conflicts)


if __name__ == "__main__":
    unittest.main()
