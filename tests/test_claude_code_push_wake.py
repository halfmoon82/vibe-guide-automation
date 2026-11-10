"""Claude Code registers SendMessage as its push wake-up channel.

Registration is not verification: the 30-minute tier stays a forced choice
until the project's push-wake-verification file holds both a success and a
failure evidence for claude-code.
"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import main
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import (
    current_supervisor_address,
    HEARTBEAT_INTERVAL_DEFAULT_MINUTES,
    HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES,
    heartbeat_interval,
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
        self.assertTrue(heartbeat_interval_options(self.paths, "claude-code")[1]["requires_cost_acceptance"])

    def test_success_and_failure_evidence_clear_the_force(self):
        self._register(success=[{"evidence_ref": "s"}], failure=[{"evidence_ref": "f"}])
        entry = heartbeat_interval_options(self.paths, "claude-code")[1]
        self.assertTrue(entry["available"])
        self.assertNotIn("requires_cost_acceptance", entry)

class UnreachableWakeTargetTests(ClaudeCodePushWakeTests):
    """run-101f5f86: a UUID target made every worker wake-up fail silently."""
    UUID = "60b33567-1c2d-4e5f-8a9b-0c1d2e3f4a5b"

    def _verified(self):
        self._register(success=[{"evidence_ref": "s"}], failure=[{"evidence_ref": "f"}])

    def test_uuid_target_is_not_offered_as_wake(self):
        register_supervisor_address(self.paths, "run-1", {
            "provider": "claude-code", "session_id": self.UUID, "host": "mac"})
        wake = current_supervisor_address(self.paths, "run-1")["wake"]
        self.assertIsNone(wake["channel"])
        self.assertIn("目标未登记会话名", wake["reason"])
        self.assertNotIn("target", wake)

    def test_heartbeat_stays_short_when_target_unreachable(self):
        self._verified()
        register_supervisor_address(self.paths, "run-1", {
            "provider": "claude-code", "session_id": self.UUID, "host": "mac"})
        got = heartbeat_interval(self.paths, "run-1", decision_only=False)
        self.assertEqual(got["minutes"], HEARTBEAT_INTERVAL_DEFAULT_MINUTES)
        self.assertIn("目标未登记会话名", got["reason"])

    def test_heartbeat_relaxes_with_named_target(self):
        self._verified()
        register_supervisor_address(self.paths, "run-1", {
            "provider": "claude-code", "session_id": self.UUID, "host": "mac",
            "wake_target": "担任监工"})
        got = heartbeat_interval(self.paths, "run-1", decision_only=False)
        self.assertEqual(got["minutes"], HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES)

    def test_register_cli_warns_without_session_name(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["supervisor-register", "--run-id", "run-1", "--provider", "claude-code",
                  "--session-id", self.UUID, "--host", "mac",
                  "--project-root", str(self.root), "--json"])
        self.assertIn("wake_target_warning", out.getvalue())
        self.assertIn("目标未登记会话名", out.getvalue())


if __name__ == "__main__":
    unittest.main()
