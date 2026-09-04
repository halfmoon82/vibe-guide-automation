import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.migration import migrate_v2_to_v310, restore_backup


class MigrationTests(unittest.TestCase):
    def fixture(self, root):
        source = root / "v2"
        (source / ".vibe" / "runs" / "run-1").mkdir(parents=True)
        (source / ".vibe" / "E2E_MAILBOX" / "pending").mkdir(parents=True)
        (source / ".vibe" / "config.json").write_text(
            json.dumps({"version": "2.0.0", "custom": {"keep": True}}) + "\n",
            encoding="utf-8",
        )
        (source / ".vibe" / "state.json").write_text(
            json.dumps({"workflow_version": 2, "session_gate": "s0_required", "unknown": "retain"}) + "\n",
            encoding="utf-8",
        )
        (source / ".vibe" / "runs" / "run-1" / "evidence.json").write_text("history\n", encoding="utf-8")
        (source / ".vibe" / "E2E_MAILBOX" / "pending" / "secret.json").write_text("exclude\n", encoding="utf-8")
        return source

    def test_v2_fixture_migrates_and_preserves_source_and_unknown_fields(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); destination = root / "v310"
            result = migrate_v2_to_v310(source, destination)
            self.assertEqual(result.status, "migrated")
            self.assertEqual(result.to_dict()["target_version"], "4.0.0")
            self.assertEqual(json.loads((destination / ".vibe" / "state.json").read_text())["unknown"], "retain")
            self.assertTrue((destination / ".vibe" / "runs" / "run-1" / "evidence.json").exists())
            self.assertFalse((destination / ".vibe" / "E2E_MAILBOX").exists())
            self.assertTrue((source / ".vibe" / "E2E_MAILBOX" / "pending" / "secret.json").exists())

    def test_repeated_migration_is_idempotent_and_backup_is_restorable(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); destination = root / "v310"
            first = migrate_v2_to_v310(source, destination)
            digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
            before = digest(destination / ".vibe" / "state.json")
            second = migrate_v2_to_v310(source, destination)
            self.assertEqual(second.status, "already_current")
            self.assertTrue(second.idempotent)
            self.assertEqual(before, digest(destination / ".vibe" / "state.json"))
            restored = root / "restored"
            restore_backup(first.backup_path, restored)
            self.assertEqual((source / ".vibe" / "config.json").read_bytes(), (restored / ".vibe" / "config.json").read_bytes())

    def test_invalid_input_returns_structured_failure_without_destination_write(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); destination = root / "v310"
            result = migrate_v2_to_v310(root / "missing", destination)
            self.assertEqual(result.status, "blocked_invalid")
            self.assertIsInstance(result.to_dict(), dict)
            self.assertFalse(destination.exists())

    def test_non_path_inputs_are_structured_invalid_results(self):
        result = migrate_v2_to_v310(None, object())
        self.assertEqual(result.status, "blocked_invalid")

    def test_forged_marker_does_not_claim_already_current(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); destination = root / "v310"
            (destination / ".vibe").mkdir(parents=True)
            (destination / ".vibe" / "migration-result.json").write_text(
                json.dumps({"status": "migrated", "target_version": "3.10.0"}), encoding="utf-8"
            )
            result = migrate_v2_to_v310(source, destination)
            self.assertNotEqual(result.status, "already_current")

    def test_restore_rejects_corrupt_backup_with_structured_result(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); destination = root / "v310"
            result = migrate_v2_to_v310(source, destination)
            payload_file = Path(result.backup_path) / "payload" / ".vibe" / "config.json"
            payload_file.write_text("tampered", encoding="utf-8")
            restored = restore_backup(result.backup_path, root / "restored")
            self.assertEqual(restored.status, "blocked_invalid")

    def test_non_v2_version_is_rejected_before_migration(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root)
            (source / ".vibe" / "config.json").write_text(json.dumps({"version": "1.0.0"}), encoding="utf-8")
            result = migrate_v2_to_v310(source, root / "v310")
            self.assertEqual(result.status, "blocked_invalid")

    def test_marker_must_match_current_destination_and_backup_contents(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); destination = root / "v310"
            first = migrate_v2_to_v310(source, destination)
            (destination / ".vibe" / "config.json").unlink()
            second = migrate_v2_to_v310(source, destination)
            self.assertNotEqual(second.status, "already_current")

    def test_corrupt_reused_backup_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); first = migrate_v2_to_v310(source, root / "one")
            payload = Path(first.backup_path) / "payload" / ".vibe" / "config.json"
            payload.write_text("corrupt", encoding="utf-8")
            second = migrate_v2_to_v310(source, root / "two")
            self.assertEqual(second.status, "migrated")
            self.assertEqual(payload.read_bytes(), (source / ".vibe" / "config.json").read_bytes())

    def test_restore_rejects_payload_symlink(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); result = migrate_v2_to_v310(source, root / "v310")
            payload = Path(result.backup_path) / "payload" / ".vibe" / "config.json"
            payload.unlink(); payload.symlink_to(source / ".vibe" / "config.json")
            restored = restore_backup(result.backup_path, root / "restored")
            self.assertEqual(restored.status, "blocked_invalid")

    def test_malformed_manifest_is_structured_and_rebuilt(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); first = migrate_v2_to_v310(source, root / "one")
            Path(first.backup_path, "manifest.json").write_text("[]", encoding="utf-8")
            result = migrate_v2_to_v310(source, root / "two")
            self.assertEqual(result.status, "migrated")

    def test_malformed_marker_files_entries_do_not_short_circuit(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); destination = root / "v310"
            first = migrate_v2_to_v310(source, destination)
            marker = destination / ".vibe" / "migration-result.json"
            value = json.loads(marker.read_text()); value["backup_manifest"]["files"] = ["bad"]
            marker.write_text(json.dumps(value), encoding="utf-8")
            result = migrate_v2_to_v310(source, destination)
            self.assertNotEqual(result.status, "already_current")

    def test_restore_rejects_unlisted_payload_object(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); source = self.fixture(root); result = migrate_v2_to_v310(source, root / "v310")
            extra = Path(result.backup_path) / "payload" / "unlisted"
            extra.symlink_to(source)
            restored = restore_backup(result.backup_path, root / "restored")
            self.assertEqual(restored.status, "blocked_invalid")


if __name__ == "__main__":
    unittest.main()
