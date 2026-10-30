import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from vibe_guide import daemon as dm
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import run_dir
from vibe_guide.supervisor import SupervisorLease, render_beat_text

RUN = "run-" + "d" * 32
RESUME = "resume --plan p --run-id " + RUN


class BeatDaemonAdvanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name), vibe_home=Path(self.tmp.name) / "home")
        (Path(self.tmp.name) / "home").mkdir()
        run_dir(self.paths, RUN, create=True)

    def start_daemon_record(self, heartbeat=None, state="running"):
        record = {"run_id": RUN, "pid": os.getpid(), "state": state,
                  "heartbeat": time.time() if heartbeat is None else heartbeat, "stale_seconds": 120.0}
        (run_dir(self.paths, RUN) / dm.DAEMON_FILE).write_text(json.dumps(record), encoding="utf-8")

    def hold_lease(self):
        self.assertTrue(SupervisorLease(self.paths).acquire(RUN))

    def beat(self, state):
        payload = {"state": state, "reason": "why"}
        return payload, render_beat_text(self.paths, RUN, payload, plan_id="p")

    def test_work_beat_without_a_daemon_still_names_resume(self):
        _, text = self.beat("work")
        self.assertIn(RESUME, text)

    def test_work_beat_with_a_live_daemon_holding_the_lease_does_not_offer_resume(self):
        self.hold_lease()
        self.start_daemon_record()
        payload, text = self.beat("work")
        self.assertNotIn(RESUME, text)
        self.assertIn("守护程序", text)
        self.assertEqual(payload["advance_by"], "daemon")

    def test_unknown_beat_with_a_live_daemon_does_not_offer_resume(self):
        self.hold_lease()
        self.start_daemon_record()
        payload, text = self.beat("unknown")
        self.assertNotIn(RESUME, text)
        self.assertIn("why", text)
        self.assertEqual(payload["advance_by"], "daemon")

    def test_stale_daemon_heartbeat_keeps_the_manual_hint(self):
        self.hold_lease()
        self.start_daemon_record(heartbeat=time.time() - 3600)
        payload, text = self.beat("work")
        self.assertIn(RESUME, text)
        self.assertNotIn("advance_by", payload)

    def test_daemon_without_the_lease_keeps_the_manual_hint(self):
        self.start_daemon_record()
        _, text = self.beat("work")
        self.assertIn(RESUME, text)

    def test_exited_daemon_keeps_the_manual_hint(self):
        self.hold_lease()
        self.start_daemon_record(state="exited")
        _, text = self.beat("work")
        self.assertIn(RESUME, text)

    def test_shipped_card_does_not_tell_the_supervisor_to_resume_unconditionally(self):
        from vibe_guide.protocols import load_protocol
        card = load_protocol("supervisor-card")
        self.assertNotIn("然后 `resume` 推进", card)
        self.assertIn("守护程序", card)


if __name__ == "__main__":
    unittest.main()
