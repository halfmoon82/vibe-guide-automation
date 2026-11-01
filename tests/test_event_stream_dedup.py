"""Event stream stays readable: idle repeats are not written, every event has a time."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from vibe_guide import stall, state
from vibe_guide.contracts import RunEvent
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import _collapse_adjacent_repeats

RUN = "run-" + "a" * 32
PROV = {"role": "system", "task_id": None, "handle_id": None, "generation": 0,
        "authorization_digest": "", "node_contract_digest": ""}
TOPOLOGY = {"run_id": RUN, "plan_revision": 1, "node_ids": ["a"], "started_nodes": [],
            "active_concurrency": 0, "capacity": 3, "parallel_groups": {},
            "monitor_entry_evidence": "monitor.tick"}


class EventStreamDedup(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = ProjectPaths(Path(self._tmp.name))

    def append(self, name, **data):
        return state.append_event(self.paths, RunEvent(name, {"run_id": RUN, **data}), dict(PROV))

    def names(self):
        return [item["event"] for item in state.load_events(self.paths, RUN)]

    def idle_cycles(self, count):
        base = {k: v for k, v in TOPOLOGY.items() if k != "run_id"}
        for _ in range(count):
            self.append("supervisor_heartbeat", status="active")
            self.append("execution_topology_observed", **base)

    def test_idle_cycles_do_not_grow_the_log(self):
        self.idle_cycles(2)
        settled = len(self.names())
        self.idle_cycles(58)  # up to 30 minutes of 30-second cycles
        self.assertEqual(len(self.names()), settled)
        self.assertEqual(self.names().count("execution_topology_observed"), 1)

    def test_topology_is_written_again_when_it_changes_even_back_to_an_old_value(self):
        base = {k: v for k, v in TOPOLOGY.items() if k != "run_id"}
        self.append("execution_topology_observed", **base)
        self.append("execution_topology_observed", **{**base, "started_nodes": ["a"]})
        self.append("execution_topology_observed", **base)
        self.assertEqual(self.names().count("execution_topology_observed"), 3)

    def test_state_changing_events_are_never_dropped(self):
        for _ in range(3):
            self.append("self_heal_attempt", node_id="a", outcome="x")
            self.append("blocked_unknown", node_id="a", reason="r")
        self.assertEqual(self.names().count("self_heal_attempt"), 3)
        self.assertEqual(self.names().count("blocked_unknown"), 3)

    def test_heartbeat_after_another_event_is_written(self):
        self.append("supervisor_heartbeat", status="active")
        self.append("start_intent", node_id="a")
        self.append("supervisor_heartbeat", status="active")
        self.assertEqual(self.names().count("supervisor_heartbeat"), 2)

    def test_skipped_write_returns_the_log_head_as_cursor(self):
        self.append("start_intent", node_id="a")
        first = self.append("supervisor_heartbeat", status="active")
        self.assertEqual(self.append("supervisor_heartbeat", status="active"), first)
        self.assertEqual(first, len(state.load_events(self.paths, RUN)))

    def test_every_new_event_has_a_utc_time_and_the_chain_verifies(self):
        self.append("start_intent", node_id="a")
        self.append("delivered", node_id="a")
        records = state.load_events(self.paths, RUN)
        for item in records:
            self.assertRegex(item["recorded_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(records[1]["previous_event_digest"], records[0]["event_digest"])

    def test_log_written_before_the_time_field_still_loads_and_extends(self):
        self.append("start_intent", node_id="a")
        path = self.paths.root / ".vibe" / "runs" / RUN / "events.jsonl"
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy.pop("recorded_at")
        legacy.pop("event_digest")
        legacy["event_digest"] = state.hashlib.sha256(json.dumps(
            legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        path.write_text(json.dumps(legacy, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
                        encoding="utf-8")
        self.append("delivered", node_id="a")
        records = state.load_events(self.paths, RUN)
        self.assertNotIn("recorded_at", records[0])
        self.assertIn("recorded_at", records[1])

    def test_a_tampered_time_breaks_the_digest(self):
        self.append("start_intent", node_id="a")
        path = self.paths.root / ".vibe" / "runs" / RUN / "events.jsonl"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["recorded_at"] = "2001-01-01T00:00:00Z"
        path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            state.load_events(self.paths, RUN)

    def test_recent_event_types_collapse_adjacent_repeats_before_the_window(self):
        names = ["a"] + ["supervisor_heartbeat"] * 50 + ["b"] + ["supervisor_heartbeat"] * 50
        self.assertEqual(_collapse_adjacent_repeats(names)[-20:],
                         ["a", "supervisor_heartbeat", "b", "supervisor_heartbeat"])

    def test_stall_verdict_is_the_same_with_and_without_repeats(self):
        node = {"status": "running", "developer_generation": 1}
        raw = [{"event": "supervisor_heartbeat"}] * 40 + [{"event": "self_heal_attempt", "data": {"node_id": "n"}}]
        deduped = [{"event": "supervisor_heartbeat"}] + raw[-1:]
        previous = None
        outcomes = []
        for events in (raw, deduped):
            previous = None
            for _ in range(stall.STALL_BEATS):
                result = stall.assess_stalls({"n": node}, {}, events, previous)
                previous = result["record"]
            outcomes.append(result["nodes"]["n"])
        self.assertEqual(outcomes[0], outcomes[1])
        self.assertTrue(outcomes[0]["stalled"])


if __name__ == "__main__":
    unittest.main()
