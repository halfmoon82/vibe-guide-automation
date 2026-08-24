import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.contracts import RunEvent
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import (
    RunSnapshot,
    acquire_writer_lease,
    append_event,
    load_snapshot,
    release_writer_lease,
    save_snapshot,
)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_events_append_with_sequence_without_rewriting_history(self):
        append_event(self.paths, RunEvent("started", {"run_id": "run-1", "node_id": "n1"}))
        event_path = Path(self.temporary.name) / ".vibe/runs/run-1/events.jsonl"
        first_bytes = event_path.read_bytes()

        append_event(self.paths, RunEvent("accepted", {"run_id": "run-1", "node_id": "n1"}))
        records = [json.loads(line) for line in event_path.read_text().splitlines()]

        self.assertTrue(event_path.read_bytes().startswith(first_bytes))
        self.assertEqual([record["sequence"] for record in records], [1, 2])
        self.assertEqual([record["event"] for record in records], ["started", "accepted"])

    def test_atomic_snapshot_ignores_interrupted_temporary_file(self):
        expected = RunSnapshot("run-1", "plan-1", 1, "running", {"n1": {"status": "running"}}, {})
        save_snapshot(self.paths, expected)
        state_dir = Path(self.temporary.name) / ".vibe/runs/run-1"
        (state_dir / ".state.json.interrupted").write_text("{not-json")

        self.assertEqual(load_snapshot(self.paths, "run-1"), expected)

    def test_load_falls_back_to_last_valid_snapshot(self):
        first = RunSnapshot("run-1", "plan-1", 1, "running", {"n1": {"status": "running"}}, {})
        second = RunSnapshot("run-1", "plan-1", 1, "complete", {"n1": {"status": "accepted"}}, {})
        save_snapshot(self.paths, first)
        save_snapshot(self.paths, second)
        state_path = Path(self.temporary.name) / ".vibe/runs/run-1/state.json"
        state_path.write_text("truncated")

        self.assertEqual(load_snapshot(self.paths, "run-1"), first)

    def test_writer_lease_is_exclusive_and_only_owner_can_release(self):
        self.assertTrue(acquire_writer_lease(self.paths, "n1", "worktree-a", "run-1"))
        self.assertFalse(acquire_writer_lease(self.paths, "n1", "worktree-a", "run-2"))
        self.assertFalse(release_writer_lease(self.paths, "n1", "worktree-a", "run-2"))
        self.assertTrue(release_writer_lease(self.paths, "n1", "worktree-a", "run-1"))
        self.assertTrue(acquire_writer_lease(self.paths, "n1", "worktree-a", "run-2"))


if __name__ == "__main__":
    unittest.main()
