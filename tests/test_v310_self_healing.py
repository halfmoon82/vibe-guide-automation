import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.models import HealingResult, ObservationDisposition
from vibe_guide.monitor import classify_observation, self_heal_binding, isolate_affected_action


class V310SelfHealingTests(unittest.TestCase):
    def test_observations_are_unknown_or_degraded_not_unavailable(self):
        for observation in (
            {"kind": "missing_visible_locate_evidence", "mode": "visible"},
            {"kind": "worktree_branch_drift"},
            {"kind": "stale_cursor"},
            {"kind": "provider_timeout"},
            {"kind": "permission_denied"},
        ):
            disposition = classify_observation(observation)
            self.assertIsInstance(disposition, ObservationDisposition)
            self.assertIn(disposition.status, {"unknown", "degraded", "repairable"})
            self.assertNotEqual(disposition.status, "unavailable")

    def test_binding_drift_repairs_original_identity_and_scope(self):
        snapshot = {"nodes": {"n1": {"task_id": "task-1", "cursor": "c1", "worktree": "wt", "branch": "main", "allowlist": ["a.py"], "binding": {"task_id": "task-1", "cursor": "c1", "worktree": "wt", "branch": "main", "allowlist": ["a.py"]}}}}
        result = self_heal_binding(snapshot, "n1", {"kind": "worktree_branch_drift", "worktree": "other", "branch": "other", "allowlist": ["a.py", "secret.py"]})
        self.assertIsInstance(result, HealingResult)
        self.assertTrue(result.repaired)
        self.assertEqual(snapshot["nodes"]["n1"]["task_id"], "task-1")
        self.assertEqual(snapshot["nodes"]["n1"]["worktree"], "wt")
        self.assertEqual(snapshot["nodes"]["n1"]["branch"], "main")
        self.assertEqual(snapshot["nodes"]["n1"]["allowlist"], ["a.py"])

    def test_binding_repair_preserves_frozen_continuation_digest(self):
        digest = "a" * 64
        snapshot = {"nodes": {"n1": {"binding": {"task_id": "task-1", "worktree": "wt", "branch": "main", "allowlist": ["a.py"], "cursor": "c1", "continuation_digest": digest}, "task_id": "task-1"}}}
        result = self_heal_binding(snapshot, "n1", {"kind": "stale_cursor", "continuation_digest": "b" * 64})
        self.assertTrue(result.repaired)
        self.assertEqual(snapshot["nodes"]["n1"]["binding"]["task_id"], "task-1")
        self.assertEqual(snapshot["nodes"]["n1"]["binding"]["worktree"], "wt")
        self.assertEqual(snapshot["nodes"]["n1"]["binding"]["branch"], "main")
        self.assertEqual(snapshot["nodes"]["n1"]["binding"]["cursor"], "c1")
        self.assertEqual(snapshot["nodes"]["n1"]["binding"]["continuation_digest"], digest)

    def test_unrepairable_action_isolated_without_touching_unrelated_node(self):
        snapshot = {"nodes": {"bad": {"status": "running"}, "ready": {"status": "planned"}}}
        isolate_affected_action(snapshot, "bad", "provider timeout")
        self.assertEqual(snapshot["nodes"]["bad"]["status"], "blocked_unknown")
        self.assertEqual(snapshot["nodes"]["ready"]["status"], "planned")

    def test_crash_window_self_heal_event_replays_frozen_binding_and_isolation(self):
        frozen = {"task_id": "task-1", "worktree": "wt", "branch": "main", "allowlist": ["a.py"], "cursor": "c1", "continuation_digest": "a" * 64}
        snapshot = SimpleNamespace(run_id="run-1", event_sequence=0, authorization_digest="auth", node_contract_digest="contract", handles={}, nodes={
            "n1": {"status": "running", "binding": frozen.copy()},
            "n2": {"status": "planned"},
        })
        provenance = {"role": "system", "authorization_digest": "auth", "node_contract_digest": "contract"}
        events = [
            {"sequence": 1, "event": "binding_self_healed", "provenance": provenance, "data": {"run_id": "run-1", "node_id": "n1", "binding": frozen, "observation_kind": "stale_cursor"}},
            {"sequence": 2, "event": "action_isolated", "provenance": provenance, "data": {"run_id": "run-1", "node_id": "n1", "reason": "provider timeout"}},
        ]
        monitor = object.__new__(__import__("vibe_guide.monitor", fromlist=["Monitor"]).Monitor)
        monitor.paths = None
        with patch("vibe_guide.monitor.load_events", return_value=events):
            monitor._reconcile_unapplied_events(snapshot)
        self.assertEqual(snapshot.event_sequence, 2)
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.nodes["n1"]["binding"]["continuation_digest"], "a" * 64)
        self.assertEqual(snapshot.nodes["n2"]["status"], "planned")


if __name__ == "__main__":
    unittest.main()
