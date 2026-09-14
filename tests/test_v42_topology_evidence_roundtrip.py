"""Monitor 的拓扑证据必须能写进事件日志、再原样读回来做比对。

这两条断言各钉住一个曾让每个 complex 计划首次派发失败的缺陷：
  1. `_sanitize_event_data` 的字段白名单漏了拓扑观测字段，写盘时被静默丢弃，
     `_validate_execution_topology` 逐字段比对随即报 "topology evidence drift"；
  2. `Monitor.start()` 只在派发之后记录拓扑观测，而派发路径上的
     `_schedule_ready()` 先要求日志里已有这条观测，于是报
     "topology evidence missing"。
"""
import tempfile
import unittest
from pathlib import Path

from vibe_guide.contracts import RunEvent
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import append_event, load_events

TOPOLOGY_FIELDS = {
    "run_id": "run-" + "0" * 32,
    "plan_revision": 3,
    "node_ids": ["a", "b"],
    "started_nodes": ["a"],
    "active_concurrency": 1,
    "capacity": 4,
    "parallel_groups": {"a": ["g"]},
    "monitor_entry_evidence": "monitor.start",
}


class TopologyEvidenceRoundTripTests(unittest.TestCase):
    def test_topology_observation_survives_event_sanitization(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            append_event(paths, RunEvent("execution_topology_observed", dict(TOPOLOGY_FIELDS)))
            observed = [
                event for event in load_events(paths, TOPOLOGY_FIELDS["run_id"])
                if event.get("event") == "execution_topology_observed"
            ]
            self.assertEqual(len(observed), 1)
            # Every field the validator compares must come back unchanged;
            # a dropped field reads as drift and blocks the run.
            self.assertEqual(observed[-1]["data"], TOPOLOGY_FIELDS)

    def test_start_records_topology_observation_before_scheduling(self):
        import inspect

        from vibe_guide.monitor import Monitor

        source = inspect.getsource(Monitor.start)
        record_at = source.index('self._record_topology_projection(snapshot, "monitor.start")')
        schedule_at = source.index("self._schedule_ready(snapshot, runner)")
        self.assertLess(
            record_at,
            schedule_at,
            "_schedule_ready() validates the topology against the event log, so the "
            "observation must be recorded before the first dispatch",
        )


if __name__ == "__main__":
    unittest.main()
