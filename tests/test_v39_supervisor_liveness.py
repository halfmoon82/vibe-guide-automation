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
from vibe_guide.runners.provider_action import ProviderActionRunner, ProviderUnavailable
from vibe_guide.state import load_events, read_writer_lease, save_snapshot
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.task_registry import load_task_binding


def make_node(node_id="n1"):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "control",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "branch": "branch-" + node_id,
        },
        "ready",
    )


class PollFailureThenDeliveryRunner(FakeRunner):
    def __init__(self):
        super().__init__()
        self.poll_count = 0

    def poll(self, handle: RunHandle):
        self.poll_count += 1
        if self.poll_count == 1:
            raise TimeoutError("provider wait timed out")
        data = dict(self._claims_by_handle[handle.run_id])
        data["evidence"] = "delivery-after-retry"
        return [
            RunEvent("delivered", data)
        ]


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.tempdir.name))
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
        self.tempdir.cleanup()

    def authorized_monitor(self):
        node = make_node()
        plan = Plan("plan-liveness", 1, "docs/prd.md", [node.id], "draft")
        record = authorize(
            build_authorization_card(plan, [node], self.capabilities), "AUTHORIZE"
        )
        return Monitor(self.paths, plan, [node]), record

    def test_timeout_records_same_task_retry_without_second_writer(self):
        monitor, record = self.authorized_monitor()
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("timeout", {"reason": "provider wait timeout"}),
                    ("delivered", {"evidence": "delivery-after-timeout"}),
                ]
            }
        )

        snapshot = monitor.start(record, runner)
        task_id = snapshot.nodes["n1"]["developer_identity"]
        handle_id = snapshot.handles["n1"]
        after_timeout = monitor.tick(snapshot.run_id, runner)

        retry = after_timeout.nodes["n1"]["retryable_action"]
        self.assertEqual(after_timeout.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(after_timeout.nodes["n1"]["active_task"]["task_id"], task_id)
        self.assertEqual(after_timeout.handles["n1"], handle_id)
        self.assertTrue(retry["same_task"])
        self.assertFalse(retry["successor"])

        recovered = monitor.tick(snapshot.run_id, runner)
        developer_calls = [
            call for call in runner.start_calls if call["role"] == "developer"
        ]
        self.assertEqual(len(developer_calls), 1)
        self.assertEqual(developer_calls[0]["task_id"], task_id)
        self.assertEqual(recovered.nodes["n1"]["status"], "review")

    def test_poll_timeout_keeps_retryable_same_task_and_lease_owner(self):
        monitor, record = self.authorized_monitor()
        runner = PollFailureThenDeliveryRunner()

        snapshot = monitor.start(record, runner)
        task_id = snapshot.nodes["n1"]["developer_identity"]
        after_timeout = monitor.tick(snapshot.run_id, runner)

        retry = after_timeout.nodes["n1"]["retryable_action"]
        self.assertTrue(retry["same_task"])
        self.assertFalse(retry["successor"])
        self.assertEqual(
            after_timeout.nodes["n1"]["active_task"]["task_id"], task_id
        )
        lease = read_writer_lease(
            self.paths, "n1", after_timeout.nodes["n1"]["worktree"]
        )
        self.assertIsNotNone(lease)
        self.assertEqual(lease.run_id, snapshot.run_id)

        recovered = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(recovered.nodes["n1"]["status"], "review")
        self.assertEqual(
            len([call for call in runner.start_calls if call["role"] == "developer"]),
            1,
        )

    def test_snapshot_resume_reuses_active_task_after_timeout(self):
        monitor, record = self.authorized_monitor()
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("timeout", {"reason": "provider wait timeout"}),
                    ("delivered", {"evidence": "delivery-after-resume"}),
                ]
            }
        )
        snapshot = monitor.start(record, runner)
        developer_id = snapshot.nodes["n1"]["developer_identity"]
        monitor.tick(snapshot.run_id, runner)
        save_snapshot(self.paths, snapshot)

        resumed = Monitor(self.paths, monitor.plan, [make_node()]).resume(
            snapshot.run_id, runner
        )

        self.assertEqual(resumed.nodes["n1"]["status"], "review")
        self.assertEqual(
            len([call for call in runner.start_calls if call["role"] == "developer"]),
            1,
        )
        self.assertEqual(resumed.nodes["n1"]["developer_identity"], developer_id)

    def test_empty_poll_keeps_parent_running_and_does_not_create_successor(self):
        monitor, record = self.authorized_monitor()
        runner = FakeRunner()
        snapshot = monitor.start(record, runner)

        waiting = monitor.tick(snapshot.run_id, runner)

        self.assertEqual(waiting.status, "running")
        self.assertEqual(waiting.nodes["n1"]["status"], "running")
        self.assertEqual(
            len([call for call in runner.start_calls if call["role"] == "developer"]),
            1,
        )
        self.assertNotIn(
            "blocked_unknown",
            [event["event"] for event in load_events(self.paths, snapshot.run_id)],
        )

    def test_unapplied_timeout_event_rebuilds_retry_marker_without_new_task(self):
        monitor, record = self.authorized_monitor()
        runner = FakeRunner(
            events={
                ("n1", "developer"): [
                    ("timeout", {"reason": "provider wait timeout"}),
                    ("delivered", {"evidence": "delivery-after-replay"}),
                ]
            }
        )
        snapshot = monitor.start(record, runner)

        # The event append is durable before this simulated interruption, but
        # the snapshot write is not. Recovery must replay the same task.
        with patch(
            "vibe_guide.monitor.save_snapshot",
            side_effect=RuntimeError("interrupted after event append"),
        ):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                monitor.tick(snapshot.run_id, runner)

        recovered_monitor = Monitor(self.paths, monitor.plan, [make_node()])
        replayed = recovered_monitor.resume(
            snapshot.run_id, runner, poll_handles=False
        )
        retry = replayed.nodes["n1"]["retryable_action"]
        self.assertTrue(retry["same_task"])
        self.assertFalse(retry["successor"])
        self.assertEqual(
            retry["task_id"], replayed.nodes["n1"]["developer_identity"]
        )
        self.assertEqual(replayed.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(
            load_task_binding(
                self.paths, "n1", "developer", run_id=snapshot.run_id
            ).status,
            "blocked_unknown",
        )

        finished = recovered_monitor.resume(snapshot.run_id, runner)
        self.assertEqual(finished.nodes["n1"]["status"], "review")
        self.assertEqual(
            len([call for call in runner.start_calls if call["role"] == "developer"]),
            1,
        )

    def test_v39_create_probe_rejects_incomplete_binding_before_provider_request(self):
        """Every contract-root binding field is validated before create I/O."""
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            worktree = paths.root / "worker"
            worktree.mkdir()
            base = {
                "run_id": "run-v39-liveness",
                "node_id": "BUG-V3-002",
                "role": "developer",
                "generation": 1,
                "binding_contract_version": "3.9",
                "binding_probe": True,
                "project_id": "project-v39",
                "worktree": str(worktree),
                "managed_root": str(paths.root),
                "branch": "codex/bug-v3-002-supervisor-liveness-rev4",
                "base_sha": "a" * 40,
            }
            invalid_contracts = [
                ("project_id_missing", {"project_id": None}),
                ("worktree_path_drift", {"worktree": str(paths.root / "other")}),
                ("managed_root_wrong_type", {"managed_root": 123}),
                ("branch_blank", {"branch": "   "}),
                ("base_sha_invalid", {"base_sha": "not-a-sha"}),
            ]
            for label, override in invalid_contracts:
                with self.subTest(label=label):
                    contract = dict(base)
                    contract.update(override)
                    with patch.object(
                        runner, "_require_result", side_effect=AssertionError("create called")
                    ) as require_result:
                        with self.assertRaises((ProviderUnavailable, ValueError)):
                            runner.task_binding(
                                contract, worktree, contract["run_id"], "start_pending"
                            )
                    require_result.assert_not_called()


if __name__ == "__main__":
    unittest.main()
