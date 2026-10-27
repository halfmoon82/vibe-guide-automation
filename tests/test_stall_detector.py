import copy
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide import stall
from vibe_guide.paths import ProjectPaths
from vibe_guide.stall import (
    TIER_AUTO_RETRY,
    TIER_ESCALATE,
    TIER_INVESTIGATE,
    assess_stalls,
    count_self_heal_attempts,
    load_progress,
    node_fingerprint,
    progress_path,
    record_beat,
)

RUN_ID = "run-7b241c25a9a34a6a84b11cec87675714"
NODE = "integration-review"


def make_node(beat=0, **overrides):
    node = {
        "status": "running",
        "developer_generation": 1,
        "review_generation": 0,
        "active_task": None,
        "evidence": [],
        "handoff_file": "",
        "status_file": "",
        "user_status": "自动修复中",
        "retryable_action": {
            "attempt": 1 + beat,
            "next_retry_at": 1793087998.0 + beat * 60,
            "last_observation_ref": "provider-pending",
            "phase": "develop",
        },
    }
    node.update(overrides)
    return node


def heal_event(node_id=NODE):
    return {"event": "self_heal_attempt", "data": {"node_id": node_id, "run_id": RUN_ID}}


def beat(previous, node, events=()):
    return assess_stalls({NODE: node}, {}, list(events), previous)


class FingerprintTests(unittest.TestCase):
    def test_volatile_retry_fields_are_not_progress(self):
        self.assertEqual(node_fingerprint(make_node(0)), node_fingerprint(make_node(7)))

    def test_progress_bearing_fields_each_change_the_fingerprint(self):
        base = node_fingerprint(make_node())
        changes = {
            "status": make_node(status="review"),
            "developer_generation": make_node(developer_generation=2),
            "review_generation": make_node(review_generation=1),
            "active_task": make_node(active_task="task_1"),
            "delivery": make_node(handoff_file=".vibe/handoff.md"),
            "status_file": make_node(status_file=".vibe/status.md"),
            "latest_evidence": make_node(evidence=[{"kind": "delivery"}]),
            "observation": make_node(
                retryable_action={"attempt": 1, "next_retry_at": 1.0, "last_observation_ref": "other"}
            ),
        }
        for name, node in changes.items():
            with self.subTest(name):
                self.assertNotEqual(base, node_fingerprint(node))

    def test_task_cursor_change_counts_as_progress(self):
        node = make_node(active_task="t1")
        first = node_fingerprint(node, {"t1": {"status": "running", "generation": 1, "cursor": "a"}})
        second = node_fingerprint(node, {"t1": {"status": "running", "generation": 1, "cursor": "b"}})
        self.assertNotEqual(first, second)

    def test_node_without_retryable_action_is_fingerprinted(self):
        node = make_node()
        del node["retryable_action"]
        self.assertEqual(len(node_fingerprint(node)), 64)


class EscalationTests(unittest.TestCase):
    def run_beats(self, count, events_by_beat=None):
        previous, outcomes = None, []
        for index in range(count):
            events = (events_by_beat or {}).get(index, [])
            outcome = beat(previous, make_node(index), events)
            previous = outcome["record"]
            outcomes.append(outcome["nodes"][NODE])
        return outcomes

    def test_third_unchanged_beat_is_supervisor_investigate(self):
        first, second, third = self.run_beats(3)
        self.assertEqual((first["unchanged_beats"], first["tier"], first["stalled"]), (1, TIER_AUTO_RETRY, False))
        self.assertEqual((second["unchanged_beats"], second["tier"], second["stalled"]), (2, TIER_AUTO_RETRY, False))
        self.assertEqual((third["unchanged_beats"], third["tier"], third["stalled"]), (3, TIER_INVESTIGATE, True))

    def test_one_self_heal_attempt_keeps_investigating_two_escalate(self):
        events = [heal_event()]
        outcomes = self.run_beats(5, {3: events, 4: events + [heal_event()]})
        self.assertEqual(outcomes[3]["tier"], TIER_INVESTIGATE)
        self.assertEqual(outcomes[3]["attempts_since_change"], 1)
        self.assertEqual(outcomes[4]["tier"], TIER_ESCALATE)
        self.assertEqual(outcomes[4]["attempts_since_change"], 2)

    def test_new_delivery_resets_count_and_attempts(self):
        events = [heal_event(), heal_event()]
        previous = None
        for index in range(4):
            previous = beat(previous, make_node(index), events if index >= 3 else [])["record"]
        outcome = beat(previous, make_node(4, handoff_file=".vibe/handoff.md"), events)
        result = outcome["nodes"][NODE]
        self.assertEqual(result["unchanged_beats"], 1)
        self.assertEqual(result["attempts_since_change"], 0)
        self.assertEqual(result["tier"], TIER_AUTO_RETRY)
        self.assertFalse(result["stalled"])
        again = beat(outcome["record"], make_node(5, handoff_file=".vibe/handoff.md"), events)
        self.assertEqual(again["nodes"][NODE]["attempts_since_change"], 0)

    def test_attempts_before_the_last_change_do_not_count(self):
        events = [heal_event()]
        previous = beat(None, make_node(0), events)["record"]
        outcome = beat(previous, make_node(1, status="review"), events)
        self.assertEqual(outcome["nodes"][NODE]["attempts_since_change"], 0)

    def test_attempts_of_other_nodes_do_not_count(self):
        outcomes = self.run_beats(3, {2: [heal_event("other-node"), heal_event("other-node")]})
        self.assertEqual(outcomes[2]["tier"], TIER_INVESTIGATE)
        self.assertEqual(outcomes[2]["attempts_since_change"], 0)

    def test_accepted_nodes_are_not_tracked(self):
        outcome = assess_stalls({NODE: make_node(status="accepted")}, {}, [], None)
        self.assertEqual(outcome["nodes"], {})
        self.assertEqual(outcome["record"]["nodes"], {})

    def test_assess_does_not_mutate_inputs(self):
        node = make_node()
        before = copy.deepcopy(node)
        beat(None, node)
        self.assertEqual(node, before)


