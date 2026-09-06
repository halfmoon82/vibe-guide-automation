import unittest

from vibe_guide.models import DAGNode
from vibe_guide.path_ownership import validate_path_ownership


def node(node_id, owned, read=()):
    return DAGNode(node_id, node_id, [], [], "foundation", {
        "input": "request", "output": "result", "error_behavior": "blocked",
        "acceptance_example": "works",
    }, "planned", writer="writer-" + node_id, worktree=".vibe/worktrees/" + node_id,
                   allowlist=list(owned), owned_paths=list(owned), read_paths=list(read))


class V38PathOwnershipTests(unittest.TestCase):
    def test_owned_overlap_is_blocked_and_read_overlap_is_allowed(self):
        result = validate_path_ownership([
            node("a", ["src/a.py"], ["shared.py"]),
            node("b", ["src/b.py"], ["shared.py"]),
        ])
        self.assertTrue(result.valid)
        conflict = validate_path_ownership([
            node("a", ["shared.py"]), node("b", ["shared.py"])
        ])
        self.assertFalse(conflict.valid)
        self.assertEqual(conflict.conflicts[0].path, "shared.py")

    def test_traversal_and_absolute_paths_are_rejected(self):
        with self.assertRaises(ValueError):
            validate_path_ownership([node("a", ["../outside.py"])])
        with self.assertRaises(ValueError):
            validate_path_ownership([node("a", ["/outside.py"])])


if __name__ == "__main__":
    unittest.main()
