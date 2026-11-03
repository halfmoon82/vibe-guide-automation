"""Integration fix: a node stuck in the reviewer phase gets the same self-heal.

The monitor's real retry marker carries ``role`` and ``reason_class`` and has no
``action`` key; the stuck session's role lives there because ``active_task`` is
None while a retry is pending.  The incident node (integration-review) is a
reviewer-phase node, so the pack, the printed verify command and ``heal-verify``
itself must all follow that role instead of defaulting to developer.
"""

import json
import unittest

from unittest import mock as _beat_mock

# 本文件在一秒内连续调用 beat 来模拟多拍；关掉 120 秒同状态防重复窗口（该行为由
# test_supervisor_runtime_consolidation 覆盖）。
_NO_BEAT_REPEAT_WINDOW = _beat_mock.patch("vibe_guide.supervisor.BEAT_REPEAT_WINDOW_SECONDS", 0)


def setUpModule():
    _NO_BEAT_REPEAT_WINDOW.start()


def tearDownModule():
    _NO_BEAT_REPEAT_WINDOW.stop()

from tests.test_self_heal_wiring import INVESTIGATE_LABEL, NODE, IncidentFixture
from vibe_guide import supervisor
from vibe_guide.cli import run_cli
from vibe_guide.state import load_events, load_snapshot


class ReviewerStallFixture(IncidentFixture):
    ROLE = "reviewer"

    def verify_as(self, *role):
        return run_cli(["heal-verify", "--run-id", self.run_id, "--node", NODE, "--json", *role],
                       self.root)

    def stalled_beat(self):
        for _ in range(3):
            out = self.beat()
        return out


