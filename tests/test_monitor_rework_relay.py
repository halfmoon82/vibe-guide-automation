"""P1c: bounded rework (3 counted rounds), relay to a new developer, node cost cap."""
import unittest
import copy
from unittest.mock import patch

from tests.test_integrated_prd_acceptance import _ProjectCase, _node, V42_STATE
from vibe_guide import handoff_rebind as hr
from vibe_guide.authorization import AuthorizationRecord, authorize, build_authorization_card
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.state import load_events


class LedgerTests(unittest.TestCase):
    def fail(self, ledger, action, text="bug", **kw):
        return hr.record_review_failure(ledger, action_id=action, finding={"finding": text}, **kw)

    def test_same_finding_with_new_id_still_accumulates(self):
        ledger = None
        for index in range(3):
            ledger, counted = self.fail(ledger, "a%d" % index, "Same  Bug")
            self.assertTrue(counted)
        self.assertEqual(ledger["local_rounds"], 3)
        self.assertEqual(len(ledger["failed_approaches"]), 1)
        self.assertTrue(hr.relay_due(ledger))

    def test_repeat_action_id_does_not_double_count(self):
        ledger, _ = self.fail(None, "x")
        again, counted = self.fail(ledger, "x")
        self.assertFalse(counted)
        self.assertEqual(again["local_rounds"], 1)

    def test_non_counting_reasons(self):
        for reason in ("upstream_missing", "format_only", "environment", "supervisor"):
            ledger, counted = self.fail(None, "r", reason_class=reason)
            self.assertFalse(counted)
            self.assertEqual(ledger["local_rounds"], 0)

    def test_relay_resets_local_but_keeps_lineage(self):
        ledger = None
        for index in range(3):
            ledger, _ = self.fail(ledger, "a%d" % index, "bug %d" % index, cost=10)
        moved = hr.start_relay(ledger)
        self.assertEqual((moved["local_rounds"], moved["total_rounds"], moved["relays"]), (0, 3, 1))
        self.assertEqual(moved["cost"], 30)
        self.assertEqual(len(moved["failed_approaches"]), 3)
        self.assertFalse(hr.relay_due(moved))

    def test_cost_cap(self):
        ledger, _ = self.fail(None, "a", cost=100)
        self.assertTrue(hr.cost_exhausted(ledger, 100))
        self.assertFalse(hr.cost_exhausted(ledger, 101))
        self.assertFalse(hr.cost_exhausted(ledger, None))

    def test_relay_pack_is_frozen_and_keeps_reviewer(self):
        pack = hr.relay_pack(node_id="n", head="abc", dirty_files=["b", "a"],
                             ledger=hr.new_rework_ledger(),
                             predecessor_task_id="dev1", reviewer_identity="rev1")
        self.assertEqual(pack["uncommitted_files"], ["a", "b"])
        self.assertEqual(pack["reviewer_identity"], "rev1")


class CardCapTests(unittest.TestCase):
    def _card(self, **kw):
        caps = AgentCapabilities("claude-code", True, True, True, True, True, "full")
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        node = DAGNode("n1", "n1", [], [], "g", {"files": ["a.py"]}, "ready")
        return build_authorization_card(plan, [node], caps, **kw)

    def test_cap_is_signed_into_digest_and_roundtrips(self):
        plain = authorize(self._card(), "AUTHORIZE")
        capped = authorize(self._card(node_cost_cap=2000000), "AUTHORIZE")
        self.assertNotEqual(plain.digest, capped.digest)
        self.assertEqual(AuthorizationRecord.from_dict(capped.to_dict()).node_cost_cap, 2000000)
        self.assertNotIn("node_cost_cap", plain.to_dict())

    def test_cap_must_be_positive_int(self):
        data = authorize(self._card(node_cost_cap=5), "AUTHORIZE").to_dict()
        data["node_cost_cap"] = -1
        with self.assertRaises(ValueError):
            AuthorizationRecord.from_dict(data)


def _named(case, snapshot, name):
    return [r for r in load_events(case.paths, snapshot.run_id) if r["event"] == name]


