import unittest

from vibe_guide.state import RunSnapshot


class ExecutionRecoveryTests(unittest.TestCase):
    def test_engine_and_topology_fields_round_trip(self):
        snapshot = RunSnapshot("run-recovery", "p", 4, "running", {"a": {"status": "planned"}}, {}, execution_engine="vibeguide_monitor", engine_mode="dag", engine_evidence_ref="probe:1", dag_revision=4, ready_set=["a"], topology_digest="a" * 64)
        payload = snapshot.to_dict()
        self.assertEqual(payload["execution_engine"], "vibeguide_monitor")
        self.assertEqual(payload["engine_mode"], "dag")
        self.assertEqual(payload["ready_set"], ["a"])
        self.assertEqual(payload["topology_digest"], "a" * 64)


if __name__ == "__main__":
    unittest.main()