class ReviewerPackTests(ReviewerStallFixture):
    def test_marker_shape_matches_what_the_monitor_writes(self):
        node = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertIsNone(node["active_task"])
        self.assertNotIn("action", node["retryable_action"])
        self.assertEqual(node["retryable_action"]["role"], "reviewer")

    def test_third_beat_pack_is_recognized_and_names_the_missing_field(self):
        out = self.stalled_beat()
        entry = out.payload["stall"]["nodes"][NODE]
        self.assertEqual(entry["tier"], "supervisor_investigate")
        pack = entry["pack"]
        self.assertTrue(pack["recognized"])
        self.assertEqual(pack["role"], "reviewer")
        self.assertEqual(pack["differences"], ["missing field: resumed"])
        self.assertEqual(pack["blocker_class"], "retryable")

    def test_beat_command_carries_the_reviewer_role(self):
        out = self.stalled_beat()
        self.assertTrue(out.text.startswith("需要监工排查："), out.text)
        self.assertIn(
            "vibe heal-verify --run-id {} --node {} --role reviewer".format(self.run_id, NODE),
            out.text)

    def test_developer_command_stays_without_a_role_flag(self):
        class Developer(IncidentFixture):
            pass
        case = Developer("setUp")
        case.setUp()
        self.addCleanup(case.tearDown)
        for _ in range(3):
            out = case.beat()
        self.assertIn("--node {}".format(NODE), out.text)
        self.assertNotIn("--role", out.text)

    def test_blocker_class_falls_back_to_the_action_key_of_older_markers(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        node = snapshot.nodes[NODE]
        node["retryable_action"] = {"action": "reconcile_pending_binding"}
        pack = supervisor._build_pack(self.paths, self.run_id, NODE, snapshot, [], "developer")
        self.assertEqual(pack["blocker_class"], "reconcile_pending_binding")
        self.assertEqual(pack["role"], "developer")


class ReviewerVerifyTests(ReviewerStallFixture):
    def test_verify_without_a_role_follows_the_persisted_pack(self):
        self.stalled_beat()
        out = self.verify_as()
        self.assertEqual(out.payload["status"], "verify_failed")
        self.assertEqual(out.payload["differences"], ["missing field: resumed"])
        self.fix_receipt()
        passed = self.verify_as()
        self.assertEqual(passed.payload["status"], "verify_passed")

    def test_verify_without_a_pack_follows_the_node_marker(self):
        out = self.verify_as()
        self.assertEqual(out.payload["differences"], ["missing field: resumed"])

    def test_explicit_role_wins_over_the_marker(self):
        self.stalled_beat()
        out = self.verify_as("--role", "developer")
        self.assertEqual(out.payload["status"], "verify_failed")
        self.assertNotEqual(out.payload["differences"], ["missing field: resumed"])

    def test_every_spelling_of_an_explicit_role_is_honored(self):
        self.stalled_beat()
        for spelling in (["--role=developer"], ["--rol", "developer"]):
            with self.subTest(spelling=spelling):
                out = self.verify_as(*spelling)
                self.assertNotEqual(out.payload["differences"], ["missing field: resumed"])
        out = self.verify_as("--role=reviewer")
        self.assertEqual(out.payload["differences"], ["missing field: resumed"])

    def test_no_escalation_before_two_real_failed_attempts(self):
        out = self.stalled_beat()
        self.assertTrue(out.text.startswith("需要监工排查："))
        self.verify_as()
        out = self.beat()
        self.assertTrue(out.text.startswith("需要监工排查："), out.text)
        self.assertEqual(out.payload["stall"]["nodes"][NODE]["tier"], "supervisor_investigate")
        self.verify_as()
        out = self.beat()
        self.assertTrue(out.text.startswith("需要人处理："), out.text)
        attempts = [e["data"] for e in load_events(self.paths, self.run_id)
                    if e["event"] == "self_heal_attempt"]
        self.assertEqual([a["outcome"] for a in attempts], ["failed", "failed"])
        for attempt in attempts:
            self.assertEqual(attempt["differences"], ["missing field: resumed"])
            self.assertEqual(attempt["operation"], "resume")

    def test_reviewer_incident_end_to_end(self):
        self.stalled_beat()
        self.fix_receipt()
        self.assertEqual(self.verify_as().payload["status"], "verify_passed")
        self.assertEqual(self.progress_entry()["unchanged_beats"], 0)
        resumed = self.beat()
        self.assertNotIn("需要监工排查：", resumed.text)
        self.assertNotIn("需要人处理", resumed.text)

    def test_status_label_for_a_stalled_reviewer_node(self):
        from vibe_guide.cli import _snapshot_result
        self.stalled_beat()
        view = _snapshot_result("resume", load_snapshot(self.paths, self.run_id), False,
                                paths=self.paths)
        self.assertEqual(view.payload["nodes"][NODE]["user_status"], INVESTIGATE_LABEL)


if __name__ == "__main__":
    unittest.main()


UPSTREAM_SESSION = "3c2b5d0c-4959-456a-8713-d8ee40a5b380"


class UpstreamReworkExemptionTests(IncidentFixture):
    """#386: integration review waits on an upstream it sent back; that is not a stall."""

    ROLE = "reviewer"

    def setUp(self):
        import os
        import time
        from pathlib import Path
        from unittest.mock import patch
        super().setUp()
        self.time = time
        home = Path(self.temporary.name) / "home"
        (home / ".claude" / "projects" / "proj").mkdir(parents=True)
        self.transcript = home / ".claude" / "projects" / "proj" / (UPSTREAM_SESSION + ".jsonl")
        patcher = patch.dict(os.environ, {"HOME": str(home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.os = os
        snapshot = load_snapshot(self.paths, self.run_id)
        # n1 plays integration-review (the incident fixture's retry-pending node);
        # "up" is the upstream node it sent back.
        snapshot.nodes["integration-review"] = snapshot.nodes.pop(NODE)
        # the status _queue_integration_review_rework leaves it in
        snapshot.nodes["integration-review"]["status"] = "rework"
        snapshot.nodes["up"] = {
            "status": "rework", "integration_finding_ref": {"node_id": "integration-review", "generation": 1},
            "developer_generation": 2,
            "active_task": {"generation": 2, "handle_id": "bridge-x", "role": "developer",
                            "task_id": UPSTREAM_SESSION},
        }
        # The authorized node set is fixed, so the extended snapshot is served in memory.
        self.snapshot = snapshot
        loader = patch("vibe_guide.supervisor.load_snapshot", lambda *args, **kwargs: self.snapshot)
        loader.start()
        self.addCleanup(loader.stop)

    def transcript_age(self, minutes):
        self.transcript.write_text('{"type":"assistant"}\n', encoding="utf-8")
        stamp = self.time.time() - minutes * 60
        self.os.utime(self.transcript, (stamp, stamp))

    def beats(self, count=4):
        for _ in range(count):
            out = self.beat()
        return out.payload["stall"]["nodes"]

    def test_integration_review_is_exempt_while_upstream_is_active(self):
        self.transcript_age(1)
        nodes = self.beats()
        self.assertNotIn("integration-review", nodes)
        self.assertNotIn("up", nodes)

    def test_dead_upstream_is_escalated_instead_of_integration_review(self):
        self.transcript_age(20)
        nodes = self.beats()
        self.assertNotIn("integration-review", nodes)
        self.assertEqual(nodes["up"]["tier"], "supervisor_investigate")

    def test_unreadable_upstream_evidence_is_not_activity(self):
        nodes = self.beats()  # no transcript at all: unknown
        self.assertIn("integration-review", nodes)

    def test_exemption_ends_when_upstream_is_accepted(self):
        self.transcript_age(1)
        self.snapshot.nodes["up"]["status"] = "accepted"
        nodes = self.beats()
        self.assertIn("integration-review", nodes)

    def test_other_nodes_are_not_exempt(self):
        self.transcript_age(1)
        self.snapshot.nodes["other"] = dict(self.snapshot.nodes["integration-review"])
        self.assertIn("other", self.beats())

    def test_platform_without_transcripts_exempts_only_while_upstream_idle_count_is_low(self):
        self.snapshot.nodes["up"]["active_task"] = {
            "generation": 2, "handle_id": "codex-1", "role": "developer", "task_id": "developer:up"}
        for beat in range(1, 6):
            nodes = self.beat().payload["stall"]["nodes"]
            if beat < 3:
                self.assertNotIn("integration-review", nodes)
        self.assertNotIn("integration-review", nodes)
        self.assertIn("up", nodes)
        self.assertEqual(nodes["up"]["tier"], "supervisor_investigate")
