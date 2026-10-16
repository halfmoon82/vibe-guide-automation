"""R2-3: the lease guard runs before any mailbox write or notification.

`_guard_lease` is supplied by the parallel supervisor-lease node; when it is
absent the check is skipped.  A superseded supervisor must not leave a
decision request or push a notification behind.
"""
import unittest
from unittest.mock import Mock, call, patch

from vibe_guide.decision_boundary import NotificationBridge
from vibe_guide.state import load_events, load_snapshot
from vibe_guide.authorization import authorize
from vibe_guide.runners.fake import FakeRunner
from tests import test_auto_scope_files as fx
from tests.test_integration_fix_monitor_routing import _Base


class _LeaseConflict(ValueError):
    """Mirrors supervisor.SupervisorLeaseConflict (a ValueError subclass).

    A ``except ValueError`` around the guarded call must not swallow it.
    """


class LeaseGuardOrderTests(_Base):
    def spy_notify(self):
        return patch.object(
            NotificationBridge, "notify", autospec=True,
            side_effect=lambda bridge, record: {"status": "accepted"},
        )

    def request_files(self):
        directory = self.paths.vibe / "decisions" / "requests"
        return sorted(directory.glob("dec-*.json")) if directory.is_dir() else []

    def test_superseded_guard_blocks_write_and_notify(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        live = load_snapshot(self.paths, snapshot.run_id)
        events_before = len(load_events(self.paths, snapshot.run_id))
        monitor._guard_lease = Mock(side_effect=_LeaseConflict("supervisor lease superseded"))
        with self.spy_notify() as spy:
            with self.assertRaisesRegex(_LeaseConflict, "superseded"):
                monitor._ensure_blocked_design_decision(live, "n1")
        monitor._guard_lease.assert_called_once_with(live.run_id)
        self.assertEqual(monitor._guard_lease.call_count, 1)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(self.request_files(), [])
        self.assertEqual(live.nodes["n1"]["status"], "blocked_design")
        self.assertFalse(live.nodes["n1"].get("decision_ids"))
        self.assertEqual(len(load_events(self.paths, snapshot.run_id)), events_before)

    def test_superseded_guard_also_blocks_the_tick_route(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="integration_scope_review")
        monitor._guard_lease = Mock(side_effect=_LeaseConflict("supervisor lease superseded"))
        with self.spy_notify() as spy:
            with self.assertRaisesRegex(_LeaseConflict, "superseded"):
                monitor.tick(snapshot.run_id, runner)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(self.request_files(), [])
        self.assertFalse([e for e in load_events(self.paths, snapshot.run_id)
                          if e["event"] == "decision_raised"])

    def test_noop_guard_positive_control_one_request_one_notify(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._guard_lease = Mock(return_value=None)
        with self.spy_notify() as spy:
            self.assertTrue(monitor._ensure_blocked_design_decision(live, "n1"))
        # On the merged tree _record also guards, so only the first call is
        # this function's own pre-write check.
        self.assertGreaterEqual(monitor._guard_lease.call_count, 1)
        self.assertEqual(monitor._guard_lease.call_args_list[0], call(live.run_id))
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(len(self.request_files()), 1)

    def test_guard_is_checked_before_the_cause_check(self):
        monitor, runner, snapshot = self.undecided()  # no cause
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._guard_lease = Mock(side_effect=_LeaseConflict("supervisor lease superseded"))
        with self.assertRaises(_LeaseConflict):
            monitor._ensure_blocked_design_decision(live, "n1")


class ExternalAuthorizationGuardTests(_Base):
    """_mark_external_authorization_required checks the lease before mutating."""

    def started(self):
        node = fx._node("n1")
        plan = fx.Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        card = fx.build_authorization_card(plan, [node], self.capabilities)
        monitor = fx.Monitor(self.paths, plan, [node])
        runner = FakeRunner(events={("n1", "developer"): [
            ("failed", {"reason": "login required", "credential_required": True})]})
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        return monitor, runner, snapshot

    def lease_files(self):
        return sorted(p for p in self.paths.vibe.rglob("*lease*") if p.is_file())

    def test_superseded_guard_leaves_lease_binding_and_node_untouched(self):
        monitor, runner, snapshot = self.started()
        before_binding = monitor._load_task_binding(snapshot, "n1", "developer").status
        before_leases = self.lease_files()
        events_before = len(load_events(self.paths, snapshot.run_id))
        monitor._guard_lease = Mock(side_effect=_LeaseConflict("supervisor lease superseded"))
        with patch.object(monitor, "_release_node_lease") as release:
            with self.assertRaisesRegex(_LeaseConflict, "superseded"):
                monitor.tick(snapshot.run_id, runner)
        release.assert_not_called()
        self.assertEqual(monitor._guard_lease.call_args_list[0], call(snapshot.run_id))
        on_disk = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(monitor._load_task_binding(on_disk, "n1", "developer").status, before_binding)
        self.assertEqual(self.lease_files(), before_leases)
        self.assertEqual(on_disk.nodes["n1"]["status"], snapshot.nodes["n1"]["status"])
        self.assertFalse([e for e in load_events(self.paths, snapshot.run_id)[events_before:]
                          if e["event"] == "blocked_design"])

    def test_direct_call_raises_before_any_mutation(self):
        monitor, runner, snapshot = self.started()
        live = load_snapshot(self.paths, snapshot.run_id)
        before = dict(live.nodes["n1"])
        monitor._guard_lease = Mock(side_effect=_LeaseConflict("supervisor lease superseded"))
        with patch.object(monitor, "_release_node_lease") as release, \
                patch.object(monitor, "_set_binding_status") as binding:
            with self.assertRaises(_LeaseConflict):
                monitor._mark_external_authorization_required(live, "n1", "login required")
        release.assert_not_called()
        binding.assert_not_called()
        self.assertEqual(monitor._guard_lease.call_count, 1)
        self.assertEqual(live.nodes["n1"], before)

    def test_noop_guard_positive_control_parks_on_external_authorization(self):
        monitor, runner, snapshot = self.started()
        monitor._guard_lease = Mock(return_value=None)
        ticked = monitor.tick(snapshot.run_id, runner)
        current = ticked.nodes["n1"]
        self.assertEqual(current["status"], "blocked_design")
        self.assertEqual(current["recovery_action"], "await_external_decision")
        self.assertEqual(monitor._guard_lease.call_args_list[0], call(snapshot.run_id))


if __name__ == "__main__":
    unittest.main()
