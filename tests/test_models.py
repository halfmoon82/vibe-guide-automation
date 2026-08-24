import json
import unittest
from pathlib import Path

from vibe_guide.models import DAGNode, Plan
from vibe_guide.paths import ProjectPaths


class ModelsContractTests(unittest.TestCase):
    def test_dag_node_constructs_and_round_trips(self):
        node = DAGNode("n0", "Contracts", [], [], "g1", {"input": "x"}, "ready")
        self.assertEqual(DAGNode.from_dict(node.to_dict()), node)

    def test_plan_serializes_as_json(self):
        plan = Plan("p1", 1, "docs/prd.md", ["n0"], "draft")
        restored = Plan.from_dict(json.loads(json.dumps(plan.to_dict())))
        self.assertEqual(restored, plan)

    def test_project_root_is_current_directory(self):
        paths = ProjectPaths.from_cwd(Path("/tmp/example/project/subdir"))
        self.assertEqual(paths.root, Path("/tmp/example/project/subdir").resolve())

    def test_invalid_node_and_status_are_rejected(self):
        with self.assertRaises(ValueError):
            DAGNode("bad id", "x", [], [], None, {}, "ready")
        with self.assertRaises(ValueError):
            DAGNode("n1", "x", ["n1", "n1"], [], None, {}, "nope")
