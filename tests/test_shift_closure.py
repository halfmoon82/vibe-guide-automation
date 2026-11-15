"""Issue #406: pin new shift + archive old shift is a closed loop, proven by host receipts."""
import unittest

from vibe_guide.handoff_rebind import evaluate_shift_closure

BASE = dict(run_id="r1", plan_id="p1", epoch=2, host_id="h1", project="/proj",
            old_thread="old", new_thread="new", worker_threads=("w1",))
READY = dict(new_registered=True, address_verified=True, heartbeat_switched=True, inflight_taken=True)


def receipt(tool, thread="new", **extra):
    base = {"native": True, "tool": tool, "ok": True, "thread_id": thread, "host_id": "h1",
            "run_id": "r1", "epoch": 2, "project": "/proj"}
    base.update(extra)
    return base


class ShiftClosureTests(unittest.TestCase):
    def run_it(self, **kw):
        args = dict(BASE, **READY)
        args.update(kw)
        return evaluate_shift_closure(**args)

    def test_normal_complete_cites_receipts(self):
        pin, arc = receipt("pin_thread"), receipt("archive_thread", "old")
        r = self.run_it(pin_receipt=pin, archive_receipt=arc)
        self.assertEqual(r["state"], "complete")
        self.assertEqual(r["receipts"], {"pin": pin, "archive": arc})

    def test_not_ready_blocks_archive(self):
        for key in READY:
            r = self.run_it(pin_receipt=receipt("pin_thread"), **{key: False})
            self.assertFalse(r["archive_allowed"], key)
            self.assertNotEqual(r["state"], "complete")
            self.assertIn(key, r["missing"])

    def test_archive_failed_after_pin_only_archive_missing(self):
        r = self.run_it(pin_receipt=receipt("pin_thread"), archive_receipt=None)
        self.assertEqual(r["state"], "pending")
        self.assertEqual(r["missing"], ["archive"])
        self.assertTrue(r["archive_allowed"])
        self.assertFalse(r["open_new_session"])

    def test_timeout_or_nl_is_unknown_not_success(self):
        for bad in ({"native": True, "tool": "pin_thread", "ok": None, "error": "timeout"},
                    {"native": False, "tool": "pin_thread", "ok": True, "thread_id": "new"},
                    "pinned it"):
            r = self.run_it(pin_receipt=bad, archive_receipt=None)
            self.assertEqual(r["state"], "unknown")
            self.assertIn("pin", r["missing"])
            self.assertTrue(r["recovery"])

    def test_wrong_target_refused(self):
        for kw in ({"old_thread": "new"}, {"old_thread": "w1"}):
            r = self.run_it(pin_receipt=receipt("pin_thread"), archive_receipt=receipt("archive_thread", "old"), **kw)
            self.assertEqual(r["state"], "refused")
            self.assertFalse(r["archive_allowed"])

    def test_receipt_mismatch_refused(self):
        for bad in (receipt("archive_thread", "other"), receipt("archive_thread", "old", host_id="h2"),
                    receipt("archive_thread", "old", run_id="r2"), receipt("archive_thread", "old", epoch=1)):
            r = self.run_it(pin_receipt=receipt("pin_thread"), archive_receipt=bad)
            self.assertEqual(r["state"], "refused")

    def test_replay_idempotent(self):
        kw = dict(pin_receipt=receipt("pin_thread"), archive_receipt=receipt("archive_thread", "old"))
        self.assertEqual(self.run_it(**kw), self.run_it(**kw))


if __name__ == "__main__":
    unittest.main()
