import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import daemon as dm
from vibe_guide import fsio, pm_inbox
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import run_dir
from vibe_guide.supervisor import supervisor_beat

RUN = "run-" + "e" * 32


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name), vibe_home=Path(self.tmp.name) / "home")
        (Path(self.tmp.name) / "home").mkdir()
        self.dir = run_dir(self.paths, RUN, create=True)
        self.inbox = self.dir / pm_inbox.INBOX_FILE

    def notify(self, kind, message="m"):
        return dm.PmNotifier(self.paths, RUN)(message, kind, "n1")

    def raw(self, data):
        with open(str(self.inbox), "ab") as stream:
            stream.write(data)


class ReaderTests(Base):
    def test_three_messages_then_zero_and_peek_does_not_advance(self):
        for index in range(3):
            self.notify("escalation", "m%d" % index)
        peeked = pm_inbox.read_unread(self.paths, RUN, peek=True)
        self.assertEqual(len(peeked["messages"]), 3)
        self.assertEqual(len(pm_inbox.read_unread(self.paths, RUN, peek=True)["messages"]), 3)
        first = pm_inbox.read_unread(self.paths, RUN)
        self.assertEqual([m["message"] for m in first["messages"]], ["m0", "m1", "m2"])
        self.assertEqual(pm_inbox.read_unread(self.paths, RUN)["messages"], [])
        self.notify("decision", "m3")
        self.assertEqual([m["message"] for m in pm_inbox.read_unread(self.paths, RUN)["messages"]], ["m3"])

    def test_cli_inbox_and_peek(self):
        for index in range(3):
            self.notify("escalation", "m%d" % index)
        argv = ["daemon", "inbox", "--run-id", RUN, "--json"]
        counts = []
        for extra in (["--peek"], [], []):
            result = run_cli(argv + extra, Path(self.tmp.name))
            counts.append(result.payload["count"])
        self.assertEqual(counts, [3, 3, 0])

    def test_missing_inbox_is_empty(self):
        self.assertEqual(pm_inbox.read_unread(self.paths, RUN)["messages"], [])

    def test_corrupt_cursor_rereads_from_the_start(self):
        self.notify("escalation", "a")
        (self.dir / pm_inbox.CURSOR_FILE).write_text("{not json", encoding="utf-8")
        result = pm_inbox.read_unread(self.paths, RUN)
        self.assertEqual(len(result["messages"]), 1)
        self.assertEqual(pm_inbox.read_unread(self.paths, RUN)["messages"], [])

    def test_cursor_past_end_or_off_a_line_boundary_rereads(self):
        self.notify("escalation", "a")
        for offset in (10 ** 6, 5):
            fsio.atomic_write_json(self.dir / pm_inbox.CURSOR_FILE, {"offset": offset})
            result = pm_inbox.read_unread(self.paths, RUN, peek=True)
            self.assertEqual(len(result["messages"]), 1, offset)
            self.assertEqual(result["cursor_status"], "reset")

    def test_unparsable_line_is_skipped_and_counted(self):
        self.notify("escalation", "a")
        self.raw(b"garbage\n[1]\n")
        self.notify("escalation", "b")
        result = pm_inbox.read_unread(self.paths, RUN)
        self.assertEqual([m["message"] for m in result["messages"]], ["a", "b"])
        self.assertEqual(result["skipped"], 2)

    def test_half_line_without_newline_is_not_read_until_finished(self):
        self.notify("escalation", "a")
        self.raw(b'{"kind": "decision", "message": "half"')
        self.assertEqual([m["message"] for m in pm_inbox.read_unread(self.paths, RUN)["messages"]], ["a"])
        self.assertEqual(pm_inbox.read_unread(self.paths, RUN)["messages"], [])
        self.raw(b"}\n")
        self.assertEqual([m["message"] for m in pm_inbox.read_unread(self.paths, RUN)["messages"]], ["half"])

    def test_cursor_write_failure_is_unknown_and_messages_stay_unread(self):
        self.notify("escalation", "a")
        with mock.patch.object(fsio, "atomic_write_json", side_effect=OSError("disk")):
            result = pm_inbox.read_unread(self.paths, RUN)
        self.assertEqual(result["cursor_status"], "unknown")
        self.assertEqual(pm_inbox.delivered(result), [])
        self.assertEqual(len(pm_inbox.read_unread(self.paths, RUN)["messages"]), 1)


