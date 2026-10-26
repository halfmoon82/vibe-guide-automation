"""Issue #278: stuck-session repair folds the manual recovery checklist into
``vibe repair`` / ``vibe requeue`` (``Monitor.repair_node``).

Business contract: after a developer session is proven stopped externally
(stopped/failed/archived binding) one ``vibe repair --node <id>`` must mark the task
binding failed, release the (possibly quarantined) writer lease, retire the
dead handle, clear the quarantine and queue a same-issue successor dispatch;
``vibe resume`` then starts the successor without manual state.json edits.
Missing pieces are handled individually -- repair never reports a cleanup it
did not do -- and a session whose binding is still live keeps the same-task
retry path instead of being parked.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import build_authorization_card, authorize
from vibe_guide.models import AgentCapabilities, DAGNode, Plan, node_branch
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    acquire_writer_lease,
    load_snapshot,
    quarantine_writer_lease,
    read_writer_lease,
    save_snapshot,
)
from vibe_guide.task_registry import load_task_binding, save_task_binding
from vibe_guide.capability_contract import build_contract, save_contract


def _node(node_id):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "g1",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "worker_profile": {
                "worker": "codex", "model": "test", "reasoning": "normal",
                "fallbacks": [],
                "selection_basis": {
                    "issue_complexity_ref": node_id,
                    "complexity_band": "standard",
                    "risk_tags": [],
                    "availability_evidence": "test",
                },
                "writer": "writer",
                "worktree": ".worktrees/" + node_id,
                "branch": "branch-" + node_id,
                "allowlist": [node_id + ".py"],
            },
        },
        "ready",
    )


class StuckRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n',
            encoding="utf-8",
        )
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities(
            "fake", True, True, True, True, True, "full"
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _authorized_monitor(self, nodes):
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        card = build_authorization_card(plan, nodes, self.capabilities)
        return Monitor(self.paths, plan, nodes), authorize(card, "AUTHORIZE")

    def _running_snapshot(self):
        """Start n1 so the run has a live handle, lease and task binding."""
        monitor, record = self._authorized_monitor([_node("n1")])
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        current = snapshot.nodes["n1"]
        self.assertIsInstance(current.get("active_task"), dict)
        self.assertIn("n1", snapshot.handles)
        return monitor, runner, snapshot

    def _stop_binding(self, snapshot, status="stopped"):
        binding = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        binding.status = status
        save_task_binding(self.paths, binding)
        return binding

    def test_stopped_session_repair_does_full_cleanup_and_queues_successor(self):
        monitor, runner, snapshot = self._running_snapshot()
        binding = self._stop_binding(snapshot, "stopped")
        lease = read_writer_lease(
            self.paths, "n1", ".worktrees/n1"
        )
        self.assertIsNotNone(lease)

        result = monitor.repair_node("n1", snapshot.run_id)

        self.assertEqual(result["kind"], "stuck_session")
        self.assertTrue(result["steps"]["binding_failed"])
        self.assertTrue(result["steps"]["lease_released"])
        self.assertTrue(result["steps"]["handle_retired"])
        persisted = load_snapshot(self.paths, snapshot.run_id)
        current = persisted.nodes["n1"]
        # state.json tasks map carries the failed binding.
        self.assertEqual(
            persisted.tasks["n1:developer"]["status"], "failed"
        )
        self.assertNotIn("n1", persisted.handles)
        self.assertIsNone(current.get("active_task"))
        self.assertIsNone(current.get("quarantine"))
        self.assertIsNone(
            read_writer_lease(self.paths, "n1", ".worktrees/n1")
        )
        retry = current["retryable_action"]
        self.assertTrue(retry["pending_schedule"])
        self.assertTrue(retry["successor"])
        self.assertEqual(retry["role"], "developer")
        self.assertEqual(retry["predecessor_task_id"], binding.task_id)

        # The queued successor dispatches on the next resume under a fresh
        # generation/task identity.
        with patch("vibe_guide.monitor.require_entry", return_value=None):
            resumed = monitor.resume(snapshot.run_id, runner)
        self.assertEqual(resumed.nodes["n1"]["status"], "running")
        successor = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        self.assertEqual(successor.successor_of, binding.task_id)
        self.assertNotEqual(successor.task_id, binding.task_id)

    def test_quarantined_dead_session_is_cleared_and_rescheduled(self):
        monitor, runner, snapshot = self._running_snapshot()
        self._stop_binding(snapshot, "stopped")
        current = snapshot.nodes["n1"]
        handle_id = snapshot.handles["n1"]
        current["status"] = "blocked_unknown"
        current["quarantine"] = {
            "run_id": snapshot.run_id,
            "handle_id": handle_id,
            "reason": "evidence",
        }
        save_snapshot(self.paths, snapshot)
        quarantine_writer_lease(
            self.paths, "n1", ".worktrees/n1", snapshot.run_id, "stuck"
        )

        result = monitor.repair_node("n1", snapshot.run_id)

        self.assertTrue(result["steps"]["quarantine_cleared"])
        persisted = load_snapshot(self.paths, snapshot.run_id)
        self.assertIsNone(persisted.nodes["n1"]["quarantine"])
        self.assertTrue(
            persisted.nodes["n1"]["retryable_action"]["pending_schedule"]
        )

    def test_live_binding_keeps_same_task_path(self):
        monitor, runner, snapshot = self._running_snapshot()
        before = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )

        # Persistence redacts provider text; inject the observed reason in
        # memory as the supervisor does when requesting a same-task retry.
        original = monitor._recovery_snapshot

        def with_raw_reason(node_id, run_id=None):
            loaded = original(node_id, run_id)
            loaded.nodes[node_id]["reason"] = "connection timed out"
            return loaded

        with patch.object(monitor, "_recovery_snapshot", side_effect=with_raw_reason):
            result = monitor.repair_node("n1", snapshot.run_id)

        persisted = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(persisted.nodes["n1"]["status"], "retry_pending")
        retry = persisted.nodes["n1"]["retryable_action"]
        self.assertTrue(retry["same_task_required"])
        self.assertFalse(retry["successor"])
        self.assertNotIn("manual_repair_required", retry)
        self.assertNotEqual(result["kind"], "stuck_session")
        after = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        self.assertEqual(after.status, before.status)
        self.assertEqual(after.task_id, before.task_id)

    def test_missing_pieces_are_tolerated(self):
        monitor, runner, snapshot = self._running_snapshot()
        self._stop_binding(snapshot, "stopped")
        # Partial cleanup already done by hand: handle popped, lease released.
        snapshot.handles.pop("n1", None)
        from vibe_guide.state import release_writer_lease

        release_writer_lease(
            self.paths, "n1", ".worktrees/n1", snapshot.run_id
        )
        current = snapshot.nodes["n1"]
        current["active_task"] = None
        current["quarantine"] = {
            "run_id": snapshot.run_id, "handle_id": None, "reason": "leftover"
        }
        save_snapshot(self.paths, snapshot)

        result = monitor.repair_node("n1", snapshot.run_id)

        self.assertTrue(result["steps"]["lease_released"])
        self.assertTrue(result["steps"]["quarantine_cleared"])
        persisted = load_snapshot(self.paths, snapshot.run_id)
        self.assertIsNone(persisted.nodes["n1"]["quarantine"])
        self.assertEqual(
            persisted.tasks["n1:developer"]["status"], "failed"
        )

    def test_corrupt_registry_parks_instead_of_fail_open(self):
        """P1: an unreadable tasks.json is not "dead" -- repair must fail
        closed (blocked_unknown / reconcile_binding) without touching the
        lease, handle, quarantine, or queueing a successor beside a possibly
        live writer."""
        monitor, runner, snapshot = self._running_snapshot()
        tasks_path = (
            self.paths.vibe / "runs" / snapshot.run_id / "tasks.json"
        )
        self.assertTrue(tasks_path.exists())
        # Corrupt the registry after the session started.
        tasks_path.write_text("{not json", encoding="utf-8")

        result = monitor.repair_node("n1", snapshot.run_id)

        self.assertEqual(result["kind"], "stuck_session")
        self.assertEqual(result["status"], "blocked_unknown")
        self.assertEqual(result["recovery_action"], "reconcile_binding")
        self.assertFalse(result["steps"]["binding_failed"])
        self.assertFalse(result["steps"]["lease_released"])
        self.assertFalse(result["steps"]["handle_retired"])
        persisted = load_snapshot(self.paths, snapshot.run_id)
        current = persisted.nodes["n1"]
        self.assertEqual(current["status"], "blocked_unknown")
        retry = current["retryable_action"]
        self.assertTrue(retry["manual_repair_required"])
        self.assertFalse(retry["successor"])
        # Nothing was cleaned: the handle and active task survive so a human
        # reconcile still sees the real marks.
        self.assertIn("n1", persisted.handles)
        self.assertIsInstance(current.get("active_task"), dict)
        self.assertIsNotNone(
            read_writer_lease(self.paths, "n1", ".worktrees/n1")
        )

    def test_nonterminal_binding_never_authorizes_successor_cleanup(self):
        monitor, runner, snapshot = self._running_snapshot()
        for status in ("blocked_unknown", "blocked_design", "delivered", "accepted"):
            with self.subTest(status=status):
                binding = self._stop_binding(snapshot, status)
                handle = snapshot.handles["n1"]
                monitor.repair_node("n1", snapshot.run_id)
                persisted = load_snapshot(self.paths, snapshot.run_id)
                self.assertEqual(persisted.handles.get("n1"), handle)
                self.assertIsNotNone(persisted.nodes["n1"].get("active_task"))
                self.assertIsNotNone(read_writer_lease(self.paths, "n1", ".worktrees/n1"))
                after = load_task_binding(self.paths, "n1", "developer", run_id=snapshot.run_id)
                self.assertNotIn(after.status, ("stopped", "failed", "archived"))
                self.assertEqual(after.task_id, binding.task_id)
                self.assertFalse((persisted.nodes["n1"].get("retryable_action") or {}).get("successor"))

    def test_repair_preserves_delivered_same_task_recovery(self):
        monitor, runner, snapshot = self._running_snapshot()
        binding = self._stop_binding(snapshot, "delivered")
        current = snapshot.nodes["n1"]
        handle = snapshot.handles.pop("n1")
        current.update(
            status="blocked_unknown", active_role=None, active_task=None,
            start_intent=None, retryable_action=None, reviewer_started=False,
            quarantine={"run_id": snapshot.run_id, "handle_id": handle,
                        "reason": "interrupted authorized continuation"},
        )
        save_snapshot(self.paths, snapshot)
        monitor.repair_node("n1", snapshot.run_id)
        with patch("vibe_guide.monitor.require_entry", return_value=None):
            resumed = monitor.resume(snapshot.run_id, runner)
        self.assertEqual(resumed.nodes["n1"]["status"], "rework")
        self.assertEqual(len(runner.start_calls), 2)
        self.assertTrue(runner.start_calls[-1]["continuation"])
        self.assertFalse(runner.start_calls[-1]["successor"])
        after = load_task_binding(self.paths, "n1", "developer", run_id=snapshot.run_id)
        self.assertEqual(after.task_id, binding.task_id)

    def test_repair_preserves_existing_delivered_continuation(self):
        monitor, runner, snapshot = self._running_snapshot()
        binding = self._stop_binding(snapshot, "delivered")
        current = snapshot.nodes["n1"]
        snapshot.handles.pop("n1")
        retry = {"role": "developer", "phase": "rework", "continuation": True,
                 "pending_schedule": True, "successor": False}
        current.update(status="blocked_unknown", active_role=None, active_task=None,
                       start_intent=None, retryable_action=retry, reviewer_started=False,
                       pair_archived=True,
                       quarantine={"run_id": snapshot.run_id, "handle_id": None,
                                   "reason": "interrupted authorized continuation"})
        save_snapshot(self.paths, snapshot)
        monitor.repair_node("n1", snapshot.run_id)
        persisted = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(persisted.nodes["n1"]["retryable_action"], retry)
        with patch("vibe_guide.monitor.require_entry", return_value=None):
            resumed = monitor.resume(snapshot.run_id, runner)
        self.assertEqual(resumed.nodes["n1"]["status"], "rework")
        self.assertTrue(runner.start_calls[-1]["continuation"])
        self.assertFalse(runner.start_calls[-1]["successor"])
        self.assertEqual(load_task_binding(self.paths, "n1", "developer",
                                          run_id=snapshot.run_id).task_id, binding.task_id)

    def test_stale_failed_binding_cannot_retire_current_writer(self):
        monitor, runner, snapshot = self._running_snapshot()
        self._stop_binding(snapshot, "failed")
        current = snapshot.nodes["n1"]
        current["developer_generation"] += 1
        current["active_task"]["generation"] = current["developer_generation"]
        save_snapshot(self.paths, snapshot)
        handle = snapshot.handles["n1"]

        result = monitor.repair_node("n1", snapshot.run_id)

        persisted = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(result["status"], "blocked_unknown")
        self.assertEqual(persisted.handles.get("n1"), handle)
        self.assertEqual(persisted.nodes["n1"]["active_task"], current["active_task"])
        self.assertIsNotNone(read_writer_lease(self.paths, "n1", ".worktrees/n1"))
        self.assertFalse(persisted.nodes["n1"]["retryable_action"].get("successor"))

    def test_binding_retirement_failure_preserves_all_writer_marks(self):
        monitor, runner, snapshot = self._running_snapshot()
        self._stop_binding(snapshot, "stopped")
        active = dict(snapshot.nodes["n1"]["active_task"])
        handle = snapshot.handles["n1"]
        with patch.object(monitor, "_set_binding_status", side_effect=OSError("registry unavailable")):
            result = monitor.repair_node("n1", snapshot.run_id)
        persisted = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(result["status"], "blocked_unknown")
        self.assertEqual(persisted.handles.get("n1"), handle)
        self.assertEqual(persisted.nodes["n1"]["active_task"], active)
        self.assertIsNotNone(read_writer_lease(self.paths, "n1", ".worktrees/n1"))
        self.assertFalse(persisted.nodes["n1"]["retryable_action"].get("successor"))


if __name__ == "__main__":
    unittest.main()
