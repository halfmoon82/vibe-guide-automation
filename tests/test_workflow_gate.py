import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.paths import ProjectPaths
from vibe_guide.session_bypass import create_challenge, save_challenge
from vibe_guide.workflow_gate import require_entry


class WorkflowGateBypassTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)

    def _paths(self):
        directory = tempfile.TemporaryDirectory()
        paths = ProjectPaths(Path(directory.name))
        (paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (paths.vibe / "state.json").write_text(
            json.dumps({"workflow_version": 2, "session_gate": "s0_required"}),
            encoding="utf-8",
        )
        save_contract(paths, build_contract(paths.root, provider="test", host_id="local"))
        return directory, paths

    def test_entry_consumes_challenge_and_returns_wizard_bypassed(self):
        directory, paths = self._paths()
        try:
            record = create_challenge("entry-1", self.now)
            save_challenge(paths, record)
            gate = require_entry(
                paths, "entry-1", "BYPASS VIBE " + record.challenge, now=self.now
            )
            self.assertEqual(gate.status, "wizard_bypassed")
            follow_up = require_entry(paths, "entry-1", "complex request", now=self.now)
            self.assertEqual(follow_up.status, "wizard_bypassed")
            # The raw command is not persisted in the challenge record.
            raw = (paths.vibe / "session-bypass.json").read_text(encoding="utf-8")
            self.assertNotIn(record.challenge, raw)
            events = (paths.vibe / "session-events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(record.challenge, events)
            self.assertEqual(
                [json.loads(line)["event"] for line in events.splitlines()],
                ["session_bypass_granted", "wizard_bypassed"],
            )
        finally:
            directory.cleanup()

    def test_bypass_can_be_requested_after_the_wizard_screened_the_session(self):
        directory, paths = self._paths()
        try:
            self.assertEqual(
                require_entry(paths, "entry-1", "complex request", now=self.now).status,
                "session_screened",
            )
            record = create_challenge("entry-1", self.now)
            save_challenge(paths, record)
            gate = require_entry(
                paths, "entry-1", "BYPASS VIBE " + record.challenge, now=self.now
            )
            self.assertEqual(gate.status, "wizard_bypassed")
        finally:
            directory.cleanup()

    def test_child_session_cannot_request_or_inherit_bypass(self):
        directory, paths = self._paths()
        try:
            record = create_challenge("entry-1", self.now)
            save_challenge(paths, record)
            with self.assertRaises(PermissionError):
                require_entry(
                    paths,
                    "child-1",
                    "BYPASS VIBE " + record.challenge,
                    origin="worker_dispatch",
                )
            child_gate = require_entry(
                paths, "child-1", "worker work", origin="worker_dispatch"
            )
            self.assertEqual(child_gate.status, "session_screened")
        finally:
            directory.cleanup()


if __name__ == "__main__":
    unittest.main()
