import tempfile
import unittest
from pathlib import Path
import os

from vibe_guide.models import DAGNode, assert_supervisor_boundary, validate_topology
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding


def make_node(node_id, *, writer="developer", reviewer="reviewer", depends=None,
              files=None, status="ready", provider="codex-app-visible",
              environment="worktree", supervisor="supervisor"):
    files = files or [node_id + ".py"]
    contract = {
        "provider": provider,
        "adapter_id": "codex",
        "environment": environment,
        "writer": writer,
        "reviewer": reviewer,
        "supervisor": supervisor,
        "host": "host-1",
        "parent_run_id": "run-parent",
        "worktree": ".vibe/worktrees/" + node_id,
        "branch": "codex/" + node_id,
        "allowlist": files,
        "input": ["input"],
        "output": ["output"],
        "error_behavior": ["error"],
        "acceptance_examples": ["accepted"],
    }
    return DAGNode(node_id, node_id, depends or [], [], "topology", contract, status)


class V3TopologyTests(unittest.TestCase):
    def test_complex_requires_independent_supervisor_writer_and_reviewer(self):
        result = validate_topology([make_node("a", writer="supervisor")], complexity="complex")
        self.assertEqual(result.status, "governance_pending")
        self.assertTrue(any("supervisor" in reason for reason in result.reasons))

    def test_duplicate_writer_and_missing_reviewer_are_governance_pending(self):
        nodes = [make_node("a", writer="same"), make_node("b", writer="same", reviewer="")]
        result = validate_topology(nodes, complexity="complex")
        self.assertEqual(result.status, "governance_pending")
        self.assertTrue(any("writer" in reason for reason in result.reasons))
        self.assertTrue(any("reviewer" in reason for reason in result.reasons))

    def test_simple_and_light_plan_bypass_complex_topology(self):
        node = make_node("a", writer="supervisor", reviewer="")
        self.assertEqual(validate_topology([node], complexity="simple").status, "bypassed")
        self.assertEqual(validate_topology([node], complexity="light_plan").status, "bypassed")

    def test_disjoint_hard_dependency_without_reason_is_unjustified_serialization(self):
        nodes = [make_node("a", files=["a.py"], status="accepted"),
                 make_node("b", depends=["a"], files=["b.py"])]
        result = validate_topology(nodes, complexity="complex")
        self.assertEqual(result.status, "governance_pending")
        self.assertTrue(any("serialization" in reason for reason in result.reasons))

    def test_designated_writer_cannot_invoke_monitor(self):
        node = make_node("a", writer="writer-a")
        with self.assertRaises(PermissionError):
            assert_supervisor_boundary("writer-a", [node])
        with self.assertRaises(PermissionError):
            assert_supervisor_boundary("random", [node])
        self.assertTrue(assert_supervisor_boundary("supervisor", [node]))

    def test_adapter_host_and_parent_binding_are_required(self):
        for field, value in (("adapter_id", "wrong"), ("host", ""), ("parent_run_id", "foreign")):
            candidate = make_node("a")
            candidate.contract[field] = value
            result = validate_topology([candidate], complexity="complex")
            self.assertEqual(result.status, "governance_pending", field)

    def test_task_registry_rejects_run_directory_symlink(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            class Paths:
                vibe = Path(root) / ".vibe"
            Paths.vibe.joinpath("runs").mkdir(parents=True)
            Paths.vibe.joinpath("runs", "run-link").symlink_to(outside, target_is_directory=True)
            binding = TaskBinding(
                provider="runner", mode="background", issue_id="n1", role="developer",
                task_id="task-1", run_id="run-link", worktree=".worktrees/n1", branch="branch-n1",
            )
            with self.assertRaises(ValueError):
                save_task_binding(Paths(), binding)

    def test_task_registry_rejects_vibe_and_lock_symlinks_before_open(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            class Paths:
                vibe = Path(root) / ".vibe"
            Paths.vibe.symlink_to(outside, target_is_directory=True)
            binding = TaskBinding(provider="runner", mode="background", issue_id="n1",
                                  role="developer", task_id="task-1", run_id="run-1",
                                  worktree=".worktrees/n1", branch="branch-n1")
            with self.assertRaises(ValueError):
                save_task_binding(Paths(), binding)
            self.assertFalse((Path(outside) / ".task-registry.lock").exists())

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            class Paths:
                vibe = Path(root) / ".vibe"
            Paths.vibe.mkdir()
            (Paths.vibe / ".task-registry.lock").symlink_to(Path(outside) / "lock")
            binding = TaskBinding(provider="runner", mode="background", issue_id="n1",
                                  role="developer", task_id="task-1", run_id="run-1",
                                  worktree=".worktrees/n1", branch="branch-n1")
            with self.assertRaises(ValueError):
                save_task_binding(Paths(), binding)
            self.assertFalse((Path(outside) / "lock").exists())

    def test_registry_accepts_pending_visible_client_thread_schema(self):
        binding = TaskBinding(provider="codex-app-visible", mode="visible", issue_id="n1",
                              role="developer", client_thread_id="client-1", run_id="run-1",
                              worktree=".worktrees/n1", branch="branch-n1")
        restored = TaskBinding.from_dict(binding.to_dict())
        self.assertEqual(restored.client_thread_id, "client-1")

    def test_registry_rejects_ancestor_symlink_without_external_side_effect(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            root_path = Path(root)
            (root_path / "alias").symlink_to(outside, target_is_directory=True)
            class Paths:
                root = root_path
                vibe = root_path / "alias" / ".vibe"
            binding = TaskBinding(provider="runner", mode="background", issue_id="n1",
                                  role="developer", task_id="task-1", run_id="run-1",
                                  worktree=".worktrees/n1", branch="branch-n1")
            with self.assertRaises(ValueError):
                save_task_binding(Paths(), binding)
            self.assertFalse((Path(outside) / ".vibe").exists())
            self.assertFalse((Path(outside) / ".task-registry.lock").exists())

    def test_registry_rejects_binding_schema_and_foreign_run_provenance(self):
        with tempfile.TemporaryDirectory() as root:
            class Paths:
                vibe = Path(root) / ".vibe"
            destination = Paths.vibe / "runs" / "run-1" / "tasks.json"
            destination.parent.mkdir(parents=True)
            value = TaskBinding(provider="runner", mode="background", issue_id="n1",
                                role="developer", task_id="task-1", run_id="run-1",
                                worktree=".worktrees/n1", branch="branch-n1").to_dict()
            value["schema_version"] = 999
            value["run_id"] = "run-other"
            import json
            destination.write_text(json.dumps({"schema_version": 1, "revision": 1,
                                               "bindings": [value]}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_task_binding(Paths(), "n1", "developer", run_id="run-1")


if __name__ == "__main__":
    unittest.main()