class UnknownRecordTests(unittest.TestCase):
    def test_unusable_previous_records_count_from_zero_and_flag_unknown(self):
        bad_records = [
            None,
            "garbage",
            [],
            {},
            {"nodes": []},
            {"nodes": {NODE: "x"}},
            {"nodes": {NODE: {"fingerprint": 5, "unchanged_beats": 1, "attempts_baseline": 0}}},
            {"nodes": {NODE: {"fingerprint": "f", "unchanged_beats": True, "attempts_baseline": 0}}},
            {"nodes": {NODE: {"fingerprint": "f", "unchanged_beats": -1, "attempts_baseline": 0}}},
            {"nodes": {NODE: {"fingerprint": "f", "unchanged_beats": 1}}},
        ]
        for previous in bad_records:
            with self.subTest(previous=previous):
                outcome = beat(previous, make_node(), [heal_event(), heal_event()])
                result = outcome["nodes"][NODE]
                self.assertTrue(outcome["unknown_previous"])
                self.assertTrue(result["unknown_previous"])
                self.assertEqual(result["unchanged_beats"], 1)
                self.assertFalse(result["stalled"])
                self.assertEqual(result["tier"], TIER_AUTO_RETRY)

    def test_valid_record_is_not_unknown(self):
        record = beat(None, make_node())["record"]
        outcome = beat(record, make_node(1))
        self.assertFalse(outcome["unknown_previous"])
        self.assertFalse(outcome["nodes"][NODE]["unknown_previous"])

    def test_known_record_missing_a_node_starts_that_node_at_one(self):
        record = beat(None, make_node())["record"]
        outcome = assess_stalls({NODE: make_node(1), "new": make_node()}, {}, [], record)
        self.assertEqual(outcome["nodes"]["new"]["unchanged_beats"], 1)
        self.assertEqual(outcome["nodes"][NODE]["unchanged_beats"], 2)
        self.assertFalse(outcome["nodes"]["new"]["unknown_previous"])


class CountAttemptsTests(unittest.TestCase):
    def test_counts_by_node_and_ignores_other_events_and_junk(self):
        events = [
            heal_event("a"),
            heal_event("a"),
            heal_event("b"),
            {"event": "node_repaired", "data": {"node_id": "a"}},
            {"event": "self_heal_attempt", "data": {}},
            {"event": "self_heal_attempt", "node_id": "c"},
            "junk",
            None,
        ]
        self.assertEqual(count_self_heal_attempts(events), {"a": 2, "b": 1, "c": 1})

    def test_empty_or_none(self):
        self.assertEqual(count_self_heal_attempts(None), {})
        self.assertEqual(count_self_heal_attempts([]), {})


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))

    def test_record_beat_persists_in_independent_file_and_accumulates(self):
        for index in range(3):
            outcome = record_beat(self.paths, RUN_ID, {NODE: make_node(index)}, {}, [])
        self.assertEqual(outcome["nodes"][NODE]["tier"], TIER_INVESTIGATE)
        path = progress_path(self.paths, RUN_ID)
        self.assertEqual(path.name, "progress.json")
        self.assertEqual(path.parent.name, RUN_ID)
        stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["nodes"][NODE]["unchanged_beats"], 3)
        self.assertEqual(load_progress(self.paths, RUN_ID), stored)
        leftovers = [item.name for item in path.parent.iterdir() if item.name != "progress.json"]
        self.assertEqual(leftovers, [])

    def test_missing_corrupt_and_invalid_files_load_as_none(self):
        self.assertIsNone(load_progress(self.paths, RUN_ID))
        path = progress_path(self.paths, RUN_ID)
        path.parent.mkdir(parents=True, exist_ok=True)
        for content in (b"{not json", b"[]", b'{"nodes": {"n": {"fingerprint": 1}}}', b"\xff\xfe"):
            path.write_bytes(content)
            with self.subTest(content=content):
                self.assertIsNone(load_progress(self.paths, RUN_ID))

    def test_corrupt_file_restarts_counting_instead_of_reporting_idle(self):
        for index in range(3):
            record_beat(self.paths, RUN_ID, {NODE: make_node(index)}, {}, [])
        progress_path(self.paths, RUN_ID).write_text("{truncated", encoding="utf-8")
        outcome = record_beat(self.paths, RUN_ID, {NODE: make_node(3)}, {}, [])
        self.assertTrue(outcome["unknown_previous"])
        self.assertEqual(outcome["nodes"][NODE]["unchanged_beats"], 1)
        self.assertEqual(outcome["nodes"][NODE]["tier"], TIER_AUTO_RETRY)
        self.assertEqual(load_progress(self.paths, RUN_ID)["nodes"][NODE]["unchanged_beats"], 1)

    def test_progress_file_is_not_a_state_or_event_file(self):
        self.assertNotIn(stall.PROGRESS_FILE, {"state.json", "events.jsonl", "state.previous.json"})


if __name__ == "__main__":
    unittest.main()