class NotifierReceiptTests(Base):
    def test_receipt_written_per_push_and_message_format_unchanged(self):
        self.notify("run_failed", "boom")
        entry = json.loads(self.inbox.read_text(encoding="utf-8"))
        self.assertEqual(set(entry), {"at", "kind", "node_id", "message", "target"})
        receipt = json.loads((self.dir / pm_inbox.RECEIPT_FILE).read_text(encoding="utf-8"))
        self.assertEqual((receipt["kind"], receipt["status"]), ("run_failed", "no_address"))

    def test_receipt_failure_does_not_change_push_result(self):
        with mock.patch.object(pm_inbox, "write_receipt", return_value=False):
            result = self.notify("run_failed")
        self.assertEqual(result["status"], "no_address")


class BeatTests(Base):
    def beat(self):
        from vibe_guide import supervisor as sv
        with mock.patch.object(sv, "supervisor_preflight", return_value={"state": "idle"}):
            payload, text, ok = supervisor_beat(self.paths, RUN, None, now=time.time())
        return payload, text

    def stale_daemon(self):
        record = {"run_id": RUN, "pid": os.getpid(), "state": "running",
                  "heartbeat": time.time() - 10000, "stale_seconds": 120.0}
        (self.dir / dm.DAEMON_FILE).write_text(json.dumps(record), encoding="utf-8")

    def test_run_failed_gives_decision_and_run_complete_does_not(self):
        self.notify("run_complete", "done")
        payload, text = self.beat()
        self.assertNotEqual(payload["state"], "decision")
        self.assertNotIn("待处理消息", text)
        self.notify("run_failed", "broke")
        payload, text = self.beat()
        self.assertEqual(payload["state"], "decision")
        self.assertIn("broke", text)

    def test_delivered_once(self):
        self.notify("run_failed", "broke")
        self.beat()
        payload, text = self.beat()
        self.assertNotIn("broke", text)
        self.assertNotEqual(payload["state"], "decision")

    def test_cursor_failure_keeps_message_for_next_beat(self):
        self.notify("run_failed", "broke")
        with mock.patch.object(fsio, "atomic_write_json", side_effect=OSError("disk")):
            payload, text = self.beat()
        self.assertNotIn("broke", text)
        self.assertEqual(payload["pm_inbox"]["status"], "unknown")
        _, text = self.beat()
        self.assertIn("broke", text)

    def test_unknown_state_is_not_rewritten_to_decision(self):
        from vibe_guide import supervisor as sv
        self.notify("run_failed", "broke")
        payload = {"state": "unknown"}
        text = sv._beat_extras(self.paths, RUN, payload, "preflight unknown")
        self.assertEqual(payload["state"], "unknown")
        self.assertIn("preflight unknown", text)
        self.assertIn("broke", text)
        with mock.patch.object(sv, "supervisor_preflight", return_value={"state": "unknown"}):
            self.notify("run_failed", "again")
            payload, _, ok = supervisor_beat(self.paths, RUN, None, now=time.time())
        self.assertEqual(payload["state"], "unknown")
        self.assertFalse(ok)

    def test_stale_daemon_is_reported_without_restart_advice(self):
        self.stale_daemon()
        payload, text = self.beat()
        self.assertIn("守护程序失活", text)
        self.assertIn("heartbeat_stale", text)
        self.assertNotIn("daemon start", text)
        self.assertEqual(payload["daemon_inactive"]["reason"], "heartbeat_stale")

    def test_not_started_daemon_is_reported_and_nothing_is_spawned(self):
        with mock.patch.object(dm, "start_daemon") as start, mock.patch("subprocess.Popen") as popen:
            _, text = self.beat()
        self.assertIn("守护程序失活", text)
        start.assert_not_called()
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
