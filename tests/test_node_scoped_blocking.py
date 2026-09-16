import unittest

from vibe_guide.dag import dependency_closure, node_scoped_ready
from vibe_guide.models import DAGNode


def node(node_id, deps=(), status="planned", integration_after=()):
    contract = {"input":"i","output":"o","error_behavior":"e","acceptance_example":"a","risk_tags":["test"],"writer":node_id+"-w","worktree":"/tmp/"+node_id,"allowlist":[]}
    return DAGNode(node_id, node_id, list(deps), list(integration_after), "g", contract, status)

class NodeScopedBlockingTests(unittest.TestCase):
    def test_blocked_node_only_blocks_dependency_closure(self):
        nodes = [node("blocked", status="blocked_unknown"), node("child", ["blocked"]), node("independent")]
        self.assertEqual(dependency_closure(nodes, {"blocked"}), {"blocked", "child"})
        self.assertEqual(node_scoped_ready(nodes, blocked_ids={"blocked"}), ["independent"])

    def test_integration_after_is_non_blocking(self):
        nodes = [node("base"), node("parallel", integration_after=["base"])]
        self.assertEqual(node_scoped_ready(nodes), ["base", "parallel"])

    def test_repair_wait_keeps_unrelated_ready(self):
        nodes = [node("repairing", status="running"), node("independent")]
        self.assertEqual(node_scoped_ready(nodes), ["independent"])

    def test_delivered_dependency_waits_for_independent_acceptance(self):
        nodes = [node("developer", status="delivered"), node("downstream", ["developer"])]
        self.assertEqual(node_scoped_ready(nodes), [])

    def test_accepted_dependency_unlocks_downstream(self):
        nodes = [node("developer", status="accepted"), node("downstream", ["developer"])]
        self.assertEqual(node_scoped_ready(nodes), ["downstream"])

if __name__ == "__main__":
    unittest.main()
