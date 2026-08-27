from copy import deepcopy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.contracts import RunEvent, RunHandle
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.adapters.task_provider import ProviderPending
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    acquire_writer_lease,
    append_event,
    load_events,
    load_snapshot,
    save_snapshot,
)
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding
from vibe_guide.capability_contract import build_contract, save_contract


class StartResponseLostRunner(FakeRunner):
    def start(self, contract, worktree):
        super().start(contract, worktree)
        raise ConnectionError("start response lost")


class ProviderRetryRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.binding_attempts = 0

    def task_binding(self, contract, worktree, run_id, status):
        self.binding_attempts += 1
        if self.binding_attempts == 1:
            raise ProviderPending("provider capability bridge is pending")
        return TaskBinding(
            provider="fake",
            mode="background",
            issue_id=contract["node_id"],
            role=contract["role"],
            task_id=contract["task_id"],
            worktree=str(worktree),
            branch=contract.get("branch", "branch-" + contract["node_id"]),
            run_id=run_id,
            status=status,
            generation=contract["generation"],
        )


class PollResponseLostRunner(FakeRunner):
    def poll(self, handle: RunHandle):
        raise ConnectionError("poll response lost")


class DuplicateHandleRunner(FakeRunner):
    def start(self, contract, worktree):
        super().start(contract, worktree)
        return RunHandle("shared-handle")


class UnclaimedEventRunner(FakeRunner):
    def poll(self, handle: RunHandle):
        return [RunEvent("delivered", {"evidence": "unclaimed"})]


class SecretStartResponseLostRunner(FakeRunner):
    def start(self, contract, worktree):
        super().start(contract, worktree)
        raise ConnectionError("START_EXCEPTION_SECRET_SENTINEL")


class SecretPollResponseLostRunner(FakeRunner):
    def poll(self, handle: RunHandle):
        raise ConnectionError("POLL_EXCEPTION_SECRET_SENTINEL")


class ObservedContinuationRunner(FakeRunner):
    def __init__(self, task_id=None, cursor=None):
        super().__init__()
        self.observed_task_id = task_id
        self.observed_cursor = cursor

    def task_binding(self, contract, worktree, run_id, status):
        return TaskBinding(
            provider="runner",
            mode="background",
            issue_id=contract["node_id"],
            role=contract["role"],
            task_id=self.observed_task_id or contract["task_id"],
            worktree=str(worktree),
            run_id=run_id,
            status=status,
            generation=contract["generation"],
            cursor=self.observed_cursor,
        )


