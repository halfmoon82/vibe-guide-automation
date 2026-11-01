"""Cluster B: a local fault must not become the whole run's final state."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide import daemon as dm
from vibe_guide import stall, worker_liveness
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events, run_dir

RUN = "run-" + "b" * 32


def node(status, **extra):
    return {"status": status, "worktree": ".worktrees/n", **extra}


class Supervisor:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.ticks = 0
        self.released = False
        self.lease = SimpleNamespace(release=self._release)

    def _release(self, run_id):
        self.released = True

    def recover_or_start(self):
        return {"active_supervisors": 1}

    def run_once(self):
        self.ticks += 1
        return self.snapshot


def snap(status, nodes):
    return SimpleNamespace(status=status, nodes=nodes, tasks={}, authorization={})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "home").mkdir()
        self.paths = ProjectPaths(self.root, vibe_home=self.root / "home")
        run_dir(self.paths, RUN, create=True)
        self.now = [1000.0]
        self.pushes = []
        self.push_status = "queued_inbox"
        self.pending = []

    def notifier(self, message, kind, node_id=""):
        self.pushes.append((kind, node_id))
        return {"status": self.push_status}

    def make(self, supervisor, **config):
        values = {"interval": 1, "heal_interval": 10 ** 9, "blocked_grace": 60}
        values.update(config)
        return dm.Daemon(
            self.paths, RUN, supervisor, dm.DaemonConfig(**values), clock=lambda: self.now[0],
            sleep=lambda _s: self.now.__setitem__(0, self.now[0] + 30), notifier=self.notifier,
            assess=lambda: {"status": "ok", "nodes": {}}, archive=lambda: self.fail("a parked run is not archived"),
            sessions=lambda s: {"live": [], "unknown": None}, pending_work=lambda: list(self.pending))

    def escalate(self, daemon, *node_ids):
        state = daemon._heal_state()
        for node_id in node_ids:
            state["nodes"][node_id] = {"escalated": True}
        daemon._save_heal_state(state)

    def events(self):
        return [e["event"] for e in load_events(self.paths, RUN)]


class ParkingTests(Base):
    blocked = staticmethod(lambda: snap("blocked_unknown", {"a": node("blocked_unknown"), "b": node("accepted")}))

    def test_a_parked_run_keeps_its_directory_and_is_marked_parked(self):
        daemon = self.make(Supervisor(self.blocked()))
        self.escalate(daemon, "a")
        outcome = daemon.run(max_cycles=10)
        self.assertEqual(outcome["status"], "parked")
        self.assertTrue(run_dir(self.paths, RUN).is_dir())
        self.assertEqual(run_dir(self.paths, RUN), self.paths.vibe / "runs" / RUN)
        record = json.loads((run_dir(self.paths, RUN) / dm.DAEMON_FILE).read_text(encoding="utf-8"))
        self.assertEqual(record["state"], "parked")
        self.assertIn("run_parked", self.events())
        self.assertNotIn("run_archived", self.events())
        self.assertTrue(daemon.supervisor.released)

    def test_unconsumed_delivery_is_consumed_before_the_run_parks(self):
        self.pending = ["delivery:a"]
        supervisor = Supervisor(self.blocked())
        daemon = self.make(supervisor, blocked_grace=10 ** 6)
        self.escalate(daemon, "a")
        outcome = daemon.run(max_cycles=4)
        self.assertEqual(outcome["status"], "max_cycles")
        self.assertGreaterEqual(supervisor.ticks, 3)  # still ticking: the delivery gets consumed
        self.assertNotIn("run_parked", self.events())
        self.pending = []  # the tick consumed it
        outcome = daemon.run(max_cycles=10)
        self.assertEqual(outcome["status"], "parked")

    def test_the_default_mailbox_probe_sees_a_real_unconsumed_delivery(self):
        deliveries = self.paths.vibe / "provider-actions" / "deliveries"
        deliveries.mkdir(parents=True)
        (deliveries / ("%s-a-developer-g1.json" % RUN)).write_text(
            json.dumps({"run_id": RUN, "issue_id": "a"}), encoding="utf-8")
        daemon = dm.Daemon(self.paths, RUN, Supervisor(self.blocked()), dm.DaemonConfig())
        self.assertEqual(daemon._default_pending_work(), ["delivery:a"])
        (deliveries / ("%s-a-developer-g1.json" % RUN)).write_text(
            json.dumps({"run_id": RUN, "issue_id": "a", "consumed_by": "x"}), encoding="utf-8")
        self.assertEqual(daemon._default_pending_work(), [])

    def test_an_unreadable_mailbox_never_counts_as_empty(self):
        def broken():
            raise OSError("mailbox")
        daemon = self.make(Supervisor(self.blocked()), blocked_grace=10 ** 6)
        daemon._pending_work = broken
        self.escalate(daemon, "a")
        self.assertEqual(daemon.run(max_cycles=4)["status"], "max_cycles")

    def test_no_address_keeps_the_run_open_and_status_says_so(self):
        self.push_status = "no_address"
        daemon = self.make(Supervisor(self.blocked()))
        self.escalate(daemon, "a")
        self.assertEqual(daemon.run(max_cycles=6)["status"], "max_cycles")
        self.assertNotIn("run_parked", self.events())
        state = daemon._heal_state()
        self.assertFalse(state["nodes"]["a"].get("pm_notified"))
        self.assertTrue(state["nodes"]["a"]["escalated"])
        # the message is only re-pushed once an address exists
        before = len(self.pushes)
        daemon.run(max_cycles=3)
        self.assertEqual(len(self.pushes), before)
        dm.register_pm_address(self.paths, RUN, "claude-code", "pm")
        self.push_status = "sent"
        self.assertEqual(daemon.run(max_cycles=10)["status"], "parked")
        self.assertTrue(daemon._heal_state()["nodes"]["a"]["pm_notified"])

    def test_pm_address_notice_comes_from_the_real_notifier_receipt(self):
        notifier = dm.PmNotifier(self.paths, RUN)
        self.assertEqual(notifier("hi", "run_blocked_unknown")["status"], "no_address")
        self.assertIn("产品经理地址未登记", dm.pm_address_notice(self.paths, RUN))
        dm.register_pm_address(self.paths, RUN, "claude-code", "pm")
        self.assertIsNone(dm.pm_address_notice(self.paths, RUN))

    def test_pm_notified_needs_a_sent_or_queued_receipt_and_old_state_reads_unnotified(self):
        daemon = self.make(Supervisor(self.blocked()))
        for status, expected in (("sent", True), ("queued_inbox", True), ("no_address", False),
                                 ("unknown", False), ("inbox_failed", False)):
            state = daemon._heal_state()
            state["nodes"] = {}
            self.push_status = status
            daemon._escalate("a", "needs_decision", "x", "external_authorization", state=state)
            self.assertTrue(state["nodes"]["a"]["escalated"], status)
            self.assertNotIn("pm_notified", {} if expected else state["nodes"]["a"], status)
            self.assertIs(bool(state["nodes"]["a"].get("pm_notified")), expected, status)
        legacy = {"nodes": {"a": {"escalated": True, "retried": True, "repairs": 2}}}
        self.assertFalse(legacy["nodes"]["a"].get("pm_notified"))


class RepairBudgetTests(Base):
    def launcher(self, *_a, **_k):
        self.launched.append(_k["node_id"])
        return SimpleNamespace(status="ok", reason="", text="", usage=None)

    def test_one_node_spending_its_budget_does_not_take_the_other_nodes_share(self):
        self.launched = []
        nodes = {"a": node("running"), "b": node("running")}
        daemon = self.make(Supervisor(snap("running", nodes)), max_sessions=1)
        daemon._snapshot = snap("running", nodes)
        daemon.launcher = self.launcher
        daemon._verify = lambda n, r: {"status": "verify_failed"}
        state = daemon._heal_state()
        state["budget"] = {"a": {"sessions": 1, "tokens": 0}}
        for node_id in ("a", "b"):
            state["nodes"][node_id] = {"fingerprint": "", "retried": True, "repairs": 0, "escalated": False}
        entry = {"pack": {"role": "developer"}}
        daemon._save_heal_state(state)
        self.assertFalse(daemon._level_two(state, state["nodes"]["a"], "a", entry, daemon._snapshot))
        self.assertTrue(state["nodes"]["a"]["escalated"])
        self.assertTrue(daemon._level_two(state, state["nodes"]["b"], "b", entry, daemon._snapshot))
        self.assertEqual(self.launched, ["b"])
        self.assertEqual(state["budget"]["b"]["sessions"], 1)

    def test_a_node_that_exhausted_its_budget_does_not_block_the_next_node_in_the_same_pass(self):
        self.launched = []
        nodes = {"a": node("running"), "b": node("running")}
        snapshot = snap("running", nodes)
        report = {"status": "ok", "nodes": {n: {"tier": "supervisor_investigate", "pack": {"role": "developer"}}
                                            for n in nodes}}
        daemon = self.make(Supervisor(snapshot), max_sessions=1)
        daemon._assess = lambda: report
        daemon.launcher = self.launcher
        daemon._verify = lambda n, r: {"status": "verify_failed"}
        state = daemon._heal_state()
        state["budget"] = {"a": {"sessions": 1, "tokens": 0}}
        state["nodes"] = {n: {"fingerprint": dm._node_fingerprint(snapshot, n), "retried": True,
                              "repairs": 0, "escalated": False} for n in nodes}
        daemon._save_heal_state(state)
        daemon._snapshot = snapshot
        daemon.heal_pass(snapshot)
        self.assertEqual(self.launched, ["b"])

    def test_at_most_one_repair_session_still_starts_per_pass(self):
        self.launched = []
        nodes = {"a": node("running"), "b": node("running")}
        snapshot = snap("running", nodes)
        report = {"status": "ok", "nodes": {n: {"tier": "supervisor_investigate", "pack": {"role": "developer"}}
                                            for n in nodes}}
        daemon = self.make(Supervisor(snapshot))
        daemon._assess = lambda: report
        daemon.launcher = self.launcher
        daemon._verify = lambda n, r: {"status": "verify_failed"}
        state = daemon._heal_state()
        state["nodes"] = {n: {"fingerprint": dm._node_fingerprint(snapshot, n), "retried": True,
                              "repairs": 0, "escalated": False} for n in nodes}
        daemon._save_heal_state(state)
        daemon._snapshot = snapshot
        daemon.heal_pass(snapshot)
        self.assertEqual(self.launched, ["a"])

    def test_nothing_to_re_check_is_inconclusive_and_not_a_failed_repair(self):
        nodes = {"a": node("running")}
        daemon = self.make(Supervisor(snap("running", nodes)))
        daemon._verify = lambda n, r: {"status": "blocked_unknown",
                                       "reason": "cannot identify this node's worker session; nothing to re-check"}
        daemon.launcher = lambda *a, **k: SimpleNamespace(status="ok", reason="", text="", usage=None)
        daemon._snapshot = snap("running", nodes)
        state = daemon._heal_state()
        node_state = {"fingerprint": "", "retried": True, "repairs": 1, "escalated": False}
        state["nodes"]["a"] = node_state
        daemon._level_two(state, node_state, "a", {"pack": {"role": "developer"}}, daemon._snapshot)
        self.assertEqual(node_state["repairs"], 1)
        recheck = [e for e in load_events(self.paths, RUN) if (e.get("data") or {}).get("kind") == "recheck"]
        self.assertEqual(recheck[-1]["data"]["reason_code"], "inconclusive")


class StallLivenessTests(Base):
    def setUp(self):
        super().setUp()
        self.node = {"status": "retry_pending", "active_task": {"role": "developer", "generation": 1,
                                                                  "task_id": "t", "handle_id": "h"}}
        self.deliveries = self.paths.vibe / "provider-actions" / "deliveries"
        self.deliveries.mkdir(parents=True)
        self.file = self.deliveries / ("%s-n-developer-g1.json" % RUN)

    def beats(self, count, submit):
        tiers = []
        for index in range(count):
            if submit:
                self.file.write_text(json.dumps({"i": index}), encoding="utf-8")
                os.utime(str(self.file), ns=(10 ** 18 + index * 10 ** 9, 10 ** 18 + index * 10 ** 9))
            outcome = stall.record_beat(self.paths, RUN, {"n": self.node}, {}, [])
            tiers.append(outcome["nodes"]["n"]["tier"])
        return tiers

    def test_a_worker_that_keeps_submitting_never_reaches_investigation(self):
        self.assertNotIn(stall.TIER_INVESTIGATE, self.beats(8, submit=True))

    def test_a_node_with_no_worker_signal_still_stalls(self):
        self.file.write_text("{}", encoding="utf-8")
        self.assertIn(stall.TIER_INVESTIGATE, self.beats(6, submit=False))

    def test_an_unreadable_signal_is_not_progress(self):
        self.assertEqual(worker_liveness.liveness_signal(self.paths, RUN, "n", {"status": "running"}), None)
        self.assertIn(stall.TIER_INVESTIGATE, self.beats(6, submit=False))


class ObservationOnlyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def start(self, events, cursor="cursor-1"):
        paths = ProjectPaths(Path(self.tmp.name))
        paths.vibe.mkdir(parents=True, exist_ok=True)
        (paths.vibe / "state.json").write_text(
            '{"workflow_version": 4, "execution_mode": "sdd_first", '
            '"session_gate": "s0_required", "capability_contract_required": true}\n')
        save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))
        n = DAGNode("n1", "n1", [], [], "control",
                    {"files": ["n1.py"], "worker": "w", "worktree": ".worktrees/n1", "branch": "b"}, "ready")
        plan = Plan("survival", 1, "docs/prd.md", ["n1"], "draft")
        caps = AgentCapabilities("fake", True, True, True, True, True, "full")
        record = authorize(build_authorization_card(plan, [n], caps), "AUTHORIZE")
        runner = FakeRunner(events={("n1", "developer"): events})
        monitor = Monitor(paths, plan, [n])
        original = Monitor._load_task_binding

        def with_cursor(this, snapshot, node_id, role):
            binding = original(this, snapshot, node_id, role)
            binding.cursor = cursor  # the fake runner issues none; a real provider does
            return binding

        with patch("vibe_guide.monitor.require_entry", return_value=None), \
                patch.object(Monitor, "_load_task_binding", with_cursor):
            first = monitor.start(record, runner)
            return paths, first, monitor.tick(first.run_id, runner)

    def test_pending_and_timeout_on_a_live_handle_are_only_observed(self):
        for name, data in (("visibility_unknown", {"reason": "provider wait is pending"}),
                           ("timeout", {"reason": "provider timeout"})):
            with self.subTest(event=name):
                self.setUp()
                paths, first, second = self.start([(name, data)])
                current = second.nodes["n1"]
                self.assertEqual(current["status"], first.nodes["n1"]["status"])
                self.assertFalse(current.get("retryable_action"))
                self.assertEqual(second.handles["n1"], first.handles["n1"])
                self.assertEqual(current["provider_observation"]["count"], 1)
                self.assertIn("provider_observation", [e["event"] for e in load_events(paths, second.run_id)])

    def test_run_park_deferred_event_replays_through_a_real_monitor_tick(self):
        from vibe_guide.state import RunEvent, append_event, load_snapshot
        paths, first, _second = self.start([])
        snap = load_snapshot(paths, first.run_id)
        prov = {"role": "system", "task_id": None, "handle_id": None, "generation": 0,
                "authorization_digest": snap.authorization_digest,
                "node_contract_digest": snap.node_contract_digest}
        append_event(paths, RunEvent("run_park_deferred", {
            "run_id": first.run_id, "reason_code": "undelivered_work", "observation_count": 1}), prov)
        last = load_events(paths, first.run_id)[-1]
        self.assertEqual(last["data"].get("reason_code"), "undelivered_work")
        self.assertEqual(last["data"].get("observation_count"), 1)
        # A real Monitor replays the event instead of raising on an unknown node.
        monitor = Monitor(paths, Plan("survival", 1, "docs/prd.md", ["n1"], "draft"),
                          [DAGNode("n1", "n1", [], [], "control",
                                   {"files": ["n1.py"], "worker": "w", "worktree": ".worktrees/n1", "branch": "b"}, "ready")])
        with patch("vibe_guide.monitor.require_entry", return_value=None):
            monitor.tick(first.run_id, FakeRunner(events={}))

    def test_without_a_valid_cursor_the_old_retry_path_still_applies(self):
        _paths, _first, second = self.start([("timeout", {"reason": "provider timeout"})], cursor=None)
        self.assertEqual(second.nodes["n1"]["status"], "retry_pending")
        self.assertTrue(second.nodes["n1"]["retryable_action"])
