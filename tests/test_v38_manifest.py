import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.manifest import (
    RunManifest,
    advance_execution_epoch,
    load_run_manifest,
    save_run_manifest,
)
from vibe_guide.paths import ProjectPaths


class V38ManifestTests(unittest.TestCase):
    def test_round_trip_preserves_current_run_identity(self):
        manifest = RunManifest.from_mapping({
            "plan_id": "vibe-guide-v3.8-spec-issue-dag",
            "plan_revision": 2,
            "run_id": "run-v38-001",
            "base_sha": "a" * 40,
            "target_branch": "codex/v38-integration",
            "execution_epoch": 3,
            "authorization_digest": "b" * 64,
            "evidence_ref": ".vibe/runs/run-v38-001/events.jsonl",
        })
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            save_run_manifest(paths, manifest)
            loaded = load_run_manifest(paths, manifest.run_id)
        self.assertEqual(loaded.to_dict(), manifest.to_dict())
        self.assertEqual(len(manifest.digest()), 64)

    def test_epoch_advance_invalidates_old_authorization_and_keeps_history(self):
        original = RunManifest.from_mapping({
            "plan_id": "plan",
            "plan_revision": 1,
            "run_id": "run",
            "base_sha": "a" * 40,
            "target_branch": "codex/v38",
            "execution_epoch": 0,
            "authorization_digest": "b" * 64,
            "evidence_ref": "events.jsonl",
        })
        advanced = advance_execution_epoch(original, "rebind")
        self.assertEqual(advanced.execution_epoch, 1)
        self.assertEqual(advanced.authorization_digest, "")
        self.assertEqual(advanced.previous_manifest_digest, original.digest())
        self.assertNotEqual(advanced.digest(), original.digest())


if __name__ == "__main__":
    unittest.main()
