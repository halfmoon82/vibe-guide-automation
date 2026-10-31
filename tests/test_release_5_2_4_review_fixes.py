"""Regression tests for the independent PR #370 review findings."""
import os
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vibe_guide.decision_boundary import ingest_worker_decisions, raise_reusable_decision
from vibe_guide.adapters.task_provider import ProviderUnavailable
from vibe_guide.paths import ProjectPaths
from vibe_guide.scope_landing import ScopeLandingUnknown, landing_report
from vibe_guide.state import load_snapshot

from tests.test_decision_reuse_338 import DecisionReuseTests, _QUESTION
from tests.test_delivery_self_report_shape import SelfReportShapeGateTests
from tests.test_landing_required_before_review import EmptyLandingGateTests
from tests.test_model_fallback_retry import (
    ModelFallbackDispatchTests, _fallback_node, _write_codex_map,
)


class GitUnreadableLandingTests(unittest.TestCase):
    def test_git_checkout_with_unreadable_head_fails_closed(self):
        monitor = SimpleNamespace(paths=ProjectPaths(self.root))
        (self.root / ".git").mkdir()
        worktree = self.root / "node-worktree"
        worktree.mkdir()
        snapshot = SimpleNamespace(nodes={"n1": {"worktree": "node-worktree"}})
        failed = subprocess.CompletedProcess(["git"], 128, stdout="", stderr="unsafe repository")
        with mock.patch("vibe_guide.scope_landing._git_in", return_value=lambda args: failed):
            with self.assertRaises(ScopeLandingUnknown):
                landing_report(monitor, snapshot, "n1")

    def setUp(self):
        self.root = Path(self.enterContext(__import__("tempfile").TemporaryDirectory()))


class EmptyLandingAllowedReplayTests(EmptyLandingGateTests):
    def test_empty_landing_allowed_replays_without_manual_reconciliation(self):
        crashed = load_snapshot(self.paths, self.snapshot.run_id)
        active = crashed.nodes["bg"]["active_task"]
        self.monitor()._record(
            crashed, "empty_landing_allowed",
            {"run_id": crashed.run_id, "node_id": "bg", "role": "developer",
             "generation": 1, "reason": "contract allow_empty_landing: true"},
            active,
        )
        crashed = load_snapshot(self.paths, self.snapshot.run_id)
        self.monitor()._reconcile_unapplied_events(crashed)
        self.assertNotEqual(crashed.nodes["bg"]["status"], "blocked_unknown")
        self.assertNotEqual(crashed.nodes["bg"].get("reason"),
                            "unapplied event needs manual reconciliation")


class ModelFallbackReplayTests(ModelFallbackDispatchTests):
    def test_model_map_fallback_replays_the_complete_resolved_chain(self):
        _write_codex_map(Path(os.environ["VIBE_HOME"]), {
            "standard": ["m1", "m2", "m3"],
        })
        node = _fallback_node("n1", model="default", fallbacks=())
        node.contract["adapter_id"] = "codex"
        monitor, _runner, snapshot = self._start_fallback_run(node, [None])
        crashed = load_snapshot(self.paths, snapshot.run_id)
        current = crashed.nodes["n1"]
        contract = dict(node.contract)
        monitor._apply_model_tier(crashed, node, contract, current, "developer")
        outcome = monitor._plan_model_fallback(
            crashed, "n1", contract, current, "developer", "develop", False,
            ProviderUnavailable("quota exhausted for model m1"),
        )
        self.assertEqual(outcome, "switch")

        crashed = load_snapshot(self.paths, snapshot.run_id)
        monitor._reconcile_unapplied_events(crashed)
        current = crashed.nodes["n1"]
        profile = current["contract_overrides"]["worker_profile"]
        self.assertEqual(profile["model"], "m2")
        self.assertEqual(profile["fallbacks"], [{"model": "m3"}])
        self.assertEqual(current["model_dispatch_chain"], ["m1", "m2", "m3"])

    def test_model_fallback_used_replays_profile_switch(self):
        monitor, _runner, snapshot = self._start_fallback_run(_fallback_node("n1"), [None])
        crashed = load_snapshot(self.paths, snapshot.run_id)
        active = crashed.nodes["n1"]["active_task"]
        monitor._record(
            crashed, "model_fallback_used",
            {"run_id": crashed.run_id, "node_id": "n1", "role": "developer",
             "proof": {"from_model": "k1", "to_model": "k2",
                       "reason_class": "quota_exhausted"}},
            active,
        )
        crashed = load_snapshot(self.paths, snapshot.run_id)
        monitor._reconcile_unapplied_events(crashed)
        current = crashed.nodes["n1"]
        self.assertNotEqual(current["status"], "blocked_unknown")
        self.assertEqual(current["contract_overrides"]["worker_profile"]["model"], "k2")

    def test_model_unavailable_parked_replays_bounded_park(self):
        monitor, _runner, snapshot = self._start_fallback_run(_fallback_node("n1"), [None])
        crashed = load_snapshot(self.paths, snapshot.run_id)
        active = crashed.nodes["n1"]["active_task"]
        attempts = [{"model": "k1", "reason_class": "quota_exhausted", "reason": "quota"}]
        monitor._record(
            crashed, "model_unavailable_parked",
            {"run_id": crashed.run_id, "node_id": "n1", "role": "developer",
             "proof": {"rounds": 2, "attempts": attempts}},
            active,
        )
        crashed = load_snapshot(self.paths, snapshot.run_id)
        monitor._reconcile_unapplied_events(crashed)
        current = crashed.nodes["n1"]
        self.assertEqual(current["status"], "retry_pending")
        self.assertIsNone(current["retryable_action"])
        self.assertEqual(current["model_unavailable"], {
            "rounds": 2,
            "attempts": [{"model": "k1", "reason_class": "quota_exhausted",
                          "reason": "[REDACTED_PROVIDER_TEXT]"}],
        })
        self.assertIsNone(current["active_task"])


class RunScopeDecisionTests(DecisionReuseTests):
    def test_run_scope_does_not_reuse_same_node_node_scoped_answer(self):
        records, _ = ingest_worker_decisions(self.paths, "run-1", "g1", [{
            "question": _QUESTION, "options": ["要", "不要"],
        }])
        self._answer(records[0])
        second = raise_reusable_decision(
            self.paths, run_id="run-1", node_id="g1",
            question=_QUESTION, options=["要", "不要"], scope="run")
        self.assertFalse(second["reused"])
        self.assertEqual(second["request"]["scope"], "run")


class MinimalBlockedReportTests(SelfReportShapeGateTests):
    def test_blocked_engineering_needs_no_completion_evidence(self):
        outcome = self.deliver({"event": "blocked_engineering"})
        self.assertTrue(outcome["recorded"])
        self.assertEqual(len(self.archived()), 1)


if __name__ == "__main__":
    unittest.main()
