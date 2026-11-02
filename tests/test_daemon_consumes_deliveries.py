"""The daemon itself receives registered deliveries (#382 #380).

A worker's ``worker-deliver`` archive must reach the node within two daemon
cycles with nobody running ``resume`` by hand -- also while the run's top
status is ``blocked_unknown`` because another node is parked.
"""
import json
import unittest
from types import SimpleNamespace

from vibe_guide import daemon as dm
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.state import load_snapshot, save_snapshot
from vibe_guide.supervisor import Supervisor

from tests.test_claude_bg_delivery_gate import BackgroundDeliveryGateTests as Base
from tests.test_claude_bg_reconnect import GOOD


class DaemonConsumesDeliveries(Base):
    # Reuse the fixture only: switch off the inherited gate tests.
    for _name in [n for n in dir(Base) if n.startswith("test_")]:
        locals()[_name] = None
    del _name

    def make_daemon(self, snapshot, **kwargs):
        runner = self.runner()
        monitor = self.monitor()
        supervisor = Supervisor(self.paths, monitor, runner, snapshot.run_id)
        config = dm.DaemonConfig(interval=0, blocked_grace=0, heal_interval=10 ** 9)
        return dm.Daemon(self.paths, snapshot.run_id, supervisor, config,
                         sleep=lambda s: None, notifier=lambda *a, **k: {"status": "sent"},
                         assess=lambda: {"status": "ok", "nodes": {}},
                         archive=lambda: None, sessions=lambda s: {"live": [], "unknown": None},
                         **kwargs)

    def park_other_node(self, snapshot):
        monitor = self.monitor()
        current = load_snapshot(self.paths, snapshot.run_id)
        monitor._mark_blocked_unknown(current, "bg2", "parked for the test", quarantine_lease=False)
        monitor._refresh_run_status(current)
        save_snapshot(self.paths, current)
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).status, "blocked_unknown")

    def test_delivery_is_received_without_manual_resume(self):
        snapshot = self.start()
        ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg", "developer", dict(GOOD), self.generation(snapshot))
        self.make_daemon(snapshot).run(max_cycles=2)
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).nodes["bg"]["status"], "review")

    def test_delivery_is_received_while_run_is_blocked_unknown(self):
        snapshot = self.start()
        self.park_other_node(snapshot)
        ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg", "developer", dict(GOOD), self.generation(snapshot))
        outcome = self.make_daemon(snapshot).run(max_cycles=2)
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).nodes["bg"]["status"], "review", outcome)

    def test_late_delivery_of_a_parked_node(self):
        snapshot = self.start()
        monitor = self.monitor()
        current = load_snapshot(self.paths, snapshot.run_id)
        monitor._mark_blocked_unknown(current, "bg", "parked for the test", quarantine_lease=False)
        monitor._mark_blocked_unknown(current, "bg2", "parked for the test", quarantine_lease=False)
        monitor._refresh_run_status(current)
        save_snapshot(self.paths, current)
        ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg", "developer", dict(GOOD), self.generation(snapshot))
        outcome = self.make_daemon(snapshot).run(max_cycles=3)
        after = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(after.nodes["bg"]["status"], "review", outcome)

    def test_unconsumed_delivery_does_not_let_a_blocked_run_park(self):
        snapshot = self.start()
        current = load_snapshot(self.paths, snapshot.run_id)
        generation = self.generation(snapshot)
        ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg", "developer", dict(GOOD), generation)
        # The report is still on disk (a node the tick cannot serve), the node is parked.
        path = self.archive_path(snapshot.run_id, "bg", generation)
        report = json.loads(path.read_text(encoding="utf-8"))
        report["consumed_by"] = None
        path.write_text(json.dumps(report), encoding="utf-8")
        daemon = self.make_daemon(snapshot)
        daemon._snapshot = SimpleNamespace(nodes={
            "bg": {"status": "blocked_unknown", "developer_generation": generation}})
        daemon._run_status = "blocked_unknown"
        daemon._parked_since = daemon.clock() - 10 ** 6  # far past blocked_grace
        daemon._heal_state()["nodes"]["bg"] = {"escalated": True}
        self.assertEqual(daemon._receivable_deliveries(), ["bg:developer:g%d" % generation])
        self.assertFalse(daemon._parked())
        # A report of an older generation is history and does not hold the run.
        daemon._snapshot = SimpleNamespace(nodes={
            "bg": {"status": "blocked_unknown", "developer_generation": generation + 1}})
        self.assertEqual(daemon._receivable_deliveries(), [])

    def test_wake_failed_marker_is_accepted_and_visible_to_the_daemon(self):
        snapshot = self.start()
        generation = self.generation(snapshot)
        payload = dict(GOOD, wake_failed=True)
        store = ProviderActionStore(self.paths)
        outcome = store.record_worker_delivery(snapshot.run_id, "bg", "developer", payload, generation)
        self.assertTrue(outcome["recorded"])
        self.assertEqual(store.unconsumed_delivery_records(snapshot.run_id), [{
            "issue_id": "bg", "role": "developer", "generation": generation, "wake_failed": True}])
        self.make_daemon(snapshot).run(max_cycles=2)
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).nodes["bg"]["status"], "review")

    def test_malformed_delivery_is_still_refused_with_the_expected_shape(self):
        snapshot = self.start()
        with self.assertRaises(ValueError) as caught:
            ProviderActionStore(self.paths).record_worker_delivery(
                snapshot.run_id, "bg", "developer", {"wake_failed": True}, self.generation(snapshot))
        self.assertIn("delivery_evidence", str(caught.exception))


del Base  # the fixture only; its own tests run in test_claude_bg_delivery_gate
