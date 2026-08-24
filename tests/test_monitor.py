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
from vibe_guide.state import acquire_writer_lease, append_event
from vibe_guide.task_registry import TaskBinding, load_task_binding, save_task_binding


class StartResponseLostRunner(FakeRunner):
    def start(self, contract, worktree):
        super().start(contract, worktree)
        raise ConnectionError("start response lost")


class PollResponseLostRunner(FakeRunner):
    def poll(self, handle: RunHandle):
        raise ConnectionError("poll response lost")


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

    def authorized_monitor(self, nodes):
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        card = build_authorization_card(plan, nodes, self.capabilities)
        return Monitor(self.paths, plan, nodes), authorize(card, "AUTHORIZE")

    def test_starts_independent_nodes_together_and_waits_for_hard_dependency(self):
        nodes = [node("n1"), node("n2"), node("n3", ["n1"])]
        monitor, record = self.authorized_monitor(nodes)
        runner = FakeRunner()

        snapshot = monitor.start(record, runner)

        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1", "n2"])
        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        self.assertEqual(snapshot.nodes["n2"]["status"], "running")
        self.assertEqual(snapshot.nodes["n3"]["status"], "pending")

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
        self.assertEqual(snapshot.nodes["n1"]["evidence"], ["delivery-1", "fix test"])

        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(
            snapshot.nodes["n1"]["evidence"],
            ["delivery-1", "fix test", "delivery-2", "review-2"],
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
                            "status": "pending",
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

    def test_technical_complete_still_requires_independent_review(self):
        completed = node("n1")
        completed.status = "complete"
        monitor, record = self.authorized_monitor([completed])
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
            "accepted",
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
        active = snapshot.nodes["n1"]["active_task"]
        save_task_binding(
            self.paths,
            TaskBinding(
                provider="runner",
                mode="background",
                issue_id="n1",
                role="reviewer",
                task_id=active["task_id"],
                worktree=".worktrees/n1",
                branch="node/n1",
                run_id="zzzz-foreign-run",
                status="review",
                generation=active["generation"],
            ),
        )
        current_registry = (
            Path(self.temporary.name)
            / ".vibe"
            / "runs"
            / snapshot.run_id
            / "tasks.json"
        )
        current_registry.unlink()

        snapshot = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")

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
