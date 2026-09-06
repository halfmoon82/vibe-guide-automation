import tempfile
import unittest
from pathlib import Path

from vibe_guide.manifest import RunManifest, save_run_manifest
from vibe_guide.paths import ProjectPaths


class V38RecoveryTests(unittest.TestCase):
    def test_manifest_rejects_foreign_epoch_and_invalid_sha(self):
        with self.assertRaises(ValueError):
            RunManifest.from_mapping({
                "plan_id": "plan", "plan_revision": 1, "run_id": "run",
                "base_sha": "not-a-sha", "target_branch": "codex/v38",
                "execution_epoch": 0, "authorization_digest": "",
                "evidence_ref": "events.jsonl",
            })

    def test_save_does_not_follow_manifest_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            run_dir = Path(directory) / ".vibe" / "runs" / "run"
            run_dir.mkdir(parents=True)
            target = Path(directory) / "outside.json"
            target.write_text("sentinel", encoding="utf-8")
            (run_dir / "run-manifest.json").symlink_to(target)
            manifest = RunManifest.from_mapping({
                "plan_id": "plan", "plan_revision": 1, "run_id": "run",
                "base_sha": "a" * 40, "target_branch": "codex/v38",
                "execution_epoch": 0, "authorization_digest": "",
                "evidence_ref": "events.jsonl",
            })
            with self.assertRaises(ValueError):
                save_run_manifest(paths, manifest)
            self.assertEqual(target.read_text(encoding="utf-8"), "sentinel")


if __name__ == "__main__":
    unittest.main()