def node(node_id, depends_on=None, worker=None):
    return DAGNode(
        node_id,
        node_id,
        depends_on or [],
        [],
        "g1",
        {
            "files": [node_id + ".py"],
            "worker": worker or "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "worker_profile": {"worker": "codex", "model": "test", "reasoning": "normal", "fallbacks": [], "selection_basis": {"issue_complexity_ref": node_id, "complexity_band": "standard", "risk_tags": [], "availability_evidence": "test"}, "writer": "writer", "worktree": ".worktrees/" + node_id, "branch": "branch-" + node_id, "allowlist": [node_id + ".py"]},
        },
        "ready",
    )


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text('{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8")
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")

    def tearDown(self):
        self.temporary.cleanup()

    def authorized_monitor(self, nodes, active_pair_limit=None):
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        card = build_authorization_card(
            plan,
            nodes,
            self.capabilities,
            active_pair_limit=active_pair_limit,
        )
        return Monitor(self.paths, plan, nodes), authorize(card, "AUTHORIZE")

    def test_starts_independent_nodes_together_and_waits_for_hard_dependency(self):
        nodes = [node("n1"), node("n2"), node("n3", ["n1"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1", "n2"])
        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        self.assertEqual(snapshot.nodes["n2"]["status"], "running")
        self.assertEqual(snapshot.nodes["n3"]["status"], "planned")

    def test_stopped_pair_releases_capacity_without_archiving_pair(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        runner = FakeRunner(
            events={("n1", "developer"): [("stopped", {"reason": "provider stopped task"})]}
        )

        snapshot = monitor.start(record, runner)
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1"])

        stopped = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(stopped.nodes["n1"]["status"], "stopped")
        self.assertFalse(stopped.nodes["n1"]["pair_archived"])
        self.assertEqual(stopped.nodes["n2"]["status"], "running")
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1", "n2"])

    def test_blocked_unknown_with_active_handle_still_uses_capacity(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        current = snapshot.nodes["n1"]
        current["status"] = "blocked_unknown"
        current["developer_generation"] = 0
        current["retryable_action"] = None
        self.assertIn("n1", snapshot.handles)
        save_snapshot(self.paths, snapshot)

        blocked = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(blocked.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(blocked.nodes["n2"]["status"], "planned")
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1"])

    def test_v2_run_and_child_binding_lock_the_same_capability_contract_digest(self):
        monitor, record = self.authorized_monitor([node("n1")])
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        contract = json.loads(
            (self.paths.vibe / "session-contract.json").read_text(encoding="utf-8")
        )
        self.assertEqual(snapshot.capability_contract_digest, contract["contract_digest"])
        self.assertEqual(
            runner.start_calls[0]["capability_contract_digest"],
            contract["contract_digest"],
        )
        self.assertEqual(
            runner.start_calls[0]["child_binding"]["capability_contract_digest"],
            contract["contract_digest"],
        )

    def test_provider_pending_is_retried_without_capability_unavailable_status(self):
        monitor, record = self.authorized_monitor([node("n1")])
        runner = ProviderRetryRunner()
        snapshot = monitor.start(record, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        self.assertEqual(snapshot.status, "running")
        self.assertEqual(
            snapshot.nodes["n1"]["retryable_action"]["phase"], "develop"
        )
        self.assertFalse(
            any(event["event"] == "blocked_unknown" for event in load_events(self.paths, snapshot.run_id))
        )
        snapshot = monitor.resume(snapshot.run_id, runner)
        self.assertEqual(runner.binding_attempts, 2)
        self.assertEqual(snapshot.nodes["n1"]["status"], "running")

    def test_active_pair_capacity_archives_accepted_pair_and_starts_next(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
                ("n1", "reviewer"): [
                    ("accepted", {"evidence": "P0-P2 clear"})
                ],
            }
        )

        snapshot = monitor.start(record, runner)
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1"])
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertTrue(snapshot.nodes["n1"]["pair_archived"])
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["n2"]["status"], "running")
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", snapshot.run_id
            ).status,
            "archived",
        )
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "reviewer", snapshot.run_id
            ).status,
            "archived",
        )

    def test_node_provider_self_claims_do_not_become_visible_task_evidence(self):
        claimed = node("n1")
        claimed.contract.update(
            {
                "provider": "codex",
                "mode": "visible",
                "hostId": "fake-host",
                "developer_task_id": "fabricated-thread",
            }
        )
        monitor, record = self.authorized_monitor([claimed])

        snapshot = monitor.start(record, FakeRunner())
        binding = load_task_binding(
            self.paths, "n1", "developer", snapshot.run_id
        )

        self.assertEqual(binding.provider, "runner")
        self.assertEqual(binding.mode, "background")
        self.assertFalse(binding.visible)
        self.assertNotEqual(binding.task_id, "fabricated-thread")

    def test_provider_complete_is_delivery_and_does_not_unlock_dependent_node(self):
        nodes = [node("n1"), node("n2", ["n1"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={("n1", "developer"): [("complete", {"evidence": "delivery"})]}
        )

        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        self.assertEqual(snapshot.nodes["n2"]["status"], "planned")
        self.assertEqual(
            [(call["node_id"], call["role"]) for call in runner.start_calls],
            [("n1", "developer"), ("n1", "reviewer")],
        )

    def test_missing_authorization_never_starts_runner(self):
        nodes = [node("n1")]
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        runner = FakeRunner()

        with self.assertRaises(PermissionError):
            Monitor(self.paths, plan, nodes).start(None, runner)
        self.assertEqual(runner.start_calls, [])

    def test_review_finding_reworks_with_same_worker_and_preserves_evidence(self):
        nodes = [node("n1", worker="worker-original")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("delivered", {"evidence": "delivery-1"}),
                    ("delivered", {"evidence": "delivery-2"}),
                ],
                ("n1", "reviewer"): [
                    ("review_finding", {"finding": "fix test", "in_contract": True}),
                    ("accepted", {"evidence": "review-2"}),
                ]
            }
        )
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(runner.start_calls[-1]["worker"], "worker-original")
        self.assertEqual(runner.start_calls[-1]["phase"], "rework")
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(
            snapshot.nodes["n1"]["evidence"],
            ["[REDACTED_PROVIDER_TEXT]", "[REDACTED_PROVIDER_TEXT]"],
        )

        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(
            snapshot.nodes["n1"]["evidence"],
            [
                "[REDACTED_PROVIDER_TEXT]",
                "[REDACTED_PROVIDER_TEXT]",
                "[REDACTED_PROVIDER_TEXT]",
                "[REDACTED_PROVIDER_TEXT]",
            ],
        )

    def test_blocked_design_stops_old_task_and_releases_lease_for_new_authorization(self):
        original = node("n1")
        monitor, record = self.authorized_monitor([original])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("complete", {"evidence": "delivery"})
                ],
                ("n1", "reviewer"): [
                    (
                        "review_finding",
                        {
                            "finding": "genuine product choice",
                            "in_contract": False,
                        },
                    )
                ],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        old_reviewer_handle = snapshot.handles["n1"]
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_design")
        self.assertIn(old_reviewer_handle, runner.stop_calls)
        self.assertTrue(snapshot.nodes["n1"]["old_task_reconciled"])

        corrected = node("n1")
        corrected.contract["acceptance_example"] = "approved corrected outcome"
        new_monitor, new_record = self.authorized_monitor([corrected])
        restarted = new_monitor.start(new_record, FakeRunner())
        self.assertEqual(restarted.nodes["n1"]["status"], "running")

    def test_unique_in_scope_consistency_is_corrected_but_real_choice_blocks(self):
        target = node("n1")
        target.contract["naming"] = "approved-name"
        decisions = [
            {
                "id": "decision-naming",
                "question": "canonical name",
                "field": "naming",
                "revision": 1,
                "selected": "approved-name",
                "status": "approved",
            }
        ]
        plan = Plan(
            "plan-1",
            1,
            "docs/prd.md",
            ["n1"],
            "draft",
            decisions=decisions,
        )
        card = build_authorization_card(plan, [target], self.capabilities)
        monitor = Monitor(self.paths, plan, [target])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("complete", {"evidence": "delivery"})
                ]
            }
        )
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        binding = runner.start_calls[-1]["consistency_binding"]
        runner.events[("n1", "reviewer")] = [
            (
                "review_finding",
                {
                    "finding": "stale lower contract name",
                    "in_contract": False,
                    "consistency": {
                        "field": "naming",
                        "action": "rework",
                        "files": ["n1.py"],
                        "candidates": [
                            {
                                "source": "approved_prd",
                                "value": "approved-name",
                                "binding": binding,
                                "decision": {
                                    "id": "decision-naming",
                                    "field": "naming",
                                    "revision": 1,
                                    "status": "approved",
                                    "selected": "approved-name",
                                },
                            },
                            {
                                "source": "implementation",
                                "value": "stale-name",
                            },
                        ],
                    },
                },
            )
        ]
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(
            snapshot.nodes["n1"]["contract_overrides"],
            {"naming": "approved-name"},
        )
        self.assertEqual(
            snapshot.nodes["n1"]["corrections"][0]["source"],
            "approved_prd",
        )
        self.assertEqual(
            snapshot.nodes["n1"]["corrections"][0]["consistency_binding"],
            binding,
        )

        ambiguous = node("n2")
        ambiguous_plan = Plan("plan-2", 1, "docs/prd.md", ["n2"], "draft")
        ambiguous_card = build_authorization_card(
            ambiguous_plan, [ambiguous], self.capabilities
        )
        ambiguous_monitor = Monitor(self.paths, ambiguous_plan, [ambiguous])
        ambiguous_runner = FakeRunner(
            events={
                ("n2", "developer"): [
                    ("complete", {"evidence": "delivery"})
                ]
            }
        )
        blocked = ambiguous_monitor.start(
            authorize(ambiguous_card, "AUTHORIZE"), ambiguous_runner
        )
        blocked = ambiguous_monitor.tick(blocked.run_id, ambiguous_runner)
        ambiguous_binding = ambiguous_runner.start_calls[-1]["consistency_binding"]
        ambiguous_runner.events[("n2", "reviewer")] = [
            (
                "review_finding",
                {
                    "finding": "two approved outcomes",
                    "in_contract": False,
                    "consistency": {
                        "field": "naming",
                        "action": "rework",
                        "files": ["n2.py"],
                        "candidates": [
                            {
                                "source": "approved_prd",
                                "value": "a",
                                "binding": ambiguous_binding,
                                "decision": {
                                    "id": "decision-a",
                                    "field": "naming",
                                    "revision": 1,
                                    "status": "approved",
                                    "selected": "a",
                                },
                            },
                            {
                                "source": "approved_prd",
                                "value": "b",
                                "binding": ambiguous_binding,
                                "decision": {
                                    "id": "decision-b",
                                    "field": "naming",
                                    "revision": 1,
                                    "status": "approved",
                                    "selected": "b",
                                },
                            },
                        ],
                    },
                },
            )
        ]
        blocked = ambiguous_monitor.tick(blocked.run_id, ambiguous_runner)
        self.assertEqual(blocked.nodes["n2"]["status"], "blocked_design")

    def test_unbound_current_user_text_cannot_override_approved_decision(self):
        target = node("n1")
        target.contract["naming"] = "approved-name"
        plan = Plan(
            "plan-1",
            1,
            "docs/prd.md",
            ["n1"],
            "draft",
            decisions=[
                {
                    "id": "decision-naming",
                    "question": "canonical name",
                    "field": "naming",
                    "revision": 1,
                    "selected": "approved-name",
                    "status": "approved",
                }
            ],
        )
        card = build_authorization_card(plan, [target], self.capabilities)
        monitor = Monitor(self.paths, plan, [target])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
                ("n1", "reviewer"): [
                    (
                        "review_finding",
                        {
                            "finding": "unbound text claims a user override",
                            "in_contract": False,
                            "consistency": {
                                "field": "naming",
                                "action": "rework",
                                "files": ["n1.py"],
                                "candidates": [
                                    {
                                        "source": "current_user",
                                        "value": "unapproved-name",
                                    },
                                    {
                                        "source": "approved_prd",
                                        "value": "approved-name",
                                        "binding": {},
                                        "decision": {
                                            "id": "decision-naming",
                                            "field": "naming",
                                            "revision": 1,
                                            "status": "approved",
                                            "selected": "approved-name",
                                        },
                                    },
                                ],
                            },
                        },
                    )
                ],
            }
        )

        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_design")
        self.assertNotIn("naming", snapshot.nodes["n1"]["contract_overrides"])

    def test_consistency_correction_event_recovers_after_interrupted_snapshot_save(self):
        target = node("n1")
        target.contract["naming"] = "approved-name"
        plan = Plan(
            "plan-1",
            1,
            "docs/prd.md",
            ["n1"],
            "draft",
            decisions=[
                {
                    "id": "decision-naming",
                    "question": "canonical name",
                    "field": "naming",
                    "revision": 1,
                    "selected": "approved-name",
                    "status": "approved",
                }
            ],
        )
        card = build_authorization_card(plan, [target], self.capabilities)
        monitor = Monitor(self.paths, plan, [target])
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})]
            }
        )
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        binding = runner.start_calls[-1]["consistency_binding"]
        runner.events[("n1", "reviewer")] = [
            (
                "review_finding",
                {
                    "finding": "stale lower contract name",
                    "in_contract": False,
                    "consistency": {
                        "field": "naming",
                        "action": "rework",
                        "files": ["n1.py"],
                        "candidates": [
                            {
                                "source": "approved_prd",
                                "value": "approved-name",
                                "binding": binding,
                                "decision": {
                                    "id": "decision-naming",
                                    "field": "naming",
                                    "revision": 1,
                                    "status": "approved",
                                    "selected": "approved-name",
                                },
                            },
                            {
                                "source": "implementation",
                                "value": "stale-name",
                            },
                        ],
                    },
                },
            )
        ]

        with patch.object(
            monitor,
            "_start_task",
            side_effect=RuntimeError("interrupted after correction event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                monitor.tick(snapshot.run_id, runner)

        recovery_runner = FakeRunner()
        recovered = monitor.resume(snapshot.run_id, recovery_runner)
        correction = recovered.nodes["n1"]["corrections"][0]
        self.assertEqual(recovered.nodes["n1"]["contract_overrides"], {"naming": "approved-name"})
        self.assertEqual(correction["consistency_binding"], binding)
        self.assertEqual(recovered.nodes["n1"]["status"], "rework")
        self.assertEqual(recovery_runner.start_calls[-1]["phase"], "rework")

        persisted = [
            record for record in load_events(self.paths, snapshot.run_id)
            if record["event"] == "consistency_corrected"
        ][0]["data"]
        self.assertEqual(
            set(persisted),
            {
                "run_id",
                "node_id",
                "field",
                "value",
                "source",
                "action",
                "files",
                "consistency_binding",
                "decision",
            },
        )

    def test_reauthorization_event_recovers_before_snapshot_and_is_not_duplicated(self):
        original = node("n1")
        original_plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        original_record = authorize(
            build_authorization_card(
                original_plan, [original], self.capabilities
            ),
            "AUTHORIZE",
        )
        original_monitor = Monitor(self.paths, original_plan, [original])
        runner = FakeRunner()
        snapshot = original_monitor.start(original_record, runner)

        corrected = node("n1")
        corrected.contract["acceptance_example"] = "corrected implementation outcome"
        corrected_record = authorize(
            build_authorization_card(
                original_plan, [corrected], self.capabilities
            ),
            "AUTHORIZE",
        )
        corrected_monitor = Monitor(self.paths, original_plan, [corrected])
        with patch.object(
            corrected_monitor,
            "_schedule_ready",
            side_effect=RuntimeError("interrupted after reauthorization event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                corrected_monitor.reauthorize(
                    snapshot.run_id,
                    corrected_record,
                    runner,
                    "executable_contract_changed",
                )

        recovery_runner = FakeRunner()
        recovered = corrected_monitor.reauthorize(
            snapshot.run_id,
            corrected_record,
            recovery_runner,
            "executable_contract_changed",
        )

        self.assertEqual(recovered.authorization_digest, corrected_record.digest)
        self.assertEqual(recovered.nodes["n1"]["status"], "rework")
        self.assertEqual(recovery_runner.start_calls[-1]["phase"], "rework")
        self.assertEqual(
            len(
                [
                    event for event in load_events(self.paths, snapshot.run_id)
                    if event["event"] == "authorization_reauthorized"
                ]
            ),
            1,
        )

    def test_reauthorization_retains_independent_accepted_epoch(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review-n1"})],
                ("n2", "developer"): [("complete", {"evidence": "delivery-n2"})],
                ("n2", "reviewer"): [("accepted", {"evidence": "review-n2"})],
            }
        )
        snapshot = monitor.start(record, runner)
        for _ in range(3):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["n2"]["status"], "accepted")
        old_n2_acceptance = dict(snapshot.nodes["n2"]["acceptance"])

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed n1 contract"
        changed_nodes = [changed, node("n2")]
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)

        reauthorized = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            FakeRunner(),
            "executable_contract_changed",
        )

        self.assertEqual(reauthorized.nodes["n2"]["status"], "accepted")
        self.assertEqual(
            reauthorized.nodes["n2"]["acceptance"]["contract_digest"],
            old_n2_acceptance["contract_digest"],
        )
        self.assertEqual(
            reauthorized.nodes["n2"]["acceptance"]["authorization_epoch"],
            changed_record.digest,
        )
        self.assertEqual(reauthorized.nodes["n1"]["status"], "rework")

    def _assert_reauthorization_rejects_observed_continuation_mismatch(
        self, task_id=None, cursor=None
    ):
        original = node("n1")
        monitor, record = self.authorized_monitor([original])
        initial = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            }
        )
        snapshot = monitor.start(record, initial)
        snapshot = monitor.tick(snapshot.run_id, initial)
        snapshot = monitor.tick(snapshot.run_id, initial)
        binding = load_task_binding(
            self.paths, "n1", "developer", run_id=snapshot.run_id
        )
        binding.cursor = "original-cursor"
        save_task_binding(self.paths, binding)

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed contract"
        changed_plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        changed_record = authorize(
            build_authorization_card(changed_plan, [changed], self.capabilities),
            "AUTHORIZE",
        )
        observed = ObservedContinuationRunner(task_id=task_id, cursor=cursor)
        reauthorized = Monitor(self.paths, changed_plan, [changed]).reauthorize(
            snapshot.run_id,
            changed_record,
            observed,
            "executable_contract_changed",
        )

        self.assertEqual(reauthorized.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(observed.start_calls, [])

    def test_reauthorization_rejects_observed_continuation_identity_mismatch(self):
        self._assert_reauthorization_rejects_observed_continuation_mismatch(
            task_id="wrong-task-id",
            cursor="original-cursor",
        )

    def test_reauthorization_rejects_observed_continuation_cursor_mismatch(self):
        self._assert_reauthorization_rejects_observed_continuation_mismatch(
            cursor="wrong-cursor"
        )

    def test_reauthorization_invalidates_accepted_downstream_suffix_and_recovers(self):
        upstream = node("n1")
        downstream = node("n2", ["n1"])
        independent = node("n3")
        nodes = [upstream, downstream, independent]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "delivery-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review-n1"})],
                ("n2", "developer"): [("complete", {"evidence": "delivery-n2"})],
                ("n2", "reviewer"): [("accepted", {"evidence": "review-n2"})],
                ("n3", "developer"): [("complete", {"evidence": "delivery-n3"})],
                ("n3", "reviewer"): [("accepted", {"evidence": "review-n3"})],
            }
        )
        snapshot = monitor.start(record, runner)
        for _ in range(6):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertTrue(
            all(item["status"] == "accepted" for item in snapshot.nodes.values())
        )
        old_identities = {
            node_id: (
                snapshot.nodes[node_id]["developer_identity"],
                snapshot.nodes[node_id]["reviewer_identity"],
            )
            for node_id in ("n2", "n3")
        }
        old_cursors = {}
        for node_id in ("n2", "n3"):
            for role in ("developer", "reviewer"):
                binding = load_task_binding(
                    self.paths, node_id, role, run_id=snapshot.run_id
                )
                binding.cursor = "cursor-{}-{}".format(node_id, role)
                save_task_binding(self.paths, binding)
                old_cursors["{}:{}".format(node_id, role)] = binding.cursor

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed upstream contract"
        changed_nodes = [changed, node("n2", ["n1"]), node("n3")]
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)

        with patch.object(
            changed_monitor,
            "_schedule_ready",
            side_effect=RuntimeError("interrupted after reauthorization event"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                changed_monitor.reauthorize(
                    snapshot.run_id,
                    changed_record,
                    FakeRunner(),
                    "executable_contract_changed",
                )

        recovered = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            FakeRunner(),
            "executable_contract_changed",
        )

        self.assertEqual(recovered.nodes["n2"]["status"], "blocked_unknown")
        self.assertIsNone(recovered.nodes["n2"]["acceptance"])
        self.assertTrue(
            recovered.nodes["n2"]["retryable_action"]["pending_schedule"]
        )
        self.assertEqual(recovered.nodes["n3"]["status"], "accepted")
        self.assertEqual(
            (
                recovered.nodes["n2"]["developer_identity"],
                recovered.nodes["n2"]["reviewer_identity"],
            ),
            old_identities["n2"],
        )
        self.assertEqual(
            (
                recovered.nodes["n3"]["developer_identity"],
                recovered.nodes["n3"]["reviewer_identity"],
            ),
            old_identities["n3"],
        )
        for node_id in ("n2", "n3"):
            for role in ("developer", "reviewer"):
                self.assertEqual(
                    load_task_binding(
                        self.paths, node_id, role, run_id=snapshot.run_id
                    ).cursor,
                    old_cursors["{}:{}".format(node_id, role)],
                )
        self.assertEqual(
            len(
                [
                    event
                    for event in load_events(self.paths, snapshot.run_id)
                    if event["event"] == "authorization_reauthorized"
                ]
            ),
            1,
        )

    def test_reauthorization_waits_for_hard_dependency_before_downstream_rework(self):
        nodes = [node("n1"), node("n2", ["n1"]), node("n3")]
        monitor, record = self.authorized_monitor(nodes)
        initial = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("n1", "n2", "n3")
            }
        )
        initial.events.update(
            {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("n1", "n2", "n3")
            }
        )
        snapshot = monitor.start(record, initial)
        for _ in range(6):
            snapshot = monitor.tick(snapshot.run_id, initial)
        self.assertTrue(
            all(current["status"] == "accepted" for current in snapshot.nodes.values())
        )

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed upstream"
        changed_nodes = [changed, node("n2", ["n1"]), node("n3")]
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)
        recovery = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "new-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "new-n1"})],
            }
        )

        snapshot = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            recovery,
            "executable_contract_changed",
        )

        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1"])
        transition_sequence = next(
            event["sequence"]
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "authorization_reauthorized"
        )
        n2_start_events = [
            event
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "start_intent"
            and event["data"].get("node_id") == "n2"
            and event["sequence"] > transition_sequence
        ]
        self.assertEqual(n2_start_events, [])
        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1", "n1"])
        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual(
            [call["node_id"] for call in recovery.start_calls],
            ["n1", "n1", "n2"],
        )

    def test_reauthorization_active_downstream_waits_for_reaccepted_dependency_and_capacity(
        self,
    ):
        nodes = [node("n1"), node("n2", ["n1"])]
        monitor, record = self.authorized_monitor(nodes, active_pair_limit=1)
        initial = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "old-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "old-n1"})],
            }
        )
        snapshot = monitor.start(record, initial)
        snapshot = monitor.tick(snapshot.run_id, initial)
        snapshot = monitor.tick(snapshot.run_id, initial)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["n2"]["status"], "running")

        original_handle = snapshot.handles["n2"]
        original_identity = snapshot.nodes["n2"]["developer_identity"]
        downstream_binding = load_task_binding(
            self.paths, "n2", "developer", run_id=snapshot.run_id
        )
        downstream_binding.cursor = "active-downstream-cursor"
        save_task_binding(self.paths, downstream_binding)

        changed_upstream = node("n1")
        changed_upstream.contract["acceptance_example"] = "changed upstream"
        changed_nodes = [changed_upstream, node("n2", ["n1"])]
        changed_plan = Plan("plan-1", 1, "docs/prd.md", ["n1", "n2"], "draft")
        changed_record = authorize(
            build_authorization_card(
                changed_plan,
                changed_nodes,
                self.capabilities,
                active_pair_limit=1,
            ),
            "AUTHORIZE",
        )
        recovery = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "new-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "new-n1"})],
            }
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)

        snapshot = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            recovery,
            "executable_contract_changed",
        )

        self.assertIn(original_handle, recovery.stop_calls)
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1"])
        self.assertEqual(snapshot.nodes["n2"]["status"], "blocked_unknown")
        self.assertTrue(snapshot.nodes["n2"]["retryable_action"]["pending_schedule"])
        transition_sequence = next(
            event["sequence"]
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "authorization_reauthorized"
        )

        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        before_acceptance = [
            event
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "start_intent"
            and event["data"].get("node_id") == "n2"
            and event["sequence"] > transition_sequence
        ]
        self.assertEqual(before_acceptance, [])
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1", "n1"])

        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(
            [call["node_id"] for call in recovery.start_calls],
            ["n1", "n1", "n2"],
        )
        self.assertEqual(recovery.start_calls[-1]["task_id"], original_identity)
        continued_binding = load_task_binding(
            self.paths, "n2", "developer", run_id=snapshot.run_id
        )
        self.assertEqual(continued_binding.task_id, original_identity)
        self.assertEqual(continued_binding.cursor, "active-downstream-cursor")

    def test_reauthorization_retry_obeys_capacity_but_not_integration_after(self):
        independent = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(independent, active_pair_limit=1)
        initial = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("n1", "n2")
            }
        )
        initial.events.update(
            {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("n1", "n2")
            }
        )
        snapshot = monitor.start(record, initial)
        for _ in range(5):
            snapshot = monitor.tick(snapshot.run_id, initial)
        self.assertEqual(snapshot.status, "complete")

        changed_nodes = [node("n1"), node("n2")]
        for item in changed_nodes:
            item.contract["acceptance_example"] = "changed-" + item.id
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(
                changed_plan, changed_nodes, self.capabilities, active_pair_limit=1
            ),
            "AUTHORIZE",
        )
        recovery = FakeRunner(
            events={
                ("n1", "developer"): [("complete", {"evidence": "new-n1"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "new-n1"})],
            }
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)
        snapshot = changed_monitor.reauthorize(
            snapshot.run_id,
            changed_record,
            recovery,
            "executable_contract_changed",
        )
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1"])
        transition_sequence = next(
            event["sequence"]
            for event in load_events(self.paths, snapshot.run_id)
            if event["event"] == "authorization_reauthorized"
        )
        self.assertFalse(
            any(
                event["event"] == "start_intent"
                and event["data"].get("node_id") == "n2"
                and event["sequence"] > transition_sequence
                for event in load_events(self.paths, snapshot.run_id)
            )
        )
        snapshot = changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual([call["node_id"] for call in recovery.start_calls], ["n1", "n1"])
        changed_monitor.tick(snapshot.run_id, recovery)
        self.assertEqual(
            [call["node_id"] for call in recovery.start_calls],
            ["n1", "n1", "n2"],
        )

        integration_upstream = node("i1")
        integration_downstream = node("i2")
        integration_downstream.integration_after = ["i1"]
        integration_monitor, integration_record = self.authorized_monitor(
            [integration_upstream, integration_downstream], active_pair_limit=2
        )
        integration_runner = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("i1", "i2")
            }
        )
        integration_runner.events.update(
            {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("i1", "i2")
            }
        )
        integration_snapshot = integration_monitor.start(
            integration_record, integration_runner
        )
        self.assertEqual(
            [call["node_id"] for call in integration_runner.start_calls],
            ["i1", "i2"],
        )
        for _ in range(3):
            integration_snapshot = integration_monitor.tick(
                integration_snapshot.run_id, integration_runner
            )
        self.assertEqual(integration_snapshot.status, "complete")

        changed_i1 = node("i1")
        changed_i1.contract["acceptance_example"] = "changed integration upstream"
        changed_i2 = node("i2")
        changed_i2.integration_after = ["i1"]
        changed_integration_nodes = [changed_i1, changed_i2]
        changed_integration_plan = Plan(
            "plan-1", 1, "docs/prd.md", ["i1", "i2"], "draft"
        )
        changed_integration_record = authorize(
            build_authorization_card(
                changed_integration_plan,
                changed_integration_nodes,
                self.capabilities,
                active_pair_limit=2,
            ),
            "AUTHORIZE",
        )
        integration_recovery = FakeRunner()
        Monitor(
            self.paths, changed_integration_plan, changed_integration_nodes
        ).reauthorize(
            integration_snapshot.run_id,
            changed_integration_record,
            integration_recovery,
            "executable_contract_changed",
        )
        self.assertEqual(
            [call["node_id"] for call in integration_recovery.start_calls],
            ["i1", "i2"],
        )

    def test_applied_reauthorization_rejects_forged_retained_downstream(self):
        nodes = [node("n1"), node("n2", ["n1"]), node("n3")]
        monitor, record = self.authorized_monitor(nodes)
        initial = FakeRunner(
            events={
                (node_id, "developer"): [("complete", {"evidence": node_id})]
                for node_id in ("n1", "n2", "n3")
            }
        )
        initial.events.update(
            {
                (node_id, "reviewer"): [("accepted", {"evidence": node_id})]
                for node_id in ("n1", "n2", "n3")
            }
        )
        snapshot = monitor.start(record, initial)
        for _ in range(6):
            snapshot = monitor.tick(snapshot.run_id, initial)

        changed = node("n1")
        changed.contract["acceptance_example"] = "changed upstream"
        changed_nodes = [changed, node("n2", ["n1"]), node("n3")]
        changed_plan = Plan(
            "plan-1", 1, "docs/prd.md", [item.id for item in changed_nodes], "draft"
        )
        changed_record = authorize(
            build_authorization_card(changed_plan, changed_nodes, self.capabilities),
            "AUTHORIZE",
        )
        changed_monitor = Monitor(self.paths, changed_plan, changed_nodes)
        with patch.object(changed_monitor, "_schedule_ready", return_value=None):
            applied = changed_monitor.reauthorize(
                snapshot.run_id,
                changed_record,
                FakeRunner(),
                "executable_contract_changed",
            )

        run_directory = Path(self.temporary.name) / ".vibe/runs" / applied.run_id
        event_path = run_directory / "events.jsonl"
        records = [json.loads(line) for line in event_path.read_text().splitlines()]
        transition_index = next(
            index
            for index, event in enumerate(records)
            if event["event"] == "authorization_reauthorized"
        )
        transition = records[transition_index]["data"]
        transition["affected_nodes"] = ["n1"]
        transition["retained_acceptances"]["n2"] = transition[
            "invalidated_acceptances"
        ].pop("n2")
        forged_n2_contract = json.loads(
            transition["authorized_node_contracts"]["n2"]
        )
        forged_n2_contract["depends_on"] = []
        transition["authorized_node_contracts"]["n2"] = json.dumps(
            forged_n2_contract,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        previous_digest = (
            records[transition_index - 1]["event_digest"]
            if transition_index
            else None
        )
        for event in records[transition_index:]:
            event["previous_event_digest"] = previous_digest
            payload = dict(event)
            payload.pop("event_digest", None)
            event["event_digest"] = hashlib.sha256(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            previous_digest = event["event_digest"]
        event_path.write_text(
            "".join(
                json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n"
                for event in records
            ),
            encoding="utf-8",
        )

        forged = applied.to_dict()
        forged_n2 = forged["nodes"]["n2"]
        forged_n2["status"] = "accepted"
        forged_n2["acceptance"] = {
            "contract_digest": transition["node_contract_digests"]["n2"],
            "authorization_epoch": transition["authorization_digest"],
        }
        forged_n2["pair_archived"] = True
        forged_n2["retryable_action"] = None
        encoded = json.dumps(
            forged, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        for state_name in ("state.json", "state.previous.json"):
            (run_directory / state_name).write_text(encoded, encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "no valid snapshot"):
            load_snapshot(self.paths, applied.run_id)

    def test_unknown_is_blocked_unknown_not_completed(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(events={"n1": [("unknown", {"reason": "poll timeout"})]})
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(snapshot.status, "blocked_unknown")

    def test_rework_reuses_developer_and_reviewer_task_identities(self):
        nodes = [node("n1", worker="worker-original")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("delivered", {"evidence": "delivery-1"}),
                    ("delivered", {"evidence": "delivery-2"}),
                ],
                ("n1", "reviewer"): [
                    ("review_finding", {"finding": "fix test", "in_contract": True}),
                    ("accepted", {"evidence": "review-2"}),
                ],
            }
        )
        snapshot = monitor.start(record, runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(
            [call["phase"] for call in runner.start_calls],
            ["develop", "review", "rework", "review"],
        )
        self.assertEqual(runner.start_calls[0]["task_id"], runner.start_calls[2]["task_id"])
        self.assertEqual(runner.start_calls[1]["task_id"], runner.start_calls[3]["task_id"])
        self.assertTrue(runner.start_calls[3]["continuation"])

    def test_second_monitor_cannot_take_an_existing_writer_lease(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        first_runner = FakeRunner()
        monitor.start(record, first_runner)

        second_runner = FakeRunner()
        second_snapshot = Monitor(self.paths, monitor.plan, nodes).start(record, second_runner)

        self.assertEqual(second_snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(second_runner.start_calls, [])

    def test_accepted_node_unlocks_dependent_node_and_resume_uses_snapshot(self):
        nodes = [node("n1"), node("n2", ["n1"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "ok"})],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        resumed = monitor.resume(snapshot.run_id, runner)

        self.assertEqual(resumed.nodes["n1"]["status"], "accepted")
        self.assertEqual(resumed.nodes["n2"]["status"], "running")
        self.assertEqual(
            [(call["node_id"], call["role"]) for call in runner.start_calls],
            [("n1", "developer"), ("n1", "reviewer"), ("n2", "developer")],
        )

    def test_live_contract_mutation_invalidates_authorization_before_start(self):
        base = node("n1")
        base.contract.update(
            {
                "branch": "codex/safe",
                "provider": "codex",
                "mode": "visible",
                "hostId": "local",
                "developer_task_id": "thread-safe",
            }
        )
        plan = Plan("plan-bound", 1, "docs/prd.md", ["n1"], "draft")
        record = authorize(
            build_authorization_card(plan, [base], self.capabilities), "AUTHORIZE"
        )
        mutations = {
            "files": ["outside.py"],
            "worker": "worker-evil",
            "worktree": "../outside",
            "branch": "codex/evil",
            "provider": "other-provider",
            "developer_task_id": "thread-other",
            "action": "deploy",
        }

        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root:
                changed = deepcopy(base)
                changed.contract[field] = value
                runner = FakeRunner()
                with self.assertRaises(PermissionError):
                    Monitor(ProjectPaths(Path(root)), plan, [changed]).start(record, runner)
                self.assertEqual(runner.start_calls, [])

    def test_contract_mutation_is_rechecked_before_tick(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        monitor.nodes["n1"].contract["files"] = ["outside.py"]

        with self.assertRaises(PermissionError):
            monitor.tick(snapshot.run_id, runner)

    def test_forged_snapshot_without_authorization_or_event_lineage_is_rejected(self):
        nodes = [node("n1")]
        monitor, _record = self.authorized_monitor(nodes)
        run_dir = Path(self.temporary.name) / ".vibe/runs/run-forged"
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(
            json.dumps(
                {
                    "run_id": "run-forged",
                    "plan_id": "plan-1",
                    "plan_version": 1,
                    "status": "running",
                    "nodes": {
                        "n1": {
                            "status": "planned",
                            "worker": "worker-n1",
                            "worktree": ".worktrees/n1",
                            "evidence": [],
                        }
                    },
                    "handles": {},
                    "tasks": {},
                }
            ),
            encoding="utf-8",
        )
        runner = FakeRunner()

        with self.assertRaises(ValueError):
            monitor.resume("run-forged", runner)
        self.assertEqual(runner.start_calls, [])

    def test_developer_cannot_self_accept(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(events={"n1": [("accepted", {"evidence": "self"})]})
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertNotEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertNotEqual(snapshot.status, "complete")
        self.assertFalse(
            acquire_writer_lease(
                self.paths, "n1", ".worktrees/n1", "run-second"
            )
        )

    def test_delivered_node_still_requires_independent_review(self):
        delivered = node("n1")
        delivered.status = "delivered"
        monitor, record = self.authorized_monitor([delivered])
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "review")
        self.assertEqual([call["role"] for call in runner.start_calls], ["reviewer"])
        self.assertNotEqual(snapshot.status, "complete")

    def test_acceptance_rejects_conflicting_reviewer_provenance(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [
                    ("accepted", {"evidence": "review", "role": "developer", "generation": 999})
                ],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertNotEqual(snapshot.status, "complete")

    def test_start_response_loss_quarantines_writer_lease(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)

        snapshot = monitor.start(record, StartResponseLostRunner())

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertFalse(
            acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "run-second")
        )

    def test_task_binding_status_tracks_confirmed_runtime_state(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            }
        )

        snapshot = monitor.start(record, runner)
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", run_id=snapshot.run_id
            ).status,
            "running",
        )

        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", run_id=snapshot.run_id
            ).status,
            "delivered",
        )
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "reviewer", run_id=snapshot.run_id
            ).status,
            "review",
        )

        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "reviewer", run_id=snapshot.run_id
            ).status,
            "archived",
        )
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", run_id=snapshot.run_id
            ).status,
            "archived",
        )

    def test_recovery_rejects_forged_terminal_before_releasing_lease(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        append_event(
            self.paths,
            RunEvent(
                "terminal_failed",
                {"run_id": snapshot.run_id, "node_id": "n1", "reason": "forged"},
            ),
            {
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            },
        )

        with self.assertRaises(ValueError):
            monitor.resume(snapshot.run_id, runner)
        self.assertFalse(
            acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "run-second")
        )

    def test_acceptance_uses_reviewer_binding_from_current_run(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [("delivered", {"evidence": "delivery"})],
                ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            }
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        current_registry = (
            Path(self.temporary.name)
            / ".vibe"
            / "runs"
            / snapshot.run_id
            / "tasks.json"
        )
        foreign_registry = (
            Path(self.temporary.name)
            / ".vibe"
            / "runs"
            / "zzzz-foreign-run"
            / "tasks.json"
        )
        foreign_registry.parent.mkdir(parents=True)
        foreign_registry.write_bytes(current_registry.read_bytes())
        current_registry.unlink()

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")

    def test_duplicate_active_handle_quarantines_second_node(self):
        nodes = [node("n1"), node("n2")]
        monitor, record = self.authorized_monitor(nodes)

        snapshot = monitor.start(record, DuplicateHandleRunner())

        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        self.assertEqual(snapshot.nodes["n2"]["status"], "blocked_unknown")
        self.assertEqual(list(snapshot.handles.values()), ["shared-handle"])
        self.assertFalse(
            acquire_writer_lease(
                self.paths, "n2", ".worktrees/n2", "run-second"
            )
        )

    def test_state_transition_requires_complete_provider_claims(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        snapshot = monitor.start(record, UnclaimedEventRunner())

        snapshot = monitor.tick(snapshot.run_id, UnclaimedEventRunner())

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertFalse(snapshot.nodes["n1"]["reviewer_started"])
        self.assertFalse(
            acquire_writer_lease(
                self.paths, "n1", ".worktrees/n1", "run-second"
            )
        )

    def test_provider_payloads_and_exceptions_never_reach_persisted_artifacts(self):
        cases = (
            (
                "EVENT_SECRET_SENTINEL",
                FakeRunner(
                    events={
                        ("n1", "developer"): [
                            (
                                "delivered",
                                {
                                    "evidence": "EVENT_SECRET_SENTINEL",
                                    "nested": {"Api-Key": "EVENT_SECRET_SENTINEL"},
                                },
                            )
                        ]
                    }
                ),
                True,
            ),
            (
                "START_EXCEPTION_SECRET_SENTINEL",
                SecretStartResponseLostRunner(),
                False,
            ),
            (
                "POLL_EXCEPTION_SECRET_SENTINEL",
                SecretPollResponseLostRunner(),
                True,
            ),
        )
        for sentinel, runner, tick in cases:
            with self.subTest(sentinel=sentinel), tempfile.TemporaryDirectory() as root:
                paths = ProjectPaths(Path(root))
                nodes = [node("n1")]
                plan = Plan("plan-secret", 1, "docs/prd.md", ["n1"], "draft")
                record = authorize(
                    build_authorization_card(plan, nodes, self.capabilities), "AUTHORIZE"
                )
                monitor = Monitor(paths, plan, nodes)
                snapshot = monitor.start(record, runner)
                if tick:
                    snapshot = monitor.tick(snapshot.run_id, runner)

                durable = b"".join(
                    path.read_bytes()
                    for path in sorted((Path(root) / ".vibe").rglob("*"))
                    if path.is_file()
                )
                self.assertNotIn(sentinel.encode("utf-8"), durable)

    def test_provider_confirmed_stop_is_persisted_as_failed_terminal_run(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("stopped", {"reason": "provider stopped task"})
                ]
            }
        )
        snapshot = monitor.start(record, runner)

        stopped = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(stopped.nodes["n1"]["status"], "stopped")
        self.assertEqual(stopped.status, "failed")
        self.assertNotIn("n1", stopped.handles)
        self.assertTrue(stopped.nodes["n1"]["reason"])
        resumed = monitor.resume(stopped.run_id, runner)
        ticked = monitor.tick(stopped.run_id, runner)
        self.assertEqual(resumed.status, "failed")
        self.assertEqual(ticked.status, "failed")
        self.assertEqual(len(runner.start_calls), 1)

    def test_poll_loss_and_repeated_unknown_keep_writer_quarantined(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = PollResponseLostRunner()
        snapshot = monitor.start(record, runner)

        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertFalse(
            acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "run-second")
        )

    def test_start_intent_is_recoverable_without_duplicate_after_interruption(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()
        from vibe_guide import monitor as monitor_module

        real_save = monitor_module.save_snapshot

        def interrupt_after_external_start(paths, snapshot):
            if runner.start_calls:
                raise RuntimeError("process interrupted after external start")
            return real_save(paths, snapshot)

        with patch("vibe_guide.monitor.save_snapshot", side_effect=interrupt_after_external_start):
            with self.assertRaises(RuntimeError):
                monitor.start(record, runner)
        self.assertEqual(len(runner.start_calls), 1)

        recovery_runner = FakeRunner()
        recovered = monitor.resume(
            next((Path(self.temporary.name) / ".vibe/runs").iterdir()).name,
            recovery_runner,
        )

        self.assertEqual(recovery_runner.start_calls, [])
        self.assertEqual(recovered.nodes["n1"]["status"], "running")
        self.assertIn("n1", recovered.handles)
        self.assertFalse(
            acquire_writer_lease(
                self.paths, "n1", ".worktrees/n1", "run-second"
            )
        )

    def test_tampered_event_lineage_blocks_tick(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)
        event_path = Path(self.temporary.name) / ".vibe/runs" / snapshot.run_id / "events.jsonl"
        records = [json.loads(line) for line in event_path.read_text().splitlines()]
        records[0]["data"]["authorization_digest"] = "0" * 64
        event_path.write_text(
            "".join(json.dumps(item) + "\n" for item in records), encoding="utf-8"
        )

        with self.assertRaises(ValueError):
            monitor.tick(snapshot.run_id, runner)

    def test_recovery_rejects_stale_reviewer_generation_without_releasing_lease(self):
        nodes = [node("n1")]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner(
            events={("n1", "developer"): [("delivered", {"evidence": "delivery"})]}
        )
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        active = snapshot.nodes["n1"]["active_task"]
        append_event(
            self.paths,
            RunEvent(
                "accepted",
                {"run_id": snapshot.run_id, "node_id": "n1", "evidence": "stale"},
            ),
            {
                "role": "reviewer",
                "task_id": active["task_id"],
                "handle_id": active["handle_id"],
                "generation": active["generation"] - 1,
                "authorization_digest": snapshot.authorization_digest,
                "node_contract_digest": snapshot.node_contract_digest,
            },
        )

        with self.assertRaises(ValueError):
            monitor.resume(snapshot.run_id, runner)
        self.assertFalse(
            acquire_writer_lease(self.paths, "n1", ".worktrees/n1", "run-second")
        )


if __name__ == "__main__":
    unittest.main()
