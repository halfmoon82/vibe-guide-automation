"""A running node whose worker session went silent is stalled too.

The incident: three ``claude --bg`` workers died at start (API 403) while vibe
kept them ``running`` / 已启动, and every beat said ``idle`` for ~70 minutes.  The
stall detector only watched nodes that read 自动修复中, so a running node with a
dead session was never assessed.  A worker whose transcript has not been written
for 15 minutes is now assessed on the first beat that sees it, without touching
the progress fingerprint of healthy long-running workers.
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_self_heal_wiring import ESCALATE_LABEL, INVESTIGATE_LABEL, NODE, IncidentFixture
from vibe_guide import stall, supervisor
from vibe_guide.cli import _snapshot_result, run_cli
from vibe_guide.state import load_snapshot, map_user_status, save_snapshot

SESSION = "3c2b5d0c-4959-456a-8713-d8ee40a5b380"
SILENT_MINUTES = 20


class RunningWorkerFixture(IncidentFixture):
    """Node n1 is running under a live-looking session; its transcript age is the knob."""

    def setUp(self):
        super().setUp()
        self.home = Path(self.temporary.name) / "home"
        (self.home / ".claude" / "projects" / "proj").mkdir(parents=True)
        self.transcript = self.home / ".claude" / "projects" / "proj" / (SESSION + ".jsonl")
        patcher = patch.dict(os.environ, {"HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        snapshot = load_snapshot(self.paths, self.run_id)
        node = snapshot.nodes[NODE]
        node["status"] = "running"
        node.pop("retryable_action", None)
        node["active_task"] = {
            "generation": 1, "handle_id": "bridge-x", "role": "developer", "task_id": SESSION,
        }
        node["user_status"] = map_user_status(node)
        save_snapshot(self.paths, snapshot)

    def write_transcript(self, age_minutes):
        self.transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
        stamp = time.time() - age_minutes * 60
        os.utime(self.transcript, (stamp, stamp))

    def stall_entry(self, out):
        return out.payload["stall"]["nodes"].get(NODE)

    def view(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        return _snapshot_result("status", snapshot, False, paths=self.paths).payload["nodes"][NODE]


class SilentWorkerTests(RunningWorkerFixture):
    def test_silent_worker_is_reported_on_the_first_beat(self):
        self.write_transcript(SILENT_MINUTES)
        out = self.beat()
        entry = self.stall_entry(out)
        self.assertIsNotNone(entry, out.text)
        self.assertEqual(entry["tier"], "supervisor_investigate")
        self.assertTrue(out.text.startswith("需要监工排查："), out.text)
        self.assertIn("{} 分钟".format(SILENT_MINUTES), out.text)
        self.assertIn(str(self.transcript), out.text)

    def test_status_label_reads_the_truth_instead_of_started(self):
        self.write_transcript(SILENT_MINUTES)
        self.beat()
        self.assertEqual(self.view()["user_status"], INVESTIGATE_LABEL)

    def test_active_worker_is_never_flagged_however_many_beats_pass(self):
        self.write_transcript(1)
        for _ in range(6):
            out = self.beat()
        self.assertIsNone(self.stall_entry(out))
        self.assertEqual(out.text, "idle")
        self.assertEqual(self.view()["user_status"], "已启动")

    def test_probe_does_not_change_the_progress_fingerprint(self):
        before = stall.node_fingerprint(
            load_snapshot(self.paths, self.run_id).nodes[NODE],
            load_snapshot(self.paths, self.run_id).tasks, NODE)
        self.write_transcript(SILENT_MINUTES)
        self.beat()
        entry = self.progress_entry()
        self.assertEqual(entry["fingerprint"], before)

    def test_worker_that_writes_again_stops_being_a_stall(self):
        self.write_transcript(SILENT_MINUTES)
        self.beat()
        self.write_transcript(0)
        out = self.beat()
        self.assertIsNone(self.stall_entry(out))
        self.assertEqual(self.view()["user_status"], "已启动")

    def test_missing_transcript_is_unknown_not_flagged_as_stalled(self):
        out = self.beat()
        self.assertIsNone(self.stall_entry(out))
        self.assertEqual(out.payload["stall"]["unknown_liveness"], [NODE])
        self.assertNotEqual(out.text, "idle")

    def test_worker_that_already_delivered_is_not_flagged(self):
        self.write_transcript(SILENT_MINUTES)
        deliveries = self.root / ".vibe" / "provider-actions" / "deliveries"
        deliveries.mkdir(parents=True, exist_ok=True)
        (deliveries / "{}-{}-developer-g1.json".format(self.run_id, NODE)).write_text("{}")
        out = self.beat()
        self.assertIsNone(self.stall_entry(out))

    def test_preflight_stays_read_only(self):
        self.write_transcript(SILENT_MINUTES)
        run_cli(["supervisor-preflight", "--run-id", self.run_id,
                 "--session-record", str(self.record)], self.root)
        self.assertFalse(stall.progress_path(self.paths, self.run_id).exists())

    def test_background_session_is_found_through_its_handle_record(self):
        records = self.root / ".vibe" / "provider-actions" / "claude-bg-sessions"
        records.mkdir(parents=True, exist_ok=True)
        (records / "claude-bg-n1-1-abc.json").write_text(json.dumps(
            {"claims": {"session_id": SESSION, "handle_id": "claude-bg-n1-1-abc"}}))
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE]["active_task"] = {
            "generation": 1, "handle_id": "claude-bg-n1-1-abc", "role": "developer",
            "task_id": "developer:n1",
        }
        save_snapshot(self.paths, snapshot)
        self.write_transcript(SILENT_MINUTES)
        out = self.beat()
        self.assertIsNotNone(self.stall_entry(out), out.text)


class EscalationAndVerifyTests(RunningWorkerFixture):
    def test_heal_verify_fails_while_silent_and_passes_once_the_worker_writes(self):
        self.write_transcript(SILENT_MINUTES)
        self.beat()
        failed = self.verify()
        self.assertEqual(failed.payload["status"], "verify_failed", failed.payload)
        self.assertIn("silent", " ".join(failed.payload["differences"]))
        self.write_transcript(0)
        passed = self.verify()
        self.assertEqual(passed.payload["status"], "verify_passed", passed.payload)

    def test_two_failed_attempts_escalate_to_the_person(self):
        self.write_transcript(35)
        self.beat()
        self.verify()
        self.verify()
        out = self.beat()
        entry = self.stall_entry(out)
        self.assertEqual(entry["tier"], "escalate_to_pm", out.text)
        self.assertTrue(out.text.startswith("需要人处理："), out.text)
        self.assertEqual(self.view()["user_status"], ESCALATE_LABEL)


class ReviewFixesTests(RunningWorkerFixture):
    """Findings from the independent review of the silent-worker fix."""

    def test_unresolvable_session_is_not_reported_as_unknown_noise(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE]["active_task"]["task_id"] = "developer:n1"
        save_snapshot(self.paths, snapshot)
        out = self.beat()
        self.assertEqual(out.text, "idle")

    def test_known_session_without_a_transcript_is_said_aloud(self):
        out = self.beat()
        self.assertIn("空转评估未知", out.text)
        self.assertIn(NODE, out.text)
        self.assertIn(SESSION, out.text)

    def test_a_legitimate_long_command_never_reaches_the_person(self):
        self.write_transcript(SILENT_MINUTES)
        self.beat()
        self.verify()
        self.verify()
        out = self.beat()
        self.assertEqual(self.stall_entry(out)["tier"], "supervisor_investigate", out.text)
        self.assertNotIn("需要人处理", out.text)

    def test_only_this_generations_delivery_counts(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE]["active_task"]["generation"] = 2
        save_snapshot(self.paths, snapshot)
        self.write_transcript(SILENT_MINUTES)
        deliveries = self.root / ".vibe" / "provider-actions" / "deliveries"
        deliveries.mkdir(parents=True, exist_ok=True)
        old = deliveries / "{}-{}-developer-g1.json".format(self.run_id, NODE)
        old.write_text("{}")
        self.assertIsNotNone(self.stall_entry(self.beat()))
        (deliveries / "{}-{}-developer-g2.json".format(self.run_id, NODE)).write_text("{}")
        self.assertIsNone(self.stall_entry(self.beat()))

    def test_silent_node_text_says_minutes_not_beats(self):
        self.write_transcript(SILENT_MINUTES)
        out = self.beat()
        self.assertIn("静默 {} 分钟".format(SILENT_MINUTES), out.text)
        self.assertNotIn("连续 1 拍", out.text)

    def test_rework_and_review_sessions_are_watched_too(self):
        for status in ("rework", "review"):
            with self.subTest(status=status):
                snapshot = load_snapshot(self.paths, self.run_id)
                snapshot.nodes[NODE]["status"] = status
                save_snapshot(self.paths, snapshot)
                self.write_transcript(SILENT_MINUTES)
                self.assertIsNotNone(self.stall_entry(self.beat()), status)
                self.assertEqual(self.view()["user_status"], INVESTIGATE_LABEL)


class LabelTests(unittest.TestCase):
    def test_marker_relabels_a_running_node_only_when_it_has_a_session(self):
        marked = {"status": "running", "stall_tier": "supervisor_investigate"}
        self.assertEqual(map_user_status(marked), "已启动")
        marked["active_task"] = {"role": "developer"}
        self.assertEqual(map_user_status(marked), INVESTIGATE_LABEL)

    def test_marker_never_relabels_delivered_or_accepted_nodes(self):
        for status in ("delivered", "accepted", "complete"):
            with self.subTest(status=status):
                node = {"status": status, "active_task": {"role": "developer"},
                        "stall_tier": "supervisor_investigate"}
                self.assertNotEqual(map_user_status(node), INVESTIGATE_LABEL)


if __name__ == "__main__":
    unittest.main()
