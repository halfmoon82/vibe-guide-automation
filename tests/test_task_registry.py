import json
import multiprocessing
import os
import tempfile
import unittest
import uuid
from pathlib import Path

from vibe_guide.paths import ProjectPaths
from vibe_guide.task_registry import (
    TaskBinding,
    load_task_binding,
    save_task_binding,
)


def _save_process_binding(root, index):
    save_task_binding(
        ProjectPaths(Path(root)),
        TaskBinding(
            provider="codex",
            mode="visible",
            issue_id="N{}".format(index),
            role="developer",
            task_id="thread-{}".format(index),
            host="local",
            worktree=".worktrees/n{}".format(index),
            branch="codex/n{}".format(index),
            run_id="run-process",
        ),
    )


def _leave_dead_registry_lock(root):
    vibe = Path(root) / ".vibe"
    vibe.mkdir(parents=True, exist_ok=True)
    lock_path = vibe / ".task-registry.lock"
    lock_path.write_text(
        json.dumps({"pid": os.getpid(), "owner": "dead-owner"}),
        encoding="utf-8",
    )
    os._exit(0)


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

    def test_composite_identity_and_aliases_are_immutable(self):
        original = self.binding()
        save_task_binding(self.paths, original)

        mutations = {
            "provider": "other-provider",
            "host": "other-host",
            "mode": "background",
            "worktree": ".worktrees/other",
            "branch": "codex/other",
        }
        for field, value in mutations.items():
            with self.subTest(field=field):
                changed = self.binding()
                setattr(changed, field, value)
                with self.assertRaises(ValueError):
                    save_task_binding(self.paths, changed)

        with self.assertRaises(ValueError):
            TaskBinding(
                provider="codex",
                mode="visible",
                issue_id="N4",
                role="developer",
                task_id="task-one",
                platform_task_id="task-two",
                threadId="thread-three",
                host="local",
                hostId="other-host",
                worktree=".worktrees/n4",
                branch="codex/n4",
                run_id="run-1",
            )

    def test_malformed_or_unversioned_registry_is_not_overwritten(self):
        registry = Path(self.temporary.name) / ".vibe/runs/run-1/tasks.json"
        registry.parent.mkdir(parents=True)
        original = b'{"bindings":{}}\n'
        registry.write_bytes(original)

        with self.assertRaises(ValueError):
            save_task_binding(self.paths, self.binding())
        self.assertEqual(registry.read_bytes(), original)

    def test_raw_token_is_never_persisted(self):
        binding = self.binding()
        binding.token = "raw-secret-sentinel"
        save_task_binding(self.paths, binding)
        registry = Path(self.temporary.name) / ".vibe/runs/run-1/tasks.json"

        self.assertNotIn("raw-secret-sentinel", registry.read_text(encoding="utf-8"))
        self.assertIsNone(load_task_binding(self.paths, "N3", "developer").token)
        payload = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], 1)

    def test_run_id_traversal_and_symlink_escape_are_rejected(self):
        escape_name = "d3-escaped-" + uuid.uuid4().hex
        escaped = (
            Path(self.temporary.name) / ".vibe/runs/../../../{}".format(escape_name) / "tasks.json"
        ).resolve()
        traversal = self.binding()
        traversal.run_id = "../../../" + escape_name
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, traversal)
        self.assertFalse(escaped.exists())

        outside = Path(self.temporary.name) / "outside"
        outside.mkdir()
        run_root = Path(self.temporary.name) / ".vibe/runs"
        run_root.mkdir(parents=True, exist_ok=True)
        (run_root / "run-symlink").symlink_to(outside, target_is_directory=True)
        symlinked = self.binding()
        symlinked.run_id = "run-symlink"
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, symlinked)
        self.assertFalse((outside / "tasks.json").exists())

    def test_registry_update_is_multi_process_safe(self):
        context = multiprocessing.get_context("fork")
        processes = [
            context.Process(target=_save_process_binding, args=(self.temporary.name, index))
            for index in range(12)
        ]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)

        registry = Path(self.temporary.name) / ".vibe/runs/run-process/tasks.json"
        payload = json.loads(registry.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["bindings"]), 12)
        self.assertEqual(
            {item["issue_id"] for item in payload["bindings"]},
            {"N{}".format(index) for index in range(12)},
        )

    def test_registry_lock_recovers_after_holder_process_dies(self):
        context = multiprocessing.get_context("fork")
        process = context.Process(
            target=_leave_dead_registry_lock, args=(self.temporary.name,)
        )
        process.start()
        process.join(5)
        self.assertEqual(process.exitcode, 0)
        lock_path = Path(self.temporary.name) / ".vibe/.task-registry.lock"
        self.assertTrue(lock_path.exists())

        save_task_binding(self.paths, self.binding())

        self.assertTrue(lock_path.exists())
        save_task_binding(self.paths, self.binding())

    def test_binding_run_id_must_match_its_parent_registry_directory(self):
        save_task_binding(self.paths, self.binding())
        current = Path(self.temporary.name) / ".vibe/runs/run-1/tasks.json"
        foreign = Path(self.temporary.name) / ".vibe/runs/foreign-run/tasks.json"
        foreign.parent.mkdir(parents=True)
        foreign.write_bytes(current.read_bytes())
        current.unlink()

        with self.assertRaises((FileNotFoundError, ValueError)):
            load_task_binding(
                self.paths, "N3", "developer", run_id="run-1"
            )


if __name__ == "__main__":
    unittest.main()
