"""Bounded model fallback retry along the dispatch profile's model chain.

Covers the model-routing node contract:
- a model-class provider failure (quota exhausted / rate limit / model
  unavailable) moves dispatch to the next chain model, each model exactly
  once, at most chain-length attempts per round;
- every switch records a ``model_fallback_used`` event (node_id, from model,
  to model, reason class) and never marks the node blocked;
- a fully exhausted chain fails closed as a *retryable* ``model_unavailable``
  whose reason lists every model's own failure;
- non-model failures never consume the chain;
- switching models never changes worktree, branch, file scope or the
  authorization digest;
- ``WorkerProfile.fallbacks`` is populated from ``model_catalog.resolve``
  (model_map config) at dispatch-profile build time.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide import model_catalog
from vibe_guide.adapters.task_provider import ProviderUnavailable
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.models import (
    AgentCapabilities,
    DAGNode,
    IssueComplexity,
    LocalModel,
    Plan,
    WorkerProfile,
)
from vibe_guide.monitor import Monitor, model_failure_reason_class
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events

from tests.test_integrated_prd_acceptance import V42_STATE, _isolate_user_dirs, _ProjectCase


class _FlakyRunner(FakeRunner):
    """FakeRunner whose start() raises the queued exceptions in order."""

    def __init__(self, outcomes, events=None):
        super().__init__(events=events)
        self._outcomes = list(outcomes)

    def start(self, contract, worktree):
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if isinstance(outcome, BaseException):
                # Record the attempt too: a failed dispatch still consumed
                # this model's single try.
                self.start_calls.append(dict(contract, worktree=str(worktree)))
                raise outcome
        return super().start(contract, worktree)


def _fallback_node(node_id, model="k1", fallbacks=("k2", "k3"), files=None):
    files = list(files or [node_id + ".py"])
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "g1",
        {
            "files": files,
            "worker": "worker-" + node_id,
            "reviewer_worker": "reviewer-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "worker_profile": {
                "worker": "codex",
                "model": model,
                "reasoning": "normal",
                "fallbacks": [{"model": name} for name in fallbacks],
                "selection_basis": {
                    "issue_complexity_ref": node_id,
                    "complexity_band": "standard",
                    "risk_tags": [],
                    "availability_evidence": "test",
                },
                "writer": "writer",
                "worktree": ".worktrees/" + node_id,
                "branch": "branch-" + node_id,
                "allowlist": files,
            },
        },
        "ready",
    )


class ModelFailureClassTests(unittest.TestCase):
    def test_quota_rate_and_unavailable_classes(self):
        self.assertEqual(model_failure_reason_class("quota exhausted for model k1"), "quota_exhausted")
        self.assertEqual(model_failure_reason_class("Error 429: rate limit reached"), "rate_limit")
        self.assertEqual(model_failure_reason_class("unsupported model k3"), "model_unavailable")
        self.assertEqual(model_failure_reason_class("model unavailable on this host"), "model_unavailable")

    def test_non_model_failures_have_no_class(self):
        for text in ("writer identity conflict", "detached HEAD", "timeout", "", None):
            with self.subTest(text=text):
                self.assertIsNone(model_failure_reason_class(text))


class ModelFallbackDispatchTests(_ProjectCase):
    state = V42_STATE

    def _start_fallback_run(self, node, outcomes):
        plan = Plan("plan-1", 1, "docs/prd.md", [node.id], "draft")
        card = build_authorization_card(plan, [node], AgentCapabilities("fake", True, True, True, True, True, "full"))
        runner = _FlakyRunner(outcomes)
        monitor = Monitor(self.paths, plan, [node])
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        return monitor, runner, snapshot

    def test_quota_exhaustion_switches_to_next_model_and_succeeds(self):
        node = _fallback_node("n1")
        monitor, runner, snapshot = self._start_fallback_run(
            node,
            [ProviderUnavailable("quota exhausted for model k1"), None],
        )
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        self.assertEqual(len(runner.start_calls), 2)
        first, second = runner.start_calls
        self.assertEqual(first["worker_profile"]["model"], "k1")
        self.assertEqual(second["worker_profile"]["model"], "k2")
        # The switch never touches scope, worktree, branch or authorization.
        self.assertEqual(first["worktree"], second["worktree"])
        self.assertEqual(first["branch"], second["branch"])
        self.assertEqual(first["files"], second["files"])
        self.assertEqual(first["authorization_digest"], second["authorization_digest"])

        events = load_events(self.paths, snapshot.run_id)
        fallbacks = [e for e in events if e["event"] == "model_fallback_used"]
        self.assertEqual(len(fallbacks), 1)
        proof = fallbacks[0]["data"]["proof"]
        self.assertEqual(proof["from_model"], "k1")
        self.assertEqual(proof["to_model"], "k2")
        self.assertEqual(proof["reason_class"], "quota_exhausted")
        self.assertNotEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")

    def test_full_chain_exhaustion_is_retryable_model_unavailable(self):
        node = _fallback_node("n1")
        monitor, runner, snapshot = self._start_fallback_run(
            node,
            [
                ProviderUnavailable("quota exhausted for model k1"),
                ProviderUnavailable("Error 429: rate limit reached"),
                ProviderUnavailable("unsupported model k3"),
            ],
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        state = snapshot.nodes["n1"]
        # Existing retryable state machine (no new durable status value):
        # retryable, never blocked_unknown, never success.
        self.assertEqual(state["status"], "retry_pending")
        self.assertNotEqual(state["status"], "blocked_unknown")
        self.assertIsInstance(state.get("retryable_action"), dict)
        self.assertEqual(state["retryable_action"].get("reason_class"), "model_unavailable")
        # Explicit model_unavailable marker lists each model's own failure.
        # (Node free-text ``reason`` is redacted on persist per the #277
        # provider-text contract, so the durable per-model detail lives in
        # this program-authored structure.)
        marker = state.get("model_unavailable")
        self.assertIsInstance(marker, dict)
        self.assertEqual(
            [(a["model"], a["reason_class"]) for a in marker["attempts"]],
            [("k1", "quota_exhausted"), ("k2", "rate_limit"), ("k3", "model_unavailable")],
        )
        # Each model was tried exactly once: bounded by the chain length.
        self.assertEqual(len(runner.start_calls), 3)
        self.assertEqual(
            [call["worker_profile"]["model"] for call in runner.start_calls],
            ["k1", "k2", "k3"],
        )
        fallbacks = [e for e in load_events(self.paths, snapshot.run_id) if e["event"] == "model_fallback_used"]
        self.assertEqual(len(fallbacks), 2)
        self.assertEqual(
            [(e["data"]["proof"]["from_model"], e["data"]["proof"]["to_model"]) for e in fallbacks],
            [("k1", "k2"), ("k2", "k3")],
        )
        self.assertFalse(any(e["event"] == "delivered" for e in load_events(self.paths, snapshot.run_id)))

    def test_model_unavailable_retry_is_bounded_then_parks(self):
        node = _fallback_node("n1")
        failures = [
            ProviderUnavailable("quota exhausted for model k1"),
            ProviderUnavailable("Error 429: rate limit reached"),
            ProviderUnavailable("unsupported model k3"),
        ]
        # every dispatch fails: round one plus one full retry round, then the
        # node parks instead of looping
        monitor, runner, snapshot = self._start_fallback_run(node, failures * 2)
        for _ in range(12):
            snapshot = monitor.tick(snapshot.run_id, runner)
        state = snapshot.nodes["n1"]
        self.assertEqual(state["status"], "retry_pending")
        self.assertIsNone(state.get("retryable_action"))
        self.assertEqual(state["model_unavailable"]["rounds"], 2)
        parked = [e for e in load_events(self.paths, snapshot.run_id)
                  if e["event"] == "model_unavailable_parked"]
        self.assertEqual(len(parked), 1)
        self.assertEqual(parked[0]["data"]["proof"]["rounds"], 2)
        # two rounds maximum, three models each: never an unbounded loop
        self.assertEqual(len(runner.start_calls), 6)
        self.assertEqual(
            [call["worker_profile"]["model"] for call in runner.start_calls],
            ["k1", "k2", "k3", "k1", "k2", "k3"],
        )
        self.assertEqual(
            [(a["model"], a["reason_class"]) for a in state["model_unavailable"]["attempts"]],
            [("k1", "quota_exhausted"), ("k2", "rate_limit"), ("k3", "model_unavailable")],
        )

    def test_non_model_failure_never_consumes_the_chain(self):
        node = _fallback_node("n1")
        monitor, runner, snapshot = self._start_fallback_run(
            node,
            [ProviderUnavailable("writer identity conflict, stop dispatch")],
        )
        snapshot = monitor.tick(snapshot.run_id, runner)

        events = load_events(self.paths, snapshot.run_id)
        self.assertFalse(any(e["event"] == "model_fallback_used" for e in events))
        self.assertEqual(len(runner.start_calls), 2)
        self.assertEqual(runner.start_calls[1]["worker_profile"]["model"], "k1")
        self.assertEqual(runner.start_calls[1]["worker_profile"]["fallbacks"],
                         [{"model": "k2"}, {"model": "k3"}])


def _write_codex_map(home: Path, tiers):
    (home / "config.json").write_text(
        json.dumps({"model_map": {"codex": tiers}}), encoding="utf-8"
    )


def _routing_issue():
    return IssueComplexity(
        "I-routing", "spec:I-routing", 4, 3, 3, 4, 3, "large",
        ["cross_module"], "complex", "evidence:I-routing",
    )


class RoutedNodeFallbackReplayTests(_ProjectCase):
    """P1-1: a fallback switch inside the router-selected chain must replay."""

    state = V42_STATE

    def _routed_node(self):
        return DAGNode(
            "r1",
            "r1",
            [],
            [],
            "g1",
            {
                "files": ["vibe_guide/model_router.py"],
                "worker": "worker-r1",
                "reviewer_worker": "reviewer-r1",
                "worktree": ".worktrees/r1",
                "issue_complexity": _routing_issue().to_dict(),
                "required_capabilities": ["visible_task"],
                "model_probes": [
                    LocalModel("rk1", ["visible_task"], 64000, ["deep"], True).to_dict(),
                    LocalModel("rk2", ["visible_task"], 32000, ["deep"], True).to_dict(),
                ],
                "routing_required": True,
            },
            "ready",
        )

    def test_routed_node_dispatches_next_chain_model_after_quota_failure(self):
        node = self._routed_node()
        plan = Plan("plan-1", 1, "docs/prd.md", [node.id], "draft")
        card = build_authorization_card(
            plan, [node], AgentCapabilities("fake", True, True, True, True, True, "full")
        )
        runner = _FlakyRunner(
            [ProviderUnavailable("quota exhausted for model rk1"), None]
        )
        monitor = Monitor(self.paths, plan, [node])
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["r1"]["status"], "running")
        self.assertEqual(len(runner.start_calls), 2)
        self.assertEqual(runner.start_calls[0]["worker_profile"]["model"], "rk1")
        self.assertEqual(runner.start_calls[1]["worker_profile"]["model"], "rk2")
        events = load_events(self.paths, snapshot.run_id)
        fallbacks = [e for e in events if e["event"] == "model_fallback_used"]
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["data"]["proof"]["to_model"], "rk2")

    def test_chain_model_with_drifted_route_digest_still_conflicts(self):
        # The chain exception never relaxes a real writer conflict: a profile
        # whose model is inside the chain but whose routing digest drifted
        # must still be rejected.
        node = self._routed_node()
        monitor = Monitor(self.paths, None, [node])
        current = {
            "worktree": ".worktrees/r1",
            "branch": "branch-r1",
            "contract_overrides": {},
            "model_dispatch_chain": ["rk1", "rk2"],
        }
        profile = WorkerProfile(
            "worker-r1",
            "rk2",
            "deep",
            [],
            {"issue_complexity_ref": "someone-else", "reasoning": "deep"},
        )
        contract = dict(node.contract, worker_profile=profile.to_dict())
        with self.assertRaisesRegex(ValueError, "conflicts with routing evidence"):
            monitor._prepare_worker_profile(node, contract, current, "developer")

    def test_chain_model_with_matching_route_digest_is_accepted(self):
        # The monitor-authored fallback switch itself (same reasoning and
        # route digest, model inside the chain) must be accepted.
        node = self._routed_node()
        monitor = Monitor(self.paths, None, [node])
        pristine = {
            "worktree": ".worktrees/r1",
            "branch": "branch-r1",
            "contract_overrides": {},
        }
        accepted = monitor._prepare_worker_profile(
            node, dict(node.contract), pristine, "developer"
        )
        current = {
            "worktree": ".worktrees/r1",
            "branch": "branch-r1",
            "contract_overrides": {},
            "model_dispatch_chain": ["rk1", "rk2"],
        }
        switched = WorkerProfile(
            accepted.worker,
            "rk2",
            accepted.reasoning,
            [],
            dict(accepted.selection_basis),
        )
        contract = dict(node.contract, worker_profile=switched.to_dict())
        replayed = monitor._prepare_worker_profile(node, contract, current, "developer")
        self.assertEqual(replayed.model, "rk2")


class ParkRecoveryTests(_ProjectCase):
    """P1-2: repair / requeue / tier adjustment must recover a parked node."""

    state = V42_STATE

    def _parked_placeholder_run(self, standard=("m1", "m2", "m3")):
        """Placeholder-profile node driven by model_map, parked after 2 rounds."""
        home = Path(os.environ["VIBE_HOME"])
        _write_codex_map(home, {"standard": list(standard)})
        node = _fallback_node("n1", model="default", fallbacks=())
        node.contract["adapter_id"] = "codex"
        plan = Plan("plan-1", 1, "docs/prd.md", [node.id], "draft")
        card = build_authorization_card(
            plan, [node], AgentCapabilities("codex", True, True, True, True, True, "full")
        )
        reasons = [
            "quota exhausted for model m1",
            "Error 429: rate limit reached",
            "unsupported model m3",
        ]
        runner = _FlakyRunner([ProviderUnavailable(text) for text in reasons * 2])
        monitor = Monitor(self.paths, plan, [node])
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(10):
            state = snapshot.nodes["n1"]
            if state.get("retryable_action") is None and isinstance(
                state.get("model_unavailable"), dict
            ):
                break
            snapshot = monitor.tick(snapshot.run_id, runner)
        return monitor, runner, snapshot, home

    def test_repair_after_park_requeues_and_applies_fixed_model_map(self):
        monitor, runner, snapshot, home = self._parked_placeholder_run()
        self.assertEqual(len(runner.start_calls), 6)
        self.assertIsNone(snapshot.nodes["n1"].get("retryable_action"))

        # The supervisor fixes the map, then repairs the node in place.
        _write_codex_map(home, {"standard": ["m9"]})
        result = monitor.repair_node("n1", snapshot.run_id)
        self.assertEqual(result["kind"], "retryable")
        self.assertEqual(result["repair_action"], "same_task_retry")

        snapshot = monitor.tick(snapshot.run_id, runner)
        state = snapshot.nodes["n1"]
        self.assertEqual(state["status"], "running")
        self.assertEqual(len(runner.start_calls), 7)
        self.assertEqual(runner.start_calls[-1]["worker_profile"]["model"], "m9")
        self.assertNotIn("model_unavailable", state)
        self.assertIsNone(state.get("model_dispatch_chain"))

    def test_schedule_retry_after_park_clears_fallback_state(self):
        monitor, runner, snapshot, _home = self._parked_placeholder_run()
        monitor.schedule_retry(
            "n1", "provider timeout", next_retry_at=__import__("time").time(),
            run_id=snapshot.run_id,
        )
        snapshot = monitor.tick(snapshot.run_id, runner)
        state = snapshot.nodes["n1"]
        self.assertEqual(state["status"], "running")
        self.assertEqual(len(runner.start_calls), 7)
        # The chain rewinds to its head: no stale switched profile is kept.
        self.assertEqual(runner.start_calls[-1]["worker_profile"]["model"], "m1")
        self.assertNotIn("model_unavailable", state)

    def test_tier_adjustment_after_park_requeues_with_new_tier(self):
        monitor, runner, snapshot, home = self._parked_placeholder_run()
        _write_codex_map(home, {"standard": ["m1", "m2", "m3"], "deep": ["d1"]})
        snapshot = monitor.adjust_model_tier(snapshot.run_id, "n1", 1, "need a deeper model")
        snapshot = monitor.tick(snapshot.run_id, runner)
        state = snapshot.nodes["n1"]
        self.assertEqual(state["status"], "running")
        self.assertEqual(len(runner.start_calls), 7)
        self.assertEqual(runner.start_calls[-1]["worker_profile"]["model"], "d1")
        self.assertNotIn("model_unavailable", state)


class UnconfiguredSingleModelTests(_ProjectCase):
    """P2-3: without alternates the legacy retryable behavior is unchanged."""

    state = V42_STATE

    def test_single_model_chain_keeps_legacy_retryable_loop(self):
        node = _fallback_node("n1", model="k1", fallbacks=())
        plan = Plan("plan-1", 1, "docs/prd.md", [node.id], "draft")
        card = build_authorization_card(
            plan, [node], AgentCapabilities("fake", True, True, True, True, True, "full")
        )
        runner = _FlakyRunner(
            [ProviderUnavailable("quota exhausted for model k1")] * 12
        )
        monitor = Monitor(self.paths, plan, [node])
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(9):
            snapshot = monitor.tick(snapshot.run_id, runner)

        state = snapshot.nodes["n1"]
        # Legacy behavior: a retryable engineering failure requeues every
        # tick until the provider recovers -- no park, no blocked_unknown.
        self.assertEqual(state["status"], "retry_pending")
        self.assertNotEqual(state["status"], "blocked_unknown")
        self.assertEqual(len(runner.start_calls), 10)
        self.assertTrue(
            all(call["worker_profile"]["model"] == "k1" for call in runner.start_calls)
        )
        self.assertNotIn("model_unavailable", state)
        self.assertIsNone(state.get("model_dispatch_chain"))
        self.assertIsNone(state.get("model_dispatch_attempts"))
        # The legacy recovery marker is a plain engineering retry, not a
        # model-fallback state.
        self.assertNotEqual(
            (state.get("retryable_action") or {}).get("reason_class"),
            "model_unavailable",
        )


class TierProfileFallbackFillTests(_ProjectCase):
    state = V42_STATE

    def test_apply_model_tier_fills_fallbacks_from_model_map(self):
        _write_home = Path(os.environ["VIBE_HOME"])
        (_write_home / "config.json").write_text(
            json.dumps({"model_map": {"codex": {"standard": ["m1", "m2", "m3"]}}}),
            encoding="utf-8",
        )
        node = _fallback_node("n1", model="default", fallbacks=())
        node.contract["adapter_id"] = "codex"
        plan = Plan("plan-1", 1, "docs/prd.md", [node.id], "draft")
        card = build_authorization_card(plan, [node], AgentCapabilities("codex", True, True, True, True, True, "full"))
        runner = _FlakyRunner([None])
        monitor = Monitor(self.paths, plan, [node])
        with patch.dict(model_catalog._PROBES, {"codex": lambda: [
            {"slug": "big", "efforts": ["low", "medium", "high"], "default": "medium"},
            {"slug": "mid", "efforts": ["low", "medium", "high"], "default": "medium"},
            {"slug": "small", "efforts": ["low", "high"], "default": "high"},
        ]}):
            snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)

        self.assertEqual(len(runner.start_calls), 1)
        profile = runner.start_calls[0]["worker_profile"]
        self.assertEqual(profile["model"], "m1")
        self.assertEqual(profile["fallbacks"], [{"model": "m2"}, {"model": "m3"}])
        self.assertEqual(snapshot.nodes["n1"]["status"], "running")


if __name__ == "__main__":
    unittest.main()
