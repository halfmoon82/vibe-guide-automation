"""The daemon replaces a rejected integration reviewer whose session is gone."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide import daemon as dm
from vibe_guide.decision_boundary import decision_list
from vibe_guide.state import load_events, load_snapshot
from vibe_guide.task_registry import load_task_binding, save_task_binding

from tests.test_reviewer_chain_defects import _OneNodeCase
from tests import support_vibe_home as hermetic


setUpModule, tearDownModule = hermetic.module_hooks()

SECRET = "REVIEWER-SECRET-VALUE"
BAD = ("accepted", {"delivery_evidence": {"thread_status": "complete", "note": SECRET}})


class ReviewerHealTests(_OneNodeCase):
    def setUp(self):
        super().setUp()
        self.pushed = []
        self.snapshot, self.runner = self._to_review([BAD, BAD])
        self.run_id = self.snapshot.run_id
        self.snapshot = self.monitor.tick(self.run_id, self.runner)
        self.assertEqual(self.snapshot.nodes["n1"]["acceptance_format_rejections"]["count"], 1)

    def set_binding(self, status):
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=self.run_id)
        binding.status = status
        save_task_binding(self.paths, binding)
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.tasks["n1:reviewer"] = binding.to_dict()
        self.monitor._save_snapshot(snapshot)

    def daemon(self, **kwargs):
        def notifier(message, kind, node_id=""):
            self.pushed.append((kind, node_id, message))
            return {"status": "sent"}
        return dm.Daemon(
            self.paths, self.run_id, SimpleNamespace(monitor=self.monitor),
            dm.DaemonConfig(interval=0, heal_interval=0), sleep=lambda s: None,
            notifier=notifier, assess=lambda: {"status": "ok", "nodes": {}}, **kwargs)

    def heal(self, daemon):
        daemon.heal_pass(load_snapshot(self.paths, self.run_id))
        return load_snapshot(self.paths, self.run_id).nodes["n1"]

    def _last_event_name(self):
        names = [e["event"] for e in load_events(self.paths, self.run_id)
                 if e["data"].get("node_id") == "n1" and e["event"] != "acceptance_correction_notified"]
        return names[-1]

    def kinds(self):
        return [e["data"].get("kind") for e in load_events(self.paths, self.run_id)
                if e["event"] == "daemon_heal"]

    def test_ended_rejected_reviewer_is_replaced_once(self):
        self.set_binding("stopped")
        node = self.heal(self.daemon(sessions=lambda snapshot: {"live": [], "unknown": None}))
        self.assertEqual(node["status"], "rework")
        self.assertIs(node["reviewer_replacement_required"], True)
        retry = node.get("retryable_action") or {}
        self.assertEqual(retry.get("role"), "reviewer")
        self.assertIs(retry.get("successor"), True)
        self.assertIn("reviewer_replace", self.kinds())
        self.assertNotIn(SECRET, str(load_events(self.paths, self.run_id)))

    def test_second_occurrence_is_recorded_instead_of_replacing(self):
        self.set_binding("stopped")
        daemon = self.daemon(sessions=lambda snapshot: {"live": [], "unknown": None})
        self.heal(daemon)
        # The successor reviewer is dispatched, delivers a bad claim again and exits.
        snapshot = self.monitor.tick(self.run_id, self.runner)
        snapshot = self.monitor.tick(self.run_id, self.runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        self.assertEqual(self._last_event_name(), "acceptance_rejected")
        self.set_binding("stopped")
        replaced = self.kinds().count("reviewer_replace")
        self.assertEqual(replaced, 1)
        node = self.heal(daemon)
        self.assertEqual(self.kinds().count("reviewer_replace"), 1)
        self.assertEqual(node["status"], "review")
        self.assertEqual(decision_list(self.paths, self.run_id), [])
        self.assertEqual(self.pushed, [])
        self.assertIn("n1", daemon._heal_state()["unresolved"])
        self.heal(daemon)
        self.assertEqual(self.pushed, [])
        self.assertEqual(self.kinds().count("reviewer_replace"), 1)

    def test_live_reviewer_is_left_alone(self):
        self.set_binding("running")
        node = self.heal(self.daemon(sessions=lambda snapshot: {"live": ["abcd1234"], "unknown": None}))
        self.assertEqual(node["status"], "review")
        self.assertNotIn("reviewer_replace", self.kinds())


    def test_unreadable_session_state_is_recorded_unknown_and_not_dispatched(self):
        self.set_binding("stopped")
        daemon = self.daemon(sessions=lambda snapshot: {"live": [], "unknown": "cannot list"})
        node = self.heal(daemon)
        self.assertEqual(node["status"], "review")
        self.assertIn("reviewer_replace_unknown", self.kinds())

    def test_sessions_are_probed_for_this_node_only(self):
        self.set_binding("stopped")
        seen = []
        def sessions(snapshot):
            seen.append(sorted(snapshot.tasks))
            return {"live": [], "unknown": None}
        self.heal(self.daemon(sessions=sessions))
        self.assertEqual(seen, [["n1:reviewer"]])

    def test_limit_reached_is_left_to_the_existing_escalation(self):
        self.set_binding("stopped")
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes["n1"]["acceptance_format_rejections"]["count"] = 3
        self.monitor._save_snapshot(snapshot)
        node = self.heal(self.daemon())
        self.assertEqual(node["status"], "review")
        self.assertNotIn("reviewer_replace", self.kinds())

class R4ReviewerHealingTests(_OneNodeCase):
    def setUp(self):
        super().setUp()
        rules = patch("vibe_guide.protocol_adoption.effective_responsibility", return_value={"status": "effective"})
        rules.start()
        self.addCleanup(rules.stop)

    def test_format_rejection_does_not_replace_a_terminal_reviewer(self):
        snapshot, runner = self._to_review([BAD])
        snapshot = self.monitor.tick(snapshot.run_id, runner)
        binding = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        original = (binding.task_id, binding.generation, binding.cursor)
        binding.status = "stopped"
        save_task_binding(self.paths, binding)
        daemon = dm.Daemon(self.paths, snapshot.run_id, SimpleNamespace(monitor=self.monitor),
            dm.DaemonConfig(interval=0, heal_interval=0), sleep=lambda s: None,
            notifier=lambda *args, **kwargs: {"status": "sent"},
            sessions=lambda current: {"live": [], "unknown": None},
            assess=lambda: {"status": "ok", "nodes": {}})
        daemon.heal_pass(snapshot)
        current = load_snapshot(self.paths, snapshot.run_id).nodes["n1"]
        self.assertFalse(current.get("reviewer_replacement_required"))
        self.assertFalse((current.get("retryable_action") or {}).get("successor"))
        final = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        self.assertEqual((final.task_id, final.generation, final.cursor), original)

    def test_repair_after_format_rejection_preserves_original_reviewer(self):
        snapshot, runner = self._to_review([BAD])
        snapshot = self.monitor.tick(snapshot.run_id, runner)
        original = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        with patch("vibe_guide.worker_liveness.session_ended", return_value=True):
            self.monitor.repair_node("n1", snapshot.run_id)
        current = load_snapshot(self.paths, snapshot.run_id).nodes["n1"]
        self.assertFalse(current.get("reviewer_replacement_required"))
        self.assertFalse((current.get("retryable_action") or {}).get("successor"))
        final = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        self.assertEqual((final.task_id, final.generation), (original.task_id, original.generation))

    def test_three_format_rejections_are_not_three_failed_recoveries(self):
        snapshot, runner = self._to_review([BAD, BAD, BAD])
        original = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        for _ in range(3):
            snapshot = self.monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes["n1"]
        self.assertFalse(current.get("reviewer_replacement_required"))
        self.assertEqual(runner.stop_calls, [])
        final = load_task_binding(self.paths, "n1", "reviewer", run_id=snapshot.run_id)
        self.assertEqual((final.task_id, final.generation), (original.task_id, original.generation))



if __name__ == "__main__":
    unittest.main()
