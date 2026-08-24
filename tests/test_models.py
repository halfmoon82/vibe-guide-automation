import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths


class ModelsContractTests(unittest.TestCase):
    def _node(self, **overrides):
        values = {
            "id": "n0",
            "title": "Contracts",
            "depends_on": [],
            "integration_after": [],
            "parallel_group": "g1",
            "contract": {"input": "x"},
            "status": "ready",
        }
        values.update(overrides)
        return DAGNode(**values)

    def test_dag_node_constructs_and_round_trips(self):
        node = self._node()
        self.assertEqual(DAGNode.from_dict(node.to_dict()), node)

    def test_dag_lifecycle_and_pause_statuses_are_explicit(self):
        statuses = (
            "planned", "ready", "running", "delivered", "review", "accepted",
            "rework", "blocked_design", "blocked_deploy", "blocked_unknown",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.assertEqual(self._node(status=status).status, status)

    def test_unsupported_dag_status_is_rejected_independently(self):
        with self.assertRaises(ValueError):
            self._node(status="complete")

    def test_dag_contract_is_json_safe(self):
        node = self._node(contract={"path": Path("nested/file"), "items": (1, 2)})
        encoded = json.dumps(node.to_dict())
        self.assertIn("nested/file", encoded)
        self.assertEqual(DAGNode.from_dict(json.loads(encoded)).contract["path"], "nested/file")

    def test_each_dag_identifier_field_is_validated(self):
        for field in ("id", "depends_on", "integration_after", "parallel_group"):
            with self.subTest(field=field):
                values = {field: "bad id"} if field in ("id", "parallel_group") else {field: ["bad id"]}
                with self.assertRaises(ValueError):
                    self._node(**values)

    def test_duplicate_dependency_lists_are_validated_independently(self):
        with self.assertRaises(ValueError):
            self._node(depends_on=["n1", "n1"])
        with self.assertRaises(ValueError):
            self._node(integration_after=["n1", "n1"])

    def test_plan_and_capability_identifiers_and_types_are_validated(self):
        with self.assertRaises(ValueError):
            Plan("bad id", 1, "docs/prd.md", ["n0"], "draft")
        with self.assertRaises(ValueError):
            Plan("p1", 1, "docs/prd.md", ["bad id"], "draft")
        with self.assertRaises(TypeError):
            AgentCapabilities("codex", 1, True, True, False, True, "full")
        with self.assertRaises(ValueError):
            AgentCapabilities("bad id", True, True, True, False, True, "full")

    def test_plan_serializes_as_json(self):
        plan = Plan("p1", 1, "docs/prd.md", ["n0"], "draft")
        restored = Plan.from_dict(json.loads(json.dumps(plan.to_dict())))
        self.assertEqual(restored, plan)

    def test_project_root_uses_marker_from_nested_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("vibe\n", encoding="utf-8")
            nested = root / "one" / "two"
            nested.mkdir(parents=True)
            paths = ProjectPaths.from_cwd(nested)
            self.assertEqual(paths.root, root.resolve())

    def test_project_paths_reject_traversal_and_expose_contained_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").touch()
            paths = ProjectPaths.from_cwd(root)
            self.assertEqual(paths.vibe_dir, (root / ".vibe").resolve())
            with self.assertRaises(ValueError):
                paths.resolve_relative("../outside")
            with self.assertRaises(ValueError):
                paths.resolve_vibe_path("../../outside")

    def test_git_root_takes_precedence_and_inputs_are_canonicalized(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            (root / ".project-root").touch()
            nested = root / "one"
            nested.mkdir()
            file_input = nested / "sample.txt"
            file_input.write_text("x", encoding="utf-8")
            link = root.parent / (root.name + "-link")
            try:
                link.symlink_to(root, target_is_directory=True)
                self.assertEqual(ProjectPaths.from_cwd(link / "one").root, root.resolve())
                self.assertEqual(ProjectPaths.from_cwd(file_input).root, root.resolve())
                self.assertEqual(ProjectPaths.from_cwd(root / "missing" / "leaf").root, root.resolve())
            finally:
                if link.is_symlink():
                    link.unlink()

    def test_vibe_home_is_canonical_and_symlink_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as cache:
            root = Path(directory)
            (root / ".project-root").touch()
            with mock.patch.dict(os.environ, {"VIBE_HOME": cache}):
                paths = ProjectPaths.from_cwd(root)
            self.assertEqual(paths.vibe_home, Path(cache).resolve())
            outside = root.parent / (root.name + "-outside")
            outside.mkdir()
            try:
                (root / ".vibe").symlink_to(outside, target_is_directory=True)
                with self.assertRaises(ValueError):
                    paths.resolve_vibe_path("state.json")
            finally:
                if (root / ".vibe").is_symlink():
                    (root / ".vibe").unlink()
                outside.rmdir()

    def test_invalid_node_and_status_are_rejected(self):
        with self.assertRaises(ValueError):
            self._node(id="bad id")
