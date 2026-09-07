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
from vibe_guide.adapters.task_provider import ProviderActionStore, ProviderUnavailable
from vibe_guide.models import WorkerProfile
from vibe_guide.paths import ProjectPaths


class CapabilityContractTests(unittest.TestCase):
    @staticmethod
    def _child_binding(contract_digest):
        profile = WorkerProfile(
            "codex",
            "gpt-5.6-sol",
            "medium",
            [],
            {
                "issue_complexity_ref": "n1",
                "complexity_band": "standard",
                "risk_tags": [],
                "availability_evidence": "test",
            },
            worktree=".worktrees/n1",
            branch="codex/n1",
            writer="developer",
            allowlist=["vibe_guide/n1.py"],
        )
        return {
            "parent_run_id": "run-1",
            "plan_revision": "1",
            "authorization_digest": "a" * 64,
            "node_id": "n1",
            "role": "developer",
            "writer": profile.writer,
            "worktree": profile.worktree,
            "branch": profile.branch,
            "allowlist": profile.allowlist,
            "worker_profile": profile.to_dict(),
            "capability_contract_digest": contract_digest,
        }

    def test_worker_dispatch_without_contract_is_unknown_not_provider_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "state.json").write_text(
                '{"workflow_version":2,"session_gate":"s0_required"}\n',
                encoding="utf-8",
            )
            request = {
                "origin": "worker_dispatch",
                "child_binding": self._child_binding("b" * 64),
            }
            with self.assertRaisesRegex(ProviderUnavailable, "session_gate_blocked"):
                ProviderActionStore(paths).request(
                    operation="create",
                    provider="codex-app-visible",
                    run_id="run-1",
                    issue_id="n1",
                    role="developer",
                    generation=1,
                    native_tool="codex_app__create_thread",
                    request=request,
                )

    def test_worker_dispatch_must_bind_current_contract_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "state.json").write_text(
                '{"workflow_version":2,"session_gate":"s0_required"}\n',
                encoding="utf-8",
            )
            contract = build_contract(root, provider="codex-app-visible", host_id="local")
            save_contract(paths, contract)
            request = {
                "origin": "worker_dispatch",
                "child_binding": self._child_binding("c" * 64),
            }
            with self.assertRaisesRegex(ProviderUnavailable, "session_gate_blocked"):
                ProviderActionStore(paths).request(
                    operation="create",
                    provider="codex-app-visible",
                    run_id="run-1",
                    issue_id="n1",
                    role="developer",
                    generation=1,
                    native_tool="codex_app__create_thread",
                    request=request,
                )
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
