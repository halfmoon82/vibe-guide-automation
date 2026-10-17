"""Dispatch must honour the effective capacity; counter drift self-heals.

Replays issue #225: capacity 5, six ready nodes.  The dispatch gate and the
topology validation must count the same thing, and a counter that disagrees
with the task registry is corrected from the registry instead of voiding the
authorization.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderPending
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.models import AgentCapabilities, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events, load_snapshot, save_snapshot
from vibe_guide.task_registry import TaskBinding

from tests.test_monitor import node


class PendingBridgeRunner(FakeRunner):
    """Async desktop bridge: every create is answered with ProviderPending."""

    def task_binding(self, contract, worktree, run_id, status):
        raise ProviderPending("provider capability bridge is pending")


class CapacityFixture(unittest.TestCase):
    NODE_COUNT = 6
    LIMIT = 5

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(self.paths, build_contract(self.paths.root, provider="fake", host_id="local"))
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
        self.node_ids = ["n{}".format(i) for i in range(1, self.NODE_COUNT + 1)]

    def monitor(self):
        nodes = [node(node_id) for node_id in self.node_ids]
        plan = Plan("plan-1", 1, "docs/prd.md", self.node_ids, "draft")
        with patch("vibe_guide.dag._write_scope_conflict_errors", return_value={}):
            card = build_authorization_card(
                plan, nodes, self.capabilities, active_pair_limit=self.LIMIT
            )
        return Monitor(self.paths, plan, nodes), authorize(card, "AUTHORIZE")

    def events(self, snapshot, name):
        return [e for e in load_events(self.paths, snapshot.run_id) if e["event"] == name]


class DispatchCapacityTests(CapacityFixture):
    def test_six_ready_nodes_dispatch_five(self):
        monitor, record = self.monitor()
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        self.assertEqual(len(runner.start_calls), 5)
        self.assertEqual(snapshot.nodes["n6"]["status"], "planned")
        self.assertEqual(snapshot.capacity, 5)
        self.assertEqual(snapshot.active_concurrency, 5)

    def test_pending_bridge_never_exceeds_capacity(self):
        monitor, record = self.monitor()
        runner = PendingBridgeRunner()
        snapshot = monitor.start(record, runner)
        occupied = [
            n for n, c in snapshot.nodes.items()
            if c.get("retryable_action") is not None or c.get("active_task")
        ]
        self.assertLessEqual(len(occupied), 5, occupied)


class CounterSelfHealTests(CapacityFixture):
    NODE_COUNT = 6

    def _started(self):
        monitor, record = self.monitor()
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        return monitor, runner, snapshot

    def _validate_as_complex(self, monitor, snapshot):
        """Topology validation only runs for complex plans; flip the band for one call."""
        monitor.plan.complexity_band = "complex"
        try:
            monitor._validate_execution_topology(snapshot)
        finally:
            monitor.plan.complexity_band = ""

    def test_stale_counter_is_corrected_from_registry_not_voided(self):
        monitor, runner, snapshot = self._started()
        snapshot.active_concurrency = 6  # issue #225: counter ahead of capacity
        monitor._reconcile_capacity(snapshot)
        self.assertEqual(snapshot.active_concurrency, 5)
        corrections = self.events(snapshot, "capacity_counter_corrected")
        self.assertEqual(len(corrections), 1)
        self.assertEqual(corrections[0]["data"]["active_concurrency"], 5)
        self._validate_as_complex(monitor, snapshot)  # must not raise

    def test_resume_after_stale_counter_does_not_report_capacity_drift(self):
        monitor, runner, snapshot = self._started()
        stored = load_snapshot(self.paths, snapshot.run_id)
        stored.active_concurrency = 6
        save_snapshot(self.paths, stored)
        resumed = monitor.resume(snapshot.run_id, runner)
        self.assertEqual(resumed.active_concurrency, 5)
        self.assertEqual(len(runner.start_calls), 5)
        self.assertNotEqual(resumed.status, "blocked_design")
        self.assertEqual(resumed.nodes["n6"]["status"], "planned")

    def test_over_capacity_registry_pauses_dispatch_without_voiding(self):
        monitor, runner, snapshot = self._started()
        # An extra live session beyond capacity (6th registered, not started).
        extra = TaskBinding(
            provider="fake", mode="background", issue_id="n6", role="developer",
            task_id="developer:n6", worktree=".worktrees/n6", branch="branch-n6",
            run_id=snapshot.run_id, status="running", generation=1,
        )
        from vibe_guide.task_registry import save_task_binding
        save_task_binding(self.paths, extra)
        monitor._reconcile_capacity(snapshot)
        self._validate_as_complex(monitor, snapshot)  # not capacity drift
        self.assertEqual(snapshot.active_concurrency, 6)
        self.assertEqual(len(runner.start_calls), 5)

    def test_slot_frees_after_archive_then_sixth_dispatches(self):
        monitor, runner, snapshot = self._started()
        self.assertEqual(snapshot.nodes["n6"]["status"], "planned")
        from vibe_guide.task_registry import load_task_binding, save_task_binding
        done = load_task_binding(self.paths, "n1", "developer", run_id=snapshot.run_id)
        done.status = "stopped"
        save_task_binding(self.paths, done)
        stored = load_snapshot(self.paths, snapshot.run_id)
        stored.nodes["n1"]["status"] = "stopped"
        stored.nodes["n1"]["active_task"] = None
        stored.handles.pop("n1", None)
        save_snapshot(self.paths, stored)
        monitor._reset_binding_cache()
        after = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(after.nodes["n6"]["status"], "running")
        self.assertEqual(len(runner.start_calls), 6)


class UnreadableRegistryTests(CapacityFixture):
    def test_unreadable_registry_holds_dispatch_as_blocked_unknown(self):
        monitor, record = self.monitor()
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        tasks = self.paths.root / ".vibe" / "runs" / snapshot.run_id / "tasks.json"
        tasks.write_text("{not json", encoding="utf-8")
        monitor._reset_binding_cache()
        before = len(runner.start_calls)
        after = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(after.status, "blocked_unknown")
        self.assertEqual(len(runner.start_calls), before)
        reports = self.events(after, "capacity_registry_unknown")
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["data"]["change_reason"], "capacity_registry_unknown")

    def test_contradictory_registry_is_unknown_not_guessed(self):
        monitor, record = self.monitor()
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        tasks = self.paths.root / ".vibe" / "runs" / snapshot.run_id / "tasks.json"
        payload = json.loads(tasks.read_text(encoding="utf-8"))
        twin = dict(payload["bindings"][0], task_id="developer:twin", threadId="developer:twin", platform_task_id="developer:twin")
        payload["bindings"].append(twin)
        tasks.write_text(json.dumps(payload), encoding="utf-8")
        occupied, unknown = monitor._occupied_nodes(snapshot)
        self.assertIsNotNone(unknown)
