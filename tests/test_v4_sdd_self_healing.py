import unittest

from vibe_guide.models import HealingResult, ObservationDisposition
from vibe_guide.monitor import (
    classify_v4_observation,
    heal_v4_node,
    record_v4_healing,
)


class V4SddSelfHealingTests(unittest.TestCase):
    def snapshot(self):
        return {
            "run_id": "run-v4",
            "nodes": {
                "bad": {
                    "status": "running",
                    "task_id": "task-bad",
                    "generation": 3,
                    "worktree": "/managed/bad",
                    "branch": "codex/bad",
                    "allowlist": ["a.py"],
                    "cursor": "cursor-1",
                    "diff": ["a.py"],
                },
                "ready": {"status": "planned", "task_id": "task-ready"},
            },
            "healing_events": [],
        }

    def test_classification_keeps_timeout_unknown_and_probe_repairable(self):
        self.assertEqual(classify_v4_observation({"kind": "provider_timeout"}).status, "degraded")
        self.assertEqual(classify_v4_observation({"kind": "binding_probe"}).status, "repairable")
        self.assertEqual(classify_v4_observation({"kind": "unknown"}).status, "unknown")

    def test_same_task_retry_repairs_node_without_global_binding_gate(self):
        snapshot = self.snapshot()
        result = heal_v4_node(snapshot, "bad", {
            "kind": "binding_drift",
            "probe": {"status": "observed"},
            "same_task": {"task_id": "task-bad", "action": "retry", "status": "ok"},
        })
        self.assertIsInstance(result, HealingResult)
        self.assertTrue(result.repaired)
        self.assertEqual(result.task_id, "task-bad")
        self.assertEqual(snapshot["nodes"]["bad"]["status"], "running")
        self.assertEqual(snapshot["nodes"]["bad"]["generation"], 3)
        self.assertEqual(snapshot["nodes"]["ready"]["status"], "planned")

    def test_native_handoff_bootstrap_then_local_fallback_is_node_local(self):
        snapshot = self.snapshot()
        result = heal_v4_node(snapshot, "bad", {
            "kind": "missing_visible_locate_evidence",
            "probe": {"status": "unknown"},
            "same_task": {"action": "locate", "status": "failed"},
            "native": {"action": "handoff", "status": "failed"},
            "bootstrap": {"action": "metadata_bootstrap", "status": "failed"},
            "local": {"action": "fallback", "status": "ok"},
        })
        self.assertTrue(result.repaired)
        self.assertEqual(snapshot["nodes"]["bad"]["status"], "running")
        self.assertFalse(snapshot["nodes"]["bad"].get("isolated", False))

    def test_unsafe_recovery_is_rejected_and_only_node_is_isolated(self):
        snapshot = self.snapshot()
        result = heal_v4_node(snapshot, "bad", {
            "kind": "binding_drift",
            "action": "reset",
            "worktree": "/host/checkout",
            "allowlist": ["secret.py"],
            "successor": True,
        })
        self.assertFalse(result.repaired)
        self.assertTrue(result.isolated)
        self.assertEqual(snapshot["nodes"]["bad"]["status"], "blocked_unknown")
        self.assertEqual(snapshot["nodes"]["ready"]["status"], "planned")
        self.assertEqual(snapshot["nodes"]["bad"]["task_id"], "task-bad")

    def test_unsafe_action_variants_are_rejected(self):
        for action in ("git reset --hard", "manual stash apply", "git clean -fd", "host-checkout-write"):
            snapshot = self.snapshot()
            result = heal_v4_node(snapshot, "bad", {"kind": "binding_drift", "action": action})
            self.assertTrue(result.isolated, action)
            self.assertFalse(result.repaired, action)

    def test_malformed_observation_returns_structured_unknown(self):
        result = heal_v4_node(self.snapshot(), "bad", None)
        self.assertFalse(result.repaired)
        self.assertTrue(result.isolated)
        self.assertIn("malformed", result.reason)

    def test_timeout_or_unknown_cannot_be_repaired_by_successful_probe(self):
        for kind in ("provider_timeout", "unknown"):
            snapshot = self.snapshot()
            result = heal_v4_node(snapshot, "bad", {
                "kind": kind, "probe": {"status": "ok"},
            })
            self.assertFalse(result.repaired, kind)
            self.assertIn(snapshot["nodes"]["bad"]["status"], {"blocked_unknown", "degraded"})

    def test_target_and_writer_variants_are_rejected(self):
        for payload in (
            {"target": "/other/project"},
            {"successor_candidate": True},
            {"operation": "create-second-writer"},
        ):
            snapshot = self.snapshot()
            result = heal_v4_node(snapshot, "bad", {"kind": "binding_drift", **payload})
            self.assertTrue(result.isolated)
            self.assertFalse(result.repaired)

    def test_structured_target_host_and_writer_keys_override_success_signals(self):
        for key, value in (
            ("target-contract", {"root": "/other"}),
            ("TARGET_CONTRACT", {"branch": "other"}),
            ("host-checkout", True),
            ("host_checkout", True),
            ("second writer", True),
            ("successor-candidate", True),
        ):
            snapshot = self.snapshot()
            result = heal_v4_node(snapshot, "bad", {
                "kind": "binding_drift", key: value,
                "same_task": {"status": "ok", "task_id": "task-bad"},
            })
            self.assertFalse(result.repaired, key)
            self.assertTrue(result.isolated, key)

    def test_action_value_second_writer_and_target_variants_are_rejected_with_success_step(self):
        for action in ("second writer", "successor candidate", "host checkout write", "target change"):
            snapshot = self.snapshot()
            result = heal_v4_node(snapshot, "bad", {
                "kind": "binding_drift", "operation": action,
                "same_task": {"status": "ok", "task_id": "task-bad"},
            })
            self.assertFalse(result.repaired, action)
            self.assertTrue(result.isolated, action)

    def test_record_healing_appends_history_without_overwriting_old_events(self):
        snapshot = self.snapshot()
        snapshot["healing_events"].append({"sequence": 1, "kind": "old"})
        record_v4_healing(snapshot, "bad", HealingResult(True, reason="retry", task_id="task-bad"))
        self.assertEqual(snapshot["healing_events"][0]["kind"], "old")
        self.assertEqual(snapshot["healing_events"][1]["node_id"], "bad")


if __name__ == "__main__":
    unittest.main()
