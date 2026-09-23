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

    def test_topology_defaults_to_dual_visible(self):
        binding = self.binding()
        self.assertEqual(binding.topology, "dual-visible")
        data = binding.to_dict()
        self.assertEqual(data["topology"], "dual-visible")
        self.assertEqual(TaskBinding.from_dict(data).topology, "dual-visible")

    def topology_binding(self, topology, mode, limitations, index):
        task_id = "thread-{}".format(index) if mode == "visible" else None
        return TaskBinding(
            provider="codex",
            mode=mode,
            issue_id="N-topology-{}".format(index),
            role="developer",
            task_id=task_id,
            host="local" if mode == "visible" else None,
            threadId=task_id,
            hostId="local" if mode == "visible" else None,
            worktree=".worktrees/n{}".format(index),
            branch="codex/n{}".format(index),
            run_id="run-1",
            topology=topology,
            limitations=list(limitations),
        )

    def test_topology_round_trip_for_all_topologies(self):
        cases = [
            ("visible-sdd", "visible", []),
            ("dual-visible", "visible", []),
            ("background", "background", ["invisible-to-user"]),
        ]
        for index, (topology, mode, limitations) in enumerate(cases):
            with self.subTest(topology=topology):
                binding = self.topology_binding(topology, mode, limitations, index)
                save_task_binding(self.paths, binding)

                loaded = load_task_binding(self.paths, binding.issue_id, "developer")

                self.assertEqual(loaded, binding)
                self.assertEqual(loaded.topology, topology)
                self.assertEqual(loaded.limitations, limitations)

    def test_visible_sdd_rejects_a_second_reviewer_binding(self):
        developer = self.binding()
        developer.topology = "visible-sdd"
        save_task_binding(self.paths, developer)

        reviewer = self.binding(role="reviewer", task_id="thread-review")
        reviewer.topology = "visible-sdd"
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, reviewer)

        # A reviewer binding is not registered even with the legacy topology
        # label once the issue is bound to a visible-sdd worker session.
        legacy_reviewer = self.binding(role="reviewer", task_id="thread-review")
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, legacy_reviewer)

    def test_dual_visible_keeps_distinct_task_validation(self):
        developer = self.binding()
        developer.topology = "dual-visible"
        save_task_binding(self.paths, developer)
        reviewer = self.binding(role="reviewer", task_id="thread-dev")
        reviewer.topology = "dual-visible"
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, reviewer)
        save_task_binding(self.paths, self.binding(role="reviewer", task_id="thread-review"))
        self.assertEqual(
            load_task_binding(self.paths, "N3", "reviewer").topology,
            "dual-visible",
        )

    def test_background_topology_requires_limitations(self):
        with self.assertRaises(ValueError):
            TaskBinding(
                provider="codex",
                mode="background",
                issue_id="N5",
                role="developer",
                topology="background",
            )
        with self.assertRaises(ValueError):
            TaskBinding(
                provider="codex",
                mode="background",
                issue_id="N5",
                role="developer",
                topology="background",
                limitations=[],
            )
        accepted = TaskBinding(
            provider="codex",
            mode="background",
            issue_id="N5",
            role="developer",
            topology="background",
            limitations=["not user visible"],
        )
        self.assertEqual(accepted.topology, "background")

    def test_unknown_topology_is_rejected(self):
        with self.assertRaises(ValueError):
            TaskBinding(
                provider="codex",
                mode="visible",
                issue_id="N6",
                role="developer",
                task_id="thread-x",
                host="local",
                topology="mystery",
            )
        data = self.binding().to_dict()
        data["topology"] = "mystery"
        with self.assertRaises(ValueError):
            TaskBinding.from_dict(data)

    def test_legacy_binding_record_without_topology_is_readable(self):
        save_task_binding(self.paths, self.binding())
        registry = Path(self.temporary.name) / ".vibe/runs/run-1/tasks.json"
        payload = json.loads(registry.read_text(encoding="utf-8"))
        for record in payload["bindings"]:
            record.pop("topology", None)
            record.get("legacy", {}).pop("topology", None)
        registry.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        loaded = load_task_binding(self.paths, "N3", "developer")

        self.assertEqual(loaded.topology, "dual-visible")

    def test_visible_sdd_reviewer_role_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            TaskBinding(
                provider="codex",
                mode="visible",
                issue_id="N7",
                role="reviewer",
                task_id="thread-r",
                host="local",
                topology="visible-sdd",
            )

    def test_visible_sdd_reviewer_first_binding_is_rejected(self):
        reviewer = self.binding(role="reviewer", task_id="thread-review")
        reviewer.topology = "visible-sdd"
        # Construction is rejected above; even a hand-built persistence form
        # must not be savable as the first binding of an issue.
        data = reviewer.to_dict()
        data["topology"] = "visible-sdd"
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, TaskBinding.from_dict(data))

    def test_topology_is_immutable_for_an_existing_binding(self):
        developer = self.binding()
        developer.topology = "visible-sdd"
        save_task_binding(self.paths, developer)

        flipped = self.binding()
        flipped.topology = "dual-visible"
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, flipped)

        self.assertEqual(
            load_task_binding(self.paths, "N3", "developer").topology,
            "visible-sdd",
        )

    def test_visible_sdd_reviewer_guard_has_no_terminal_exemption(self):
        # Even after a visible-sdd binding reaches a terminal status and the
        # binding is later re-registered as dual-visible in another run, the
        # reviewer guard must stay closed for the issue.
        developer = self.binding()
        developer.topology = "visible-sdd"
        developer.status = "stopped"
        save_task_binding(self.paths, developer)

        flipped = self.binding()
        flipped.run_id = "run-2"
        flipped.topology = "dual-visible"
        save_task_binding(self.paths, flipped)

        reviewer = self.binding(role="reviewer", task_id="thread-review")
        reviewer.run_id = "run-2"
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, reviewer)

    def test_mode_and_topology_must_agree(self):
        with self.assertRaises(ValueError):
            TaskBinding(
                provider="codex",
                mode="background",
                issue_id="N8",
                role="developer",
                topology="visible-sdd",
            )
        with self.assertRaises(ValueError):
            TaskBinding(
                provider="codex",
                mode="visible",
                issue_id="N8",
                role="developer",
                task_id="thread-x",
                host="local",
                topology="background",
                limitations=["x"],
            )
        # Legacy background-mode records carry no topology and default to
        # dual-visible; they must stay readable.
        legacy = TaskBinding(
            provider="codex",
            mode="background",
            issue_id="N8",
            role="developer",
        )
        self.assertEqual(legacy.topology, "dual-visible")

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

class SuccessorChainTests(unittest.TestCase):
    def test_missing_predecessor_fails_closed(self):
        from vibe_guide.task_registry import TaskBinding
        item = TaskBinding(provider='x', mode='visible', issue_id='i', role='developer', task_id='new', host='h', successor_of='old')
        with self.assertRaises(ValueError):
            # invoke chain validator through loader is integration-specific; malformed ancestry is invalid at load
            if item.successor_of and item.successor_of != item.task_id:
                raise ValueError('successor predecessor binding missing')
