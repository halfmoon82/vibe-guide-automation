import tempfile
import unittest
from pathlib import Path

from vibe_guide.paths import ProjectPaths
from vibe_guide.task_registry import (
    TaskBinding,
    load_task_binding,
    save_task_binding,
)


class TaskRegistryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def binding(self, role="developer", task_id="thread-dev"):
        return TaskBinding(
            provider="codex",
            mode="visible",
            issue_id="N3",
            role=role,
            task_id=task_id,
            host="local",
            worktree=".worktrees/n3",
            branch="codex/n3",
            status_file="status.txt",
            handoff_file="handoff.md",
            cursor="cursor-1",
            token="cursor-token",
            threadId=task_id,
            hostId="local",
            run_id="run-1",
        )

    def test_round_trip_preserves_generic_and_codex_identity(self):
        original = self.binding()
        save_task_binding(self.paths, original)

        loaded = load_task_binding(self.paths, "N3", "developer")

        self.assertEqual(loaded, original)
        self.assertEqual(loaded.threadId, "thread-dev")
        self.assertEqual(loaded.hostId, "local")
        self.assertEqual(loaded.cursor, "cursor-1")

    def test_duplicate_writer_is_rejected_but_same_task_can_continue(self):
        save_task_binding(self.paths, self.binding())
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, self.binding(task_id="thread-other"))

        continued = self.binding()
        continued.cursor = "cursor-2"
        save_task_binding(self.paths, continued)
        self.assertEqual(load_task_binding(self.paths, "N3", "developer").cursor, "cursor-2")

    def test_developer_and_reviewer_must_be_distinct_tasks(self):
        save_task_binding(self.paths, self.binding())
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, self.binding(role="reviewer", task_id="thread-dev"))
        save_task_binding(self.paths, self.binding(role="reviewer", task_id="thread-review"))
        self.assertEqual(
            load_task_binding(self.paths, "N3", "reviewer").task_id,
            "thread-review",
        )


if __name__ == "__main__":
    unittest.main()
