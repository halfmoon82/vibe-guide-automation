"""Claude Code registers SendMessage as its push wake-up channel.

Registration is not verification: the 30-minute tier stays a forced choice
until the project's push-wake-verification file holds both a success and a
failure evidence for claude-code.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import (
    current_supervisor_address,
    heartbeat_interval_options,
    register_supervisor_address,
    wake_channel,
)


class ClaudeCodePushWakeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".vibe").mkdir()
        self.paths = ProjectPaths(self.root)

    def test_channel_is_send_message_for_every_claude_spelling(self):
        for provider in ("claude-code", "claude-code-visible", "claude-bg"):
            self.assertEqual(wake_channel(provider), ("SendMessage", ""))

    def test_address_returns_channel_target_and_signal(self):
        register_supervisor_address(
            self.paths, "run-1", {"provider": "claude-code", "session_id": "监工会话", "host": "mac"})
        wake = current_supervisor_address(
            self.paths, "run-1", node_id="n1", role="developer", generation=5)["wake"]
        self.assertEqual(wake["channel"], "SendMessage")
        self.assertEqual(wake["target"], "监工会话")
        self.assertEqual(
            wake["signal"], "vibe-wake run=run-1 node=n1 role=developer generation=5")

    def test_registered_session_name_is_the_wake_target(self):
        # A UUID session id is not addressable by SendMessage; the name is.
        register_supervisor_address(self.paths, "run-1", {
            "provider": "claude-code", "session_id": "60b33567-uuid", "host": "mac",
            "wake_target": " 担任监工 "})
        wake = current_supervisor_address(self.paths, "run-1")["wake"]
        self.assertEqual(wake["target"], "担任监工")

    def _register(self, **platforms):
        (self.root / ".vibe/push-wake-verification.json").write_text(
            json.dumps({"platforms": {"claude-code": platforms}}))

    def test_failure_evidence_alone_does_not_verify(self):
        self._register(failure=[{"evidence_ref": "f"}])
        self.assertTrue(heartbeat_interval_options(self.paths, "claude-code")[1]["forced"])

    def test_success_and_failure_evidence_clear_the_force(self):
        self._register(success=[{"evidence_ref": "s"}], failure=[{"evidence_ref": "f"}])
        entry = heartbeat_interval_options(self.paths, "claude-code")[1]
        self.assertTrue(entry["available"])
        self.assertNotIn("forced", entry)


if __name__ == "__main__":
    unittest.main()
