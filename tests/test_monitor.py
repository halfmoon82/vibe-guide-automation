from copy import deepcopy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.contracts import RunEvent, RunHandle
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import acquire_writer_lease, append_event, load_events
from vibe_guide.task_registry import load_task_binding


class StartResponseLostRunner(FakeRunner):
    def start(self, contract, worktree):
        super().start(contract, worktree)
        raise ConnectionError("start response lost")


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
        },
        "ready",
    )


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
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
