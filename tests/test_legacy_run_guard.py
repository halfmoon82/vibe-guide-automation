"""Unfinished pre-5.2 runs are refused; finished ones stay readable."""
import json
import tempfile
import unittest
from pathlib import Path

from tests import test_monitor as fixtures
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.models import Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import LEGACY_RUN_BLOCK, legacy_run_in_flight, load_snapshot, run_dir


class LegacyRunGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        host = fixtures.MonitorTests("setUp")
        host.setUp()
        self.addCleanup(host.tearDown)
        self.paths = host.paths
        nodes = [fixtures.node("n1")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        card = build_authorization_card(plan, nodes, host.capabilities)
        self.monitor = Monitor(self.paths, plan, nodes)
        self.snapshot = self.monitor.start(authorize(card, "AUTHORIZE"), FakeRunner())

    def _as_legacy(self, status):
        path = run_dir(self.paths, self.snapshot.run_id, create=False) / "state.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw.update({"legacy_run": False, "legacy_evidence": None, "legacy_evidence_digest": "", "status": status})
        path.write_text(json.dumps(raw), encoding="utf-8")

    def test_new_snapshots_do_not_carry_legacy_keys(self):
        path = run_dir(self.paths, self.snapshot.run_id, create=False) / "state.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse({"legacy_run", "legacy_evidence", "legacy_evidence_digest"} & set(raw))

    def test_unfinished_legacy_run_is_blocked_on_resume_and_tick(self):
        self._as_legacy("running")
        for call in (self.monitor.resume, self.monitor.tick):
            with self.assertRaises(PermissionError) as raised:
                call(self.snapshot.run_id, FakeRunner())
            self.assertIn(LEGACY_RUN_BLOCK, str(raised.exception))
            self.assertIn("5.1.16", str(raised.exception))

    def test_legacy_keys_are_dropped_on_read_and_complete_is_not_blocked(self):
        self._as_legacy(self.snapshot.status)
        self.assertEqual(load_snapshot(self.paths, self.snapshot.run_id).run_id, self.snapshot.run_id)
        self.assertTrue(legacy_run_in_flight(self.paths, self.snapshot.run_id))
        self._as_legacy("complete")
        self.assertFalse(legacy_run_in_flight(self.paths, self.snapshot.run_id))


class LegacyRunCliTests(unittest.TestCase):
    """Through the public CLI: unfinished legacy run -> blocked, finished -> status works."""

    def setUp(self):
        from tests.test_end_to_end import EndToEndTests
        self.host = EndToEndTests("setUp")
        self.host.setUp()
        self.addCleanup(self.host.tearDown)
        self.host.initialize()
        self.host.create_plan()
        self.runner = self.host.local_runner()
        started = self.host.run_cli(
            ["monitor", "--plan", "e2e-plan", "--authorize", "AUTHORIZE", "--json"], runner=self.runner)
        self.assertEqual(started.exit_code, 0, started.text)
        self.run_id = started.payload["run_id"]
        self.state = self.host.root / ".vibe" / "runs" / self.run_id / "state.json"

    def _as_legacy(self, status):
        raw = json.loads(self.state.read_text(encoding="utf-8"))
        raw.update({"legacy_run": False, "legacy_evidence": None, "legacy_evidence_digest": "", "status": status})
        self.state.write_text(json.dumps(raw), encoding="utf-8")

    def test_resume_of_unfinished_legacy_run_is_blocked_not_unknown(self):
        self._as_legacy("running")
        result = self.host.run_cli(["resume", "--plan", "e2e-plan", "--json"], runner=self.runner)
        self.assertEqual(result.exit_code, 3, result.text)
        self.assertIn(LEGACY_RUN_BLOCK, result.payload["reason"])

    def test_status_of_finished_legacy_run_still_works(self):
        self._as_legacy("complete")
        result = self.host.run_cli(["status", "--plan", "e2e-plan", "--json"], runner=self.runner)
        self.assertEqual(result.exit_code, 0, result.text)
        self.assertEqual(result.payload["run_id"], self.run_id)


if __name__ == "__main__":
    unittest.main()