class RelayFlowTests(_ProjectCase):
    state = V42_STATE

    def test_third_failed_review_relays_to_new_developer_same_reviewer(self):
        finding = {"finding": "still wrong", "severity": "P1"}
        monitor, runner, snapshot = self._start([_node("n1")], {
            ("n1", "developer"): [("complete", {"evidence": "d"})] * 8,
            ("n1", "reviewer"): [("review_finding", dict(finding))] * 8,
        }, card_kwargs={"node_cost_cap": 2000000})
        for _ in range(30):
            snapshot = monitor.tick(snapshot.run_id, runner)
            if any(c.get("successor") for c in runner.start_calls if c["role"] == "developer"):
                break
        self.assertTrue(_named(self, snapshot, "rework_relay_started"))
        reviews = [c for c in runner.start_calls if c["role"] == "reviewer"]
        self.assertEqual(len(reviews), 3, "no fourth review round before the relay")
        ledger = snapshot.nodes["n1"]["rework_ledger"]
        self.assertGreaterEqual(ledger["relays"], 1)
        self.assertGreaterEqual(ledger["total_rounds"], 3)
        self.assertLess(ledger["local_rounds"], 3, "new worker restarts its own count")
        developers = [c for c in runner.start_calls if c["role"] == "developer"]
        successors = [c for c in developers if c.get("successor")]
        self.assertTrue(successors, "a new developer must be dispatched")
        self.assertNotEqual(successors[0]["task_id"], developers[0]["task_id"])
        self.assertEqual({c["task_id"] for c in reviews}, {"reviewer:n1"},
                         "the original reviewer stays")
        self.assertNotEqual(snapshot.nodes["n1"].get("recovery_action"),
                            "change_reviewer_or_split_node")

    def test_cap_reached_stops_auto_relay(self):
        monitor, runner, snapshot = self._start([_node("n1")], {
            ("n1", "developer"): [("complete", {"evidence": "d"})] * 8,
            ("n1", "reviewer"): [("review_finding",
                                  {"finding": "x", "severity": "P1", "cost": 500})] * 8,
        }, card_kwargs={"node_cost_cap": 400})
        for _ in range(12):
            snapshot = monitor.tick(snapshot.run_id, runner)
            if snapshot.nodes["n1"]["status"] == "blocked_unknown":
                break
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertFalse(_named(self, snapshot, "rework_relay_started"))

    def test_card_without_cap_keeps_legacy_park(self):
        monitor, runner, snapshot = self._start([_node("n1")], {
            ("n1", "developer"): [("complete", {"evidence": "d"})] * 5,
            ("n1", "reviewer"): [("review_finding", {"finding": "w", "severity": "P1"})] * 5,
        })
        snapshot = self._tick_until(monitor, runner, snapshot, "n1",
                                    {"blocked_unknown", "blocked_design"})
        self.assertEqual(snapshot.nodes["n1"].get("recovery_action"),
                         "change_reviewer_or_split_node")


class ReplayTests(_ProjectCase):
    state = V42_STATE

    def test_relay_started_event_replays_the_relay_after_a_crash(self):
        monitor, runner, snapshot = self._start([_node("n1")], {
            ("n1", "developer"): [("complete", {"evidence": "d"})] * 8,
            ("n1", "reviewer"): [("review_finding",
                                  {"finding": "w", "severity": "P1"})] * 8,
        }, card_kwargs={"node_cost_cap": 2000000})
        for _ in range(20):
            snapshot = monitor.tick(snapshot.run_id, runner)
            if _named(self, snapshot, "rework_relay_started"):
                break
        events = load_events(self.paths, snapshot.run_id)
        started = _named(self, snapshot, "rework_relay_started")[-1]
        stale = copy.deepcopy(snapshot)
        stale.event_sequence = events.index(started)
        node = stale.nodes["n1"]
        node["rework_ledger"]["relays"] = 0
        node["status"] = "rework"
        node.pop("retryable_action", None)
        node["pair_archived"] = False
        # In the real crash window the later events do not exist yet; replay
        # exactly up to the relay record.
        prefix = events[: events.index(started) + 1]
        with patch("vibe_guide.monitor.load_events", return_value=prefix):
            monitor._reconcile_unapplied_events(stale)
        restored = stale.nodes["n1"]
        self.assertEqual(restored["rework_ledger"],
                         started["data"]["proof"]["ledger"])
        retry = restored.get("retryable_action") or {}
        self.assertTrue(retry.get("successor"))
        self.assertEqual(retry.get("role"), "developer")
        self.assertTrue(restored.get("pair_archived"))

    def test_counted_event_replays_the_ledger_after_a_crash(self):
        monitor, runner, snapshot = self._start([_node("n1")], {
            ("n1", "developer"): [("complete", {"evidence": "d"})] * 8,
            ("n1", "reviewer"): [("review_finding",
                                  {"finding": "w", "severity": "P1"})] * 8,
        }, card_kwargs={"node_cost_cap": 2000000})
        for _ in range(10):
            snapshot = monitor.tick(snapshot.run_id, runner)
            if _named(self, snapshot, "rework_round_counted"):
                break
        events = load_events(self.paths, snapshot.run_id)
        counted = _named(self, snapshot, "rework_round_counted")[-1]
        stale = copy.deepcopy(snapshot)
        stale.event_sequence = events.index(counted)
        stale.nodes["n1"].pop("rework_ledger", None)
        prefix = events[: events.index(counted) + 1]
        with patch("vibe_guide.monitor.load_events", return_value=prefix):
            monitor._reconcile_unapplied_events(stale)
        self.assertEqual(
            stale.nodes["n1"]["rework_ledger"],
            counted["data"]["proof"]["ledger"],
            "replaying the counted event must restore the ledger without double counting")


if __name__ == "__main__":
    unittest.main()
