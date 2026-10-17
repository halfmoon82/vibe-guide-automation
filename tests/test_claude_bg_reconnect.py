"""claude-code-background: reconnect across processes and accept self-reports.

Recorded failure (run-c216111f, 2026-10-17): the monitor registered the
placeholder identity ``developer:<node>`` while the runner kept the real
session UUID only in process memory.  In-process polls then reported an
identity the monitor did not register, a fresh ``vibe resume`` could not find
the handle at all, and ``vibe worker-deliver`` refused every report because
background dispatch writes no ``requests/`` record.
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.contracts import RunHandle
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.task_registry import load_task_binding

UUID = "1a2b3c4d-1111-4222-8333-444455556666"

GOOD = {
    "delivery_evidence": {
        "completion_marker": "DELIVERY_COMPLETE",
        "delivery_path": ".vibe/runs/run-1/bg/delivery.md",
        "thread_status": "completed",
    },
}


class FakeLauncher:
    def __init__(self, state="working"):
        self.state = state
        self.calls = []

    def launch(self, role, issue_id, contract_path):
        self.calls.append(("launch", role, issue_id))
        return {"task_id": UUID}

    def status(self, task_id):
        self.calls.append(("status", task_id))
        return self.state

    def stop(self, task_id):
        self.calls.append(("stop", task_id))

    def resume(self, task_id, message):
        self.calls.append(("resume", task_id, message))


def background_node(node_id="bg"):
    return DAGNode(
        node_id, node_id, [], [], "g1",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "dispatch_topology": "background",
            "worker_profile": {"worker": "codex", "model": "test", "reasoning": "normal", "fallbacks": [], "selection_basis": {"issue_complexity_ref": node_id, "complexity_band": "standard", "risk_tags": [], "availability_evidence": "test"}, "writer": "writer", "worktree": ".worktrees/" + node_id, "branch": "branch-" + node_id, "allowlist": [node_id + ".py"]},
        },
        "ready",
    )


class RunnerReconnectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.contract = {
            "node_id": "n1", "role": "developer", "run_id": "run-1",
            "generation": 3, "task_id": "developer:n1", "contract_path": "/c.md",
        }

    def tearDown(self):
        self.temporary.cleanup()

    def runner(self, launcher):
        return ClaudeBgRunner(lambda c, w: launcher, delivery_reader=lambda claims: None, paths=self.paths)

    def test_events_carry_the_monitor_identity_and_the_real_session(self):
        launcher = FakeLauncher("blocked")
        runner = self.runner(launcher)
        handle = runner.start(dict(self.contract), Path("/tmp"))
        data = runner.poll(handle)[0].data
        self.assertEqual(data["task_id"], "developer:n1")
        self.assertEqual(data["session_id"], UUID)
        self.assertIn(("status", UUID), launcher.calls)

    def test_a_new_runner_adopts_the_persisted_session(self):
        first = self.runner(FakeLauncher())
        handle = first.start(dict(self.contract), Path("/tmp"))

        launcher = FakeLauncher("blocked")
        events = self.runner(launcher).poll(RunHandle(handle.run_id))

        self.assertEqual([event.event for event in events], ["needs_attention"])
        self.assertEqual(launcher.calls, [("status", UUID)])
        data = events[0].data
        self.assertEqual(
            (data["task_id"], data["session_id"], data["handle_id"], data["generation"]),
            ("developer:n1", UUID, handle.run_id, 3),
        )

    def test_handles_are_unique_across_processes(self):
        first = self.runner(FakeLauncher()).start(dict(self.contract), Path("/tmp"))
        second = self.runner(FakeLauncher()).start(dict(self.contract), Path("/tmp"))
        self.assertNotEqual(first.run_id, second.run_id)

    def test_resume_generation_survives_a_restart(self):
        first = self.runner(FakeLauncher())
        handle = first.start(dict(self.contract), Path("/tmp"))
        first.resume(handle, "fix P1", 4)
        data = self.runner(FakeLauncher("blocked")).poll(handle)[0].data
        self.assertEqual(data["generation"], 4)

    def test_unknown_handle_without_record_stays_blocked_unknown(self):
        events = self.runner(FakeLauncher()).poll(RunHandle("claude-bg-n1-ghost"))
        self.assertEqual([event.event for event in events], ["blocked_unknown"])


class MonitorBackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text('{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8")
        save_contract(self.paths, build_contract(self.paths.root, provider="fake", host_id="local"))
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
        self.nodes = [background_node()]
        self.plan = Plan("plan-1", 1, "docs/prd.md", ["bg"], "draft")

    def tearDown(self):
        self.temporary.cleanup()

    def monitor(self):
        return Monitor(self.paths, self.plan, self.nodes)

    def start(self, launcher):
        with patch("vibe_guide.dag._write_scope_conflict_errors", return_value={}):
            card = build_authorization_card(self.plan, self.nodes, self.capabilities)
        runner = ClaudeBgRunner(lambda c, w: launcher, paths=self.paths)
        snapshot = self.monitor().start(authorize(card, "AUTHORIZE"), runner)
        self.assertEqual(snapshot.nodes["bg"]["status"], "running")
        return snapshot

    def test_in_process_poll_matches_the_registered_task(self):
        launcher = FakeLauncher()
        snapshot = self.start(launcher)
        launcher.state = "blocked"
        runner = ClaudeBgRunner(lambda c, w: launcher, paths=self.paths)
        snapshot = self.monitor().tick(snapshot.run_id, runner)
        self.assertNotEqual(
            snapshot.nodes["bg"].get("reason"),
            "event provenance conflicts with the registered task",
        )

    def test_restarted_monitor_keeps_polling_the_same_session(self):
        snapshot = self.start(FakeLauncher())
        launcher = FakeLauncher("working")
        restarted = ClaudeBgRunner(lambda c, w: launcher, paths=self.paths)

        snapshot = self.monitor().tick(snapshot.run_id, restarted)

        self.assertEqual(snapshot.nodes["bg"]["status"], "running")
        self.assertIn(("status", UUID), launcher.calls)

    def test_background_self_report_is_accepted_and_reaches_the_runner(self):
        snapshot = self.start(FakeLauncher())
        generation = load_task_binding(self.paths, "bg", "developer", run_id=snapshot.run_id).generation
        store = ProviderActionStore(self.paths)

        outcome = store.record_worker_delivery(snapshot.run_id, "bg", "developer", dict(GOOD), generation)

        self.assertTrue(outcome["recorded"])
        restarted = ClaudeBgRunner(lambda c, w: FakeLauncher(), paths=self.paths)
        events = restarted.poll(RunHandle(snapshot.handles["bg"]))
        self.assertEqual([event.event for event in events], ["delivered"])
        self.assertEqual(events[0].data["payload"], GOOD)

    def test_background_self_report_rejects_other_generations(self):
        snapshot = self.start(FakeLauncher())
        generation = load_task_binding(self.paths, "bg", "developer", run_id=snapshot.run_id).generation
        store = ProviderActionStore(self.paths)
        for wrong in (generation - 1, generation + 1):
            if wrong < 1:
                continue
            with self.assertRaises(ValueError):
                store.record_worker_delivery(snapshot.run_id, "bg", "developer", dict(GOOD), wrong)

    def test_self_report_for_an_undispatched_node_is_still_refused(self):
        snapshot = self.start(FakeLauncher())
        store = ProviderActionStore(self.paths)
        with self.assertRaisesRegex(ValueError, "no dispatched task"):
            store.record_worker_delivery(snapshot.run_id, "other", "developer", dict(GOOD), 1)
        with self.assertRaisesRegex(ValueError, "no dispatched task"):
            store.record_worker_delivery(snapshot.run_id, "bg", "reviewer", dict(GOOD), 1)


if __name__ == "__main__":
    unittest.main()
