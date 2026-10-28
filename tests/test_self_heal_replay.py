"""Replay of the stuck-receipt incident through the real beat / heal-verify / reader path.

A resume receipt written as ``{"threadId": ...}`` (no ``resumed: true``) is
refused by the reader forever.  The run must stop reading as self-healing,
hand the supervisor a pack, accept the supervisor's fix only on the reader's
own verdict, and refuse to move on when the run evidence no longer verifies.
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

from tests.test_self_heal_wiring import (
    ESCALATE_LABEL,
    INVESTIGATE_LABEL,
    NODE,
    RESUMING_TEXT,
    IncidentFixture,
)
from vibe_guide.cli import _snapshot_result
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.state import load_events, load_snapshot


class IncidentReplayTests(IncidentFixture):
    def reader_refuses_receipt(self):
        """Poll a handle whose pending resume is the fixture's action with the real reader."""
        runner = ProviderActionRunner(self.paths, "claude-code", "claude-code-visible")
        handle = type("Handle", (), {"run_id": "replay-handle"})()
        runner.store._atomic(runner._handle_path(handle.run_id), {
            "node_id": NODE, "role": "developer", "run_id": self.run_id, "task_id": "t1",
            "generation": 1, "host": "local", "pending_action": self.action["action_id"],
            "pending_operation": "resume",
        })
        events = runner.poll(handle)
        return any(
            e.event == "visibility_unknown" and "resume is unverified" in e.data.get("reason", "")
            for e in events
        )

    def status(self):
        return _snapshot_result(
            "resume", load_snapshot(self.paths, self.run_id), False, paths=self.paths)

    def test_incident_from_stuck_receipt_to_continued_progress(self):
        self.assertTrue(self.reader_refuses_receipt())
        self.assertIn(RESUMING_TEXT, self.status().text)

        first, second = self.beat(), self.beat()
        for early in (first, second):
            self.assertNotIn("需要监工排查：", early.text)
        third = self.beat()
        self.assertTrue(third.text.startswith("需要监工排查："), third.text)
        pack = third.payload["stall"]["nodes"][NODE]["pack"]
        self.assertEqual(pack["differences"], ["missing field: resumed"])

        stalled = self.status()
        self.assertNotIn(RESUMING_TEXT, stalled.text)
        self.assertEqual(stalled.payload["nodes"][NODE]["user_status"], INVESTIGATE_LABEL)
        self.assertNotEqual(stalled.payload["nodes"][NODE]["user_status"], "自动修复中")

        before_fix = self.verify()
        self.assertEqual(before_fix.payload["status"], "verify_failed")
        self.assertTrue(self.reader_refuses_receipt())

        self.fix_receipt()
        after_fix = self.verify()
        self.assertEqual(after_fix.payload["status"], "verify_passed")
        self.assertFalse(self.reader_refuses_receipt())

        outcomes = [e["data"]["outcome"] for e in self.attempts()]
        self.assertEqual(outcomes, ["failed", "passed"])
        persisted = json.loads(
            (self.paths.vibe / "runs" / self.run_id / "events.jsonl").read_text(encoding="utf-8")
            .splitlines()[-1])["data"]
        self.assertEqual(
            sorted(persisted),
            ["action_id", "differences", "node_id", "operation", "outcome", "run_id",
             "tier", "unchanged_beats"],
        )

        resumed = self.beat()
        self.assertNotIn("需要监工排查：", resumed.text)
        self.assertNotIn(ESCALATE_LABEL, resumed.text)
        self.assertEqual(self.progress_entry()["unchanged_beats"], 1)
        self.assertIn(RESUMING_TEXT, self.status().text)

    def test_a_fix_that_keeps_failing_ends_with_the_product_manager(self):
        for _ in range(3):
            self.beat()
        self.verify()
        self.verify()
        out = self.beat()
        self.assertTrue(out.text.startswith("需要人处理："), out.text)
        self.assertEqual(self.status().payload["nodes"][NODE]["user_status"], "需要人处理")

    def test_broken_digest_chain_refuses_to_advance_and_reports(self):
        for _ in range(3):
            self.beat()
        self.fix_receipt()
        path = self.paths.vibe / "runs" / self.run_id / "events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[1])
        record["data"]["injected"] = "by hand"
        lines[1] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        out = self.verify()
        self.assertEqual(out.payload["status"], "blocked_integrity")
        self.assertIn("digest", out.payload["reason"])
        self.assertNotEqual(out.exit_code, 0)
        self.assertEqual(self.progress_entry()["unchanged_beats"], 3)
        with self.assertRaises(ValueError):
            load_events(self.paths, self.run_id)


if __name__ == "__main__":
    unittest.main()
