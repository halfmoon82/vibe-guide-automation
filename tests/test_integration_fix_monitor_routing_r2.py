"""R2-3: the lease guard runs before any mailbox write or notification.

`_guard_lease` is supplied by the parallel supervisor-lease node; when it is
absent the check is skipped.  A superseded supervisor must not leave a
decision request or push a notification behind.
"""
import unittest
from unittest.mock import Mock, patch

from vibe_guide.decision_boundary import NotificationBridge
from vibe_guide.state import load_events, load_snapshot
from tests.test_integration_fix_monitor_routing import _Base


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
        monitor._guard_lease = Mock(side_effect=PermissionError("supervisor lease superseded"))
        with self.spy_notify() as spy:
            with self.assertRaisesRegex(PermissionError, "superseded"):
                monitor._ensure_blocked_design_decision(live, "n1")
        monitor._guard_lease.assert_called_once_with(live.run_id)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(self.request_files(), [])
        self.assertEqual(live.nodes["n1"]["status"], "blocked_design")
        self.assertFalse(live.nodes["n1"].get("decision_ids"))
        self.assertEqual(len(load_events(self.paths, snapshot.run_id)), events_before)

    def test_superseded_guard_also_blocks_the_tick_route(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="integration_scope_review")
        monitor._guard_lease = Mock(side_effect=PermissionError("supervisor lease superseded"))
        with self.spy_notify() as spy:
            with self.assertRaisesRegex(PermissionError, "superseded"):
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
        monitor._guard_lease.assert_called_once_with(live.run_id)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(len(self.request_files()), 1)

    def test_guard_is_checked_before_the_cause_check(self):
        monitor, runner, snapshot = self.undecided()  # no cause
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._guard_lease = Mock(side_effect=PermissionError("supervisor lease superseded"))
        with self.assertRaises(PermissionError):
            monitor._ensure_blocked_design_decision(live, "n1")


if __name__ == "__main__":
    unittest.main()
