import unittest
from types import SimpleNamespace

from vibe_guide.monitor import Monitor


class DagTopologyEnforcementTests(unittest.TestCase):
    def test_ready_set_and_digest_drift_are_blocked(self):
        node = SimpleNamespace(id="a", depends_on=[], parallel_group="g", allowlist=[], owned_paths=[], writer="w", reviewer="r", worktree=".w")
        monitor = object.__new__(Monitor)
        monitor.plan = SimpleNamespace(complexity_band="complex", version=3)
        monitor.nodes = {"a": node}
        snapshot = SimpleNamespace(nodes={"a": {"status": "planned"}}, handles={}, dag_revision=3, ready_set=["a"], topology_digest="", started_nodes=[], active_concurrency=0, capacity=1, parallel_groups={}, monitor_entry_evidence="monitor.start")
        _, digest = monitor._topology_projection(snapshot)
        snapshot.topology_digest = digest
        monitor._validate_execution_topology(snapshot)
        snapshot.ready_set = []
        with self.assertRaises(PermissionError):
            monitor._validate_execution_topology(snapshot)


if __name__ == "__main__":
    unittest.main()
