import unittest

from vibe_guide.dag import ready_nodes, schedule_ready_nodes
from vibe_guide.models import DAGNode


def n(node_id, depends=None, integration=None, status="planned"):
    return DAGNode(node_id, node_id, depends or [], integration or [], None,
                   {"input": "x", "output": "y", "error_behavior": "err", "acceptance_example": "ok"}, status)


class V4DagDependencyTests(unittest.TestCase):
    def test_hard_dependency_blocks_until_accepted(self):
        self.assertEqual(ready_nodes([n("a", status="running"), n("b", depends=["a"])]), [])
        self.assertEqual([item.id for item in ready_nodes([n("a", status="accepted"), n("b", depends=["a"])])], ["b"])

    def test_integration_after_is_non_blocking(self):
        self.assertEqual([item.id for item in ready_nodes([n("a", integration=["future"])])], ["a"])


if __name__ == "__main__":
    unittest.main()
