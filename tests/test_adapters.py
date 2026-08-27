import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore, _canonical_digest
from vibe_guide.paths import ProjectPaths


def _request_record(**overrides):
    record = {
        "schema_version": 1,
        "action_id": "action-1",
        "operation": "create",
        "provider": "codex-app-visible",
        "run_id": "run-1",
        "issue_id": "V2-8",
        "role": "reviewer",
        "generation": 1,
        "sequence": 0,
        "native_tool": "create_thread",
        "request": {"prompt": "review"},
        "request_digest": "",
    }
    record.update(overrides)
    record["request_digest"] = _canonical_digest(
        {key: value for key, value in record.items() if key != "request_digest"}
    )
    return record


class ProviderActionStoreTests(unittest.TestCase):
    def test_has_request_matches_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            request_dir = paths.vibe / "provider-actions" / "requests"
            request_dir.mkdir(parents=True)
            (request_dir / "action-1.json").write_text(
                json.dumps(_request_record()), encoding="utf-8"
            )

            self.assertTrue(
                ProviderActionStore(paths).has_request(
                    "run-1", "V2-8", "reviewer"
                )
            )

    def test_has_request_rejects_non_matching_identity_and_filters_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            request_dir = paths.vibe / "provider-actions" / "requests"
            request_dir.mkdir(parents=True)
            for record in (
                _request_record(action_id="action-1", operation="create"),
                _request_record(action_id="action-2", operation="resume"),
            ):
                (request_dir / (record["action_id"] + ".json")).write_text(
                    json.dumps(record), encoding="utf-8"
                )

            store = ProviderActionStore(paths)
            self.assertTrue(store.has_request("run-1", "V2-8", "reviewer", "resume"))
            self.assertFalse(store.has_request("run-1", "V2-8", "reviewer", "wait"))
            self.assertFalse(store.has_request("run-1", "other", "reviewer"))

    def test_has_request_absent_mailbox_is_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = ProviderActionStore(paths)

            self.assertFalse(store.has_request("run-1", "V2-8", "reviewer"))
            self.assertFalse((paths.vibe / "provider-actions").exists())

    def test_has_request_fails_closed_on_invalid_record(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            request_dir = paths.vibe / "provider-actions" / "requests"
            request_dir.mkdir(parents=True)
            (request_dir / "action-invalid.json").write_text(
                json.dumps({"run_id": "run-1"}), encoding="utf-8"
            )

            with self.assertRaises(ValueError):
                ProviderActionStore(paths).has_request("run-1", "V2-8", "reviewer")

    def test_has_request_fails_closed_on_malformed_json(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            request_dir = paths.vibe / "provider-actions" / "requests"
            request_dir.mkdir(parents=True)
            (request_dir / "action-invalid.json").write_text("not-json", encoding="utf-8")

            with self.assertRaises(ValueError):
                ProviderActionStore(paths).has_request("run-1", "V2-8", "reviewer")

    def test_has_request_fails_closed_on_symlinked_record(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            request_dir = paths.vibe / "provider-actions" / "requests"
            request_dir.mkdir(parents=True)
            target = Path(directory) / "outside.json"
            target.write_text(json.dumps(_request_record()), encoding="utf-8")
            (request_dir / "action-1.json").symlink_to(target)

            with self.assertRaises(ValueError):
                ProviderActionStore(paths).has_request("run-1", "V2-8", "reviewer")


if __name__ == "__main__":
    unittest.main()
