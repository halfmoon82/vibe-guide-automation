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
from vibe_guide.state import append_event
from vibe_guide.contracts import RunEvent, RunHandle
from vibe_guide.task_registry import TaskBinding, save_task_binding
from vibe_guide.paths import ProjectPaths


class IncidentReplayTests(IncidentFixture):
    def setUp(self):
        super().setUp()
        self.action = self.store.request(
            operation="resume", provider="claude-code", run_id=self.run_id,
            issue_id=NODE, role="developer", generation=1, sequence=2,
            native_tool="ccd_session_mgmt__send_message",
            request={"threadId": "t1", "hostId": "local"})
        self.store.complete(self.action["action_id"], {"threadId": "t1"}, native=False)

    def reader_refuses_receipt(self):
        """Poll a handle whose pending resume is the fixture's action with the real reader."""
        # Mirror the receipt in an isolated reader project with a real
        # visible-provider registry, without replacing the run's fake writer.
        (self.root / "receipt-reader").mkdir(exist_ok=True)
        reader_paths = ProjectPaths(self.root / "receipt-reader")
        reader_paths.vibe.mkdir(parents=True, exist_ok=True)
        (reader_paths.vibe / "state.json").write_text("{}")
        runner = ProviderActionRunner(reader_paths, "claude-code", "claude-code-visible")
        action = runner.store.request(
            operation="resume", provider=runner.provider, run_id=self.run_id,
            issue_id=NODE, role="developer", generation=1,
            sequence=getattr(self, "_reader_sequence", 0) + 1,
            native_tool="ccd_session_mgmt__send_message",
            request={"threadId": "t1", "hostId": "local"})
        self._reader_sequence = action["sequence"]
        runner.store.complete(action["action_id"], self.store.result(self.action["action_id"]), native=False)
        metadata = {
            "node_id": NODE, "role": "developer", "run_id": self.run_id, "task_id": "t1",
            "generation": 1, "host": "local", "pending_action": action["action_id"],
            "pending_operation": "resume",
        }
        handle = RunHandle(runner._handle_id(metadata, self.run_id))
        metadata["handle_id"] = handle.run_id
        if not getattr(self, "_reader_adopted", False):
            save_task_binding(reader_paths, TaskBinding(
                provider=runner.provider, mode="visible", issue_id=NODE, role="developer",
                task_id="t1", host="local", worktree=".", branch="fixture",
                run_id=self.run_id, generation=1))
            append_event(reader_paths, RunEvent("orphan_dispatch_adopted", {
                **{key: metadata[key] for key in ("run_id", "node_id", "role", "task_id", "host", "generation", "handle_id")},
                "action_id": action["action_id"], "request_digest": action["request_digest"],
            }))
            self._reader_adopted = True
        runner.store._atomic(runner._handle_path(handle.run_id), metadata)
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
