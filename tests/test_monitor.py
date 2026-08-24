import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner


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
                "n1": [
                    ("delivered", {"evidence": "delivery-1"}),
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
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(snapshot.nodes["n1"]["evidence"], ["delivery-1", "fix test", "review-2"])

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
        runner = FakeRunner(events={"n1": [("accepted", {"evidence": "ok"})]})
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)

        resumed = monitor.resume(snapshot.run_id, runner)

        self.assertEqual(resumed.nodes["n1"]["status"], "accepted")
        self.assertEqual(resumed.nodes["n2"]["status"], "running")
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["n1", "n2"])


if __name__ == "__main__":
    unittest.main()
