"""#220: the supervisor beat reports a drifted capability contract early.

The run snapshot records the contract digest it was authorized under.  When
``.vibe/session-contract.json`` is rewritten by something outside the run, the
beat must say so (and point at re-authorization) instead of reading idle/work;
an unreadable contract is unknown, never "consistent".
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import CONTRACT_DRIFT_HINT, contract_drift, supervisor_preflight


class ContractDriftBeatTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".vibe").mkdir()
        self.paths = ProjectPaths(self.root)
        contract = build_contract(self.root, provider="fake", host_id="local")
        save_contract(self.paths, contract)
        self.digest = contract.contract_digest
        self.record = self.root / "rollout.jsonl"
        self.record.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000}}}}) + "\n")

    def _snapshot(self, digest):
        return patch("vibe_guide.supervisor.load_snapshot",
                     return_value=SimpleNamespace(capability_contract_digest=digest))

    def _rewrite_contract(self):
        save_contract(self.paths, build_contract(
            self.root, provider="other", host_id="elsewhere"))

    def test_unchanged_contract_is_not_drift(self):
        with self._snapshot(self.digest):
            self.assertIsNone(contract_drift(self.paths, "run-1"))

    def test_rewritten_contract_gives_the_drift_branch_with_a_hint(self):
        self._rewrite_contract()
        with self._snapshot(self.digest):
            payload = supervisor_preflight(self.paths, "run-1", str(self.record))
        self.assertEqual(payload["state"], "contract_drift")
        self.assertIn("重新授权", payload["action"])
        self.assertEqual(payload["hint"], CONTRACT_DRIFT_HINT)
        self.assertIn("git", payload["hint"])
        self.assertEqual(payload["recorded_digest"], self.digest)
        self.assertNotEqual(payload["current_digest"], self.digest)

    def test_cli_beat_prints_the_drift_branch_and_fails_closed(self):
        from vibe_guide.cli import run_cli

        self._rewrite_contract()
        with self._snapshot(self.digest):
            out = run_cli(["supervisor-beat", "--run-id", "run-1",
                           "--session-record", str(self.record)], self.root)
        text = out if isinstance(out, str) else str(out)
        self.assertIn("contract_drift", text)
        self.assertIn("重新授权", text)
        self.assertNotIn("idle", text.split("contract_drift")[0])

    def test_cached_receipt_cannot_hide_the_drift(self):
        from vibe_guide.cli import run_cli

        self._rewrite_contract()
        cached = {"state": "work", "reason": "cached receipt"}
        with self._snapshot(self.digest), patch(
                "vibe_guide.cli.preflight_with_checkpoint", return_value=cached) as checkpoint:
            out = run_cli(["supervisor-preflight", "--run-id", "run-1",
                           "--session-record", str(self.record)], self.root)
        self.assertIn("contract_drift", str(out))
        checkpoint.assert_not_called()

    def test_unchanged_contract_still_reaches_the_checkpoint(self):
        from vibe_guide.cli import run_cli

        cached = {"state": "work", "reason": "cached receipt"}
        with self._snapshot(self.digest), patch(
                "vibe_guide.cli.preflight_with_checkpoint", return_value=cached) as checkpoint:
            run_cli(["supervisor-preflight", "--run-id", "run-1",
                     "--session-record", str(self.record)], self.root)
        checkpoint.assert_called_once()

    def test_missing_contract_is_unknown_not_consistent(self):
        (self.root / ".vibe/session-contract.json").unlink()
        with self._snapshot(self.digest):
            payload = supervisor_preflight(self.paths, "run-1", str(self.record))
        self.assertEqual(payload["state"], "unknown")
        self.assertIn("unreadable", payload["reason"])

    def test_garbled_contract_is_unknown(self):
        (self.root / ".vibe/session-contract.json").write_text("{not json")
        with self._snapshot(self.digest):
            self.assertEqual(contract_drift(self.paths, "run-1")["state"], "unknown")

    def test_a_run_that_never_recorded_a_digest_is_not_compared(self):
        self._rewrite_contract()
        with self._snapshot(""):
            self.assertIsNone(contract_drift(self.paths, "run-1"))


if __name__ == "__main__":
    unittest.main()
