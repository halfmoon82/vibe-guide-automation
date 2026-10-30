import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_monitor as tm
from vibe_guide.adapters.claude_bg import ClaudeBgLauncher, ClaudeBgUnknown
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner

UUID = "1a2b3c4d-1111-4222-8333-444455556666"


class StopOriginMonitorTests(unittest.TestCase):
    setUp = tm.MonitorTests.setUp
    authorized_monitor = tm.MonitorTests.authorized_monitor

    def run_with(self, data):
        monitor, record = self.authorized_monitor([tm.node("n1")])
        runner = tm.FakeRunner(events={("n1", "developer"): [("stopped", dict(data))]})
        snapshot = monitor.start(record, runner)
        return monitor, runner, monitor.tick(snapshot.run_id, runner)

    def test_unexpected_stop_retries_once_then_fails(self):
        monitor, runner, snapshot = self.run_with(
            {"reason": "session stopped", "stop_origin": "unexpected"})
        self.assertNotIn(snapshot.nodes["n1"]["status"], {"stopped", "failed"})
        self.assertEqual(len(runner.start_calls), 2)
        self.assertEqual(snapshot.nodes["n1"]["status"], "running")
        # A second unexpected stop is final.
        runner.events[("n1", "developer")] = [
            ("stopped", {"reason": "session stopped", "stop_origin": "unexpected"})]
        again = monitor.tick(snapshot.run_id, runner)
        self.assertNotEqual(again.nodes["n1"]["status"], "running")

    def test_persistent_unexpected_stop_redispatches_once_then_fails(self):
        class AlwaysStopped(tm.FakeRunner):
            def poll(self, handle):
                self.events[("n1", "developer")] = [
                    ("stopped", {"reason": "session stopped",
                                 "stop_origin": "unexpected"})]
                return super().poll(handle)

        monitor, record = self.authorized_monitor([tm.node("n1")])
        runner = AlwaysStopped()
        snapshot = monitor.start(record, runner)
        clock = [1000.0]
        with patch("time.time", side_effect=lambda: clock[0]):
            for _ in range(8):
                snapshot = monitor.tick(snapshot.run_id, runner)
                clock[0] += 500
                if snapshot.nodes["n1"]["status"] in {"stopped", "failed"}:
                    break
        self.assertEqual(len(runner.start_calls), 2)
        self.assertIn(snapshot.nodes["n1"]["status"], {"stopped", "failed"})

    def test_vibe_initiated_or_unrecorded_stop_stays_stopped(self):
        for data in ({"reason": "x", "stop_origin": "vibe"}, {"reason": "x"}):
            _, _, snapshot = self.run_with(data)
            self.assertEqual(snapshot.nodes["n1"]["status"], "stopped")


class FakeLauncher:
    def __init__(self, state="working"):
        self.state = state
        self.calls = []

    def launch(self, role, issue_id, contract_path):
        self.calls.append("launch")
        return {"task_id": UUID}

    def status(self, task_id):
        return self.state

    def stop(self, task_id):
        self.calls.append("stop")
        self.state = "stopped"


class RunnerStopOriginTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.launcher = FakeLauncher()
        self.runner = ClaudeBgRunner(
            lambda c, w: self.launcher, delivery_reader=lambda claims: None,
            paths=ProjectPaths(Path(self.tmp.name)))
        self.contract = {"node_id": "n1", "role": "developer", "run_id": "r1",
                         "generation": 1, "contract_path": "/c.md"}

    def test_stop_origin_recorded_only_for_vibe_stop(self):
        handle = self.runner.start(self.contract, Path("/tmp"))
        self.launcher.state = "stopped"
        self.assertEqual(self.runner.poll(handle)[0].data["stop_origin"], "unexpected")
        self.launcher.state = "working"
        self.runner.stop(handle)
        self.assertEqual(self.runner.poll(handle)[0].data["stop_origin"], "vibe")

    def test_vibe_stop_survives_runner_restart(self):
        handle = self.runner.start(self.contract, Path("/tmp"))
        self.runner.stop(handle)
        fresh = ClaudeBgRunner(
            lambda c, w: self.launcher, delivery_reader=lambda claims: None,
            paths=ProjectPaths(Path(self.tmp.name)))
        self.assertEqual(fresh.poll(handle)[0].data["stop_origin"], "vibe")

    def test_second_dispatch_beside_live_session_is_refused(self):
        """D6 regression: a retry start must not launch beside a live same-node session."""
        self.runner.start(self.contract, Path("/tmp"))
        live = {"live": ["1a2b3c4d"], "unknown": None}
        with patch("vibe_guide.live_sessions.probe_live_sessions", return_value=live):
            with self.assertRaises(ClaudeBgUnknown):
                self.runner.start(dict(self.contract, generation=2), Path("/tmp"))
        self.assertEqual(self.launcher.calls.count("launch"), 1)


    def test_reviewer_dispatch_ignores_developer_session(self):
        self.runner.start(self.contract, Path("/tmp"))
        live = {"live": ["1a2b3c4d"], "unknown": None}
        with patch("vibe_guide.live_sessions.probe_live_sessions", return_value=live) as probe:
            self.runner.start(dict(self.contract, role="reviewer"), Path("/tmp"))
        probe.assert_not_called()
        self.assertEqual(self.launcher.calls.count("launch"), 2)

    def test_unknown_probe_fails_closed(self):
        self.runner.start(self.contract, Path("/tmp"))
        unknown = {"live": [], "unknown": "claude agents could not be read"}
        with patch("vibe_guide.live_sessions.probe_live_sessions", return_value=unknown):
            with self.assertRaises(ClaudeBgUnknown):
                self.runner.start(dict(self.contract, generation=2), Path("/tmp"))
        with patch("vibe_guide.live_sessions.probe_live_sessions", side_effect=OSError):
            with self.assertRaises(ClaudeBgUnknown):
                self.runner.start(dict(self.contract, generation=2), Path("/tmp"))
        self.assertEqual(self.launcher.calls.count("launch"), 1)


class BranchExistsTests(unittest.TestCase):
    def test_remote_only_ref_is_not_an_existing_branch(self):
        with tempfile.TemporaryDirectory() as raw:
            repo = Path(raw)
            git = lambda *a: subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
            git("init", "-q")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-qm", "i")
            git("update-ref", "refs/remotes/origin/node/x", "HEAD")
            launcher = ClaudeBgLauncher.__new__(ClaudeBgLauncher)
            launcher._run = lambda cmd, cwd: subprocess.run(
                cmd, cwd=cwd, capture_output=True, text=True)
            self.assertFalse(launcher._branch_exists(repo, "node/x"))
            git("branch", "node/x")
            self.assertTrue(launcher._branch_exists(repo, "node/x"))


if __name__ == "__main__":
    unittest.main()
