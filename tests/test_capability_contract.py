import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vibe_guide.capability_contract import (
    CAPABILITY_STATUSES,
    CapabilityFact,
    build_contract,
    capability_status,
    contract_path,
    load_contract,
    save_contract,
)
from vibe_guide.paths import ProjectPaths


class CapabilityContractTests(unittest.TestCase):
    def test_build_round_trip_recomputes_stable_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root)
            now = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
            contract = build_contract(
                root,
                provider="codex",
                host_id="host-a",
                facts={
                    "runtime.exec": {
                        "status": "verified_available",
                        "scope": "init",
                        "route": "runtime.exec",
                        "evidence_ref": "probe-1",
                    }
                },
                now=now,
            )
            self.assertIn("verified_available", CAPABILITY_STATUSES)
            self.assertNotEqual(contract.contract_digest, "")
            path = save_contract(paths, contract)
            loaded = load_contract(paths, now=now)
            self.assertEqual(path, contract_path(paths))
            self.assertEqual(loaded, contract)
            self.assertEqual(loaded.contract_digest, contract.contract_digest)

    def test_missing_and_unknown_timeout_never_become_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            contract = build_contract(
                root,
                facts={
                    "terminal.exec": {
                        "status": "unknown_timeout",
                        "scope": "task",
                        "route": "",
                        "evidence_ref": "probe-timeout",
                    }
                },
            )
            self.assertEqual(capability_status(contract, "missing"), "unknown")
            self.assertEqual(
                capability_status(contract, "terminal.exec"),
                "unknown_timeout",
            )
            self.assertNotEqual(
                capability_status(contract, "terminal.exec"),
                "unavailable",
            )

    def test_expired_fact_is_stale_but_invalid_status_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            now = datetime(2026, 8, 26, 10, 0, tzinfo=timezone.utc)
            expired = (now - timedelta(minutes=1)).isoformat()
            contract = build_contract(
                root,
                facts={
                    "browser.control": {
                        "status": "verified_available",
                        "scope": "task",
                        "route": "browser.control",
                        "evidence_ref": "probe-expired",
                        "expires_at": expired,
                    }
                },
                now=now,
            )
            self.assertEqual(
                capability_status(contract, "browser.control", now=now),
                "stale",
            )
            with self.assertRaises(ValueError):
                CapabilityFact(
                    "terminal.exec",
                    "unavailable",
                    "task",
                    "",
                    "probe",
                    now.isoformat(),
                    (now + timedelta(hours=1)).isoformat(),
                )

    def test_malformed_or_symlinked_contract_is_rejected_without_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "project"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            paths = ProjectPaths(root)
            contract = build_contract(root)
            save_contract(paths, contract)
            target = contract_path(paths)
            target.write_text("{bad\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_contract(paths)

            target.unlink()
            target.symlink_to(outside / "contract.json")
            with self.assertRaises(ValueError):
                load_contract(paths)
            with self.assertRaises(ValueError):
                save_contract(paths, contract)
            self.assertEqual(list(outside.iterdir()), [])

    def test_save_is_atomic_and_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root)
            contract = build_contract(root)
            first = save_contract(paths, contract)
            first_bytes = first.read_bytes()
            second = save_contract(paths, contract)
            self.assertEqual(first, second)
            self.assertEqual(first_bytes, second.read_bytes())
            decoded = json.loads(first.read_text(encoding="utf-8"))
            self.assertEqual(decoded["contract_digest"], contract.contract_digest)
            self.assertFalse(any(path.name.startswith(".session-contract") for path in first.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
