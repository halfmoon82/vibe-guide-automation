import unittest
from types import SimpleNamespace

from vibe_guide.monitor import Monitor


class MonitorDispatchEngineTests(unittest.TestCase):
    def test_unverified_execution_engine_is_unknown(self):
        monitor = object.__new__(Monitor)
        monitor.plan = SimpleNamespace(complexity_band="complex", version=1)
        record = SimpleNamespace(execution_engine="vibeguide_monitor", engine_mode="dag", engine_evidence_ref="unverified:probe", dag_revision=1)
        with self.assertRaises(PermissionError) as ctx:
            monitor._execution_engine_binding(record)
        self.assertIn("execution_engine_unverified", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
