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

    def test_started_set_survives_snapshot_reload_key_order(self):
        def node(node_id):
            return SimpleNamespace(id=node_id, depends_on=[], parallel_group="g", allowlist=[], owned_paths=[], writer="w", reviewer="r", worktree="." + node_id)

        monitor = object.__new__(Monitor)
        monitor.plan = SimpleNamespace(complexity_band="complex", version=3)
        monitor.nodes = {"stall": node("stall"), "heal": node("heal")}
        running = {"status": "running", "active_task": {"role": "developer"}}
        # Dispatch order is DAG order; save_snapshot writes sort_keys=True, so
        # the reloaded nodes mapping comes back alphabetical while the
        # persisted started_nodes list keeps dispatch order.
        snapshot = SimpleNamespace(nodes={"heal": dict(running), "stall": dict(running)}, handles={}, dag_revision=3, ready_set=[], topology_digest="", started_nodes=["stall", "heal"], active_concurrency=2, capacity=5, parallel_groups={"stall": ["g"], "heal": ["g"]}, monitor_entry_evidence="monitor.resume")
        ready, digest = monitor._topology_projection(snapshot)
        snapshot.ready_set = ready
        snapshot.topology_digest = digest
        monitor._validate_execution_topology(snapshot)

        snapshot.started_nodes = ["stall"]
        with self.assertRaises(PermissionError):
            monitor._validate_execution_topology(snapshot)


if __name__ == "__main__":
    unittest.main()
