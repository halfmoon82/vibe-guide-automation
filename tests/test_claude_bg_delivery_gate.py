"""claude-code-background: a self-reported delivery must pass the delivery gate.

Recorded on a real ``claude --bg`` run (5.0.18, /tmp/vibe-bg-probe,
2026-10-18): reconnect and ``vibe worker-deliver`` worked, but the delivered
event then fell to blocked_unknown under the vibeguide_monitor engine:

* the runner nested the worker report under ``payload`` while the monitor
  reads ``delivery_evidence`` from the event itself;
* the background task binding had no host and no cursor;
* the monitor never creates the node worktree, so the first launch failed.

The earlier background tests ran without an execution engine, which skips
the delivery gate entirely; these run under vibeguide_monitor.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.claude_bg import ClaudeBgUnknown
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.models import AgentCapabilities
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.task_registry import load_task_binding

from tests.test_claude_bg_reconnect import GOOD, UUID, FakeLauncher
# Module import, not the class: a TestCase in this namespace would run twice.
from tests import test_issue_88_delivery_gate as gate88

NODE_ID = gate88.NODE_ID


def _background_node():
    node = gate88._node()
    node.contract["dispatch_topology"] = "background"
    return node


class RunnerEventShapeTests(unittest.TestCase):
    def test_delivered_event_carries_delivery_evidence_at_top_level(self):
        with tempfile.TemporaryDirectory() as root:
            report = {"payload": dict(GOOD, summary="done", task_id="forged")}
            runner = ClaudeBgRunner(lambda c, w: FakeLauncher(), delivery_reader=lambda claims: report,
                                    paths=ProjectPaths(Path(root)))
            contract = {"node_id": "n1", "role": "developer", "run_id": "run-1", "generation": 1,
                        "task_id": "developer:n1", "contract_path": "/c.md"}
            handle = runner.start(contract, Path(root))
            event = runner.poll(handle)[0]
        self.assertEqual(event.event, "delivered")
        self.assertEqual(event.data["delivery_evidence"], GOOD["delivery_evidence"])
        self.assertEqual(event.data["payload"], report["payload"])
        # Worker text never overrides the runner's own claims.
        self.assertEqual(event.data["task_id"], "developer:n1")


class BackgroundDeliveryGateTests(unittest.TestCase):
    _engine_monitor = gate88.DeliveryGateRereportTests._engine_monitor

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        save_contract(self.paths, build_contract(self.paths.root, provider="fake", host_id="local"))
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
        # Present already, so this case isolates the delivery gate.
        (self.paths.root / ".worktrees" / NODE_ID).mkdir(parents=True)

    def tearDown(self):
        self.temporary.cleanup()

    def runner(self):
        return ClaudeBgRunner(lambda c, w: FakeLauncher(), paths=self.paths)

    def start(self):
        monitor, record = self._engine_monitor([_background_node()])
        snapshot = monitor.start(record, self.runner())
        self.assertEqual(snapshot.execution_engine, "vibeguide_monitor")
        self.assertEqual(snapshot.nodes[NODE_ID]["status"], "running", snapshot.nodes[NODE_ID].get("reason"))
        return monitor, snapshot

    def test_binding_records_the_session_as_cursor_and_a_host(self):
        _monitor, snapshot = self.start()
        binding = load_task_binding(self.paths, NODE_ID, "developer", run_id=snapshot.run_id)
        self.assertEqual(binding.cursor, UUID)
        self.assertEqual(binding.host, "local")
        self.assertEqual(binding.task_id, "developer:" + NODE_ID)

    def test_self_reported_delivery_reaches_review_after_a_restart(self):
        monitor, snapshot = self.start()
        generation = load_task_binding(self.paths, NODE_ID, "developer", run_id=snapshot.run_id).generation
        ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, NODE_ID, "developer", dict(GOOD), generation
        )

        snapshot = monitor.tick(snapshot.run_id, self.runner())

        current = snapshot.nodes[NODE_ID]
        self.assertEqual(current["status"], "review", current.get("reason"))
        self.assertEqual(current["completion_marker"], "DELIVERY_COMPLETE")

    def test_delivery_without_evidence_is_a_recoverable_format_gap(self):
        monitor, snapshot = self.start()
        generation = load_task_binding(self.paths, NODE_ID, "developer", run_id=snapshot.run_id).generation
        path = ProviderActionStore(self.paths)._delivery_path(snapshot.run_id, NODE_ID, "developer", generation)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"payload": {"summary": "no evidence"}}', encoding="utf-8")

        snapshot = monitor.tick(snapshot.run_id, self.runner())

        self.assertNotEqual(snapshot.nodes[NODE_ID]["status"], "blocked_unknown")
        self.assertIn(NODE_ID, snapshot.handles)


class WorktreeBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        git = lambda *args: subprocess.run(["git", "-C", str(self.root)] + list(args),
                                           check=True, capture_output=True, text=True)
        git("init", "-q", "-b", "main")
        git("-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-q", "--allow-empty", "-m", "init")
        self.launcher = FakeLauncher()
        self.runner = ClaudeBgRunner(lambda c, w: self.launcher, delivery_reader=lambda claims: None,
                                     paths=ProjectPaths(self.root))
        self.contract = {"node_id": "n1", "role": "developer", "run_id": "run-1", "generation": 1,
                         "task_id": "developer:n1", "contract_path": "/c.md", "branch": "node/n1"}

    def tearDown(self):
        self.temporary.cleanup()

    def test_missing_node_worktree_is_created_on_the_contract_branch(self):
        worktree = self.root / ".worktrees" / "n1"
        self.runner.start(dict(self.contract), worktree)
        self.assertTrue(worktree.is_dir())
        branch = subprocess.run(["git", "-C", str(worktree), "branch", "--show-current"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(branch, "node/n1")
        self.assertEqual(self.launcher.calls[0][0], "launch")

    def test_existing_contract_branch_is_checked_out_not_recreated(self):
        subprocess.run(["git", "-C", str(self.root), "branch", "node/n1"], check=True)
        worktree = self.root / ".worktrees" / "n1"
        self.runner.start(dict(self.contract), worktree)
        branch = subprocess.run(["git", "-C", str(worktree), "branch", "--show-current"],
                                check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(branch, "node/n1")

    def test_worktree_outside_the_project_worktrees_is_refused(self):
        with tempfile.TemporaryDirectory() as elsewhere:
            target = Path(elsewhere) / "n1"
            with self.assertRaises(ClaudeBgUnknown):
                self.runner.start(dict(self.contract), target)
            self.assertFalse(target.exists())
        self.assertEqual(self.launcher.calls, [])


if __name__ == "__main__":
    unittest.main()
