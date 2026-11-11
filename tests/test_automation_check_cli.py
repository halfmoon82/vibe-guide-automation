"""V6 P0a wiring: `vibe automation-check` gates automation creation.

validate_automation_spec / verify_automation_readback are unit-tested in
test_authorization.py; this file proves the CLI actually calls them and that
the first-shift handoff tells the host to run the gate before creating a beat.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import main, _first_shift_handoff
from vibe_guide.paths import ProjectPaths

HB = {"kind": "heartbeat", "target_thread_id": "t-1"}
CRON = {"kind": "cron", "cron": "0 * * * *"}


class AutomationCheckCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def run_check(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["automation-check", "--project-root", str(self.root), "--json", *args])
        return code, json.loads(out.getvalue())

    def test_valid_specs_pass_before_create(self):
        for spec in (HB, CRON):
            code, payload = self.run_check("--spec", json.dumps(spec))
            self.assertEqual(code, 0, spec)
            self.assertEqual((payload["stage"], payload["status"]), ("pre_create", "ok"))

    def test_heartbeat_with_cron_or_without_target_is_refused(self):
        for spec in (dict(HB, cron="0 * * * *"), {"kind": "heartbeat"}, {"kind": "weekly"}):
            code, payload = self.run_check("--spec", json.dumps(spec))
            self.assertEqual(code, 3, spec)
            self.assertEqual(payload["status"], "rejected")

    def test_missing_or_broken_spec_is_refused(self):
        self.assertEqual(self.run_check()[0], 3)
        code, payload = self.run_check("--spec", "{not json")
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "blocked_invalid")

    def test_readback_and_fired_must_come_together(self):
        code, payload = self.run_check("--spec", json.dumps(HB), "--readback", json.dumps(HB))
        self.assertEqual(code, 3)
        self.assertEqual(payload["status"], "blocked_invalid")

    def test_post_create_matches_and_mismatches(self):
        fired = {"kind": "heartbeat", "thread_id": "t-1"}
        code, payload = self.run_check("--spec", json.dumps(HB), "--readback", json.dumps(HB),
                                       "--fired", json.dumps(fired))
        self.assertEqual((code, payload["stage"], payload["status"]), (0, "post_create", "ok"))
        wrong = {"kind": "heartbeat", "thread_id": "other"}
        code, payload = self.run_check("--spec", json.dumps(HB), "--readback", json.dumps(HB),
                                       "--fired", json.dumps(wrong))
        self.assertEqual((code, payload["status"]), (3, "rejected"))

    def test_first_shift_handoff_names_the_gate(self):
        paths = ProjectPaths(self.root)
        text = _first_shift_handoff(paths, "plan-1", "run-1")
        self.assertIn("vibe automation-check --spec", text)
        self.assertIn("--readback", text)


if __name__ == "__main__":
    unittest.main()
