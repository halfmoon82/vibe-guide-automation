"""#376: a gateway 400 on a background session is a transient provider failure.

The session exits as ``failed``; the node must not park in blocked_unknown.
Recovery order (prd-guide section 7): same task with backoff first, a new
session only once the old one cannot be continued, escalation after three
identical failures.  ``reason_code=provider_http_400`` must reach events.jsonl
verbatim.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_monitor as tm
from vibe_guide.adapters.claude_bg import ClaudeBgLauncher
from vibe_guide.runners.claude_bg import ClaudeBgRunner, PROVIDER_HTTP_400
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_snapshot

UUID = "1a2b3c4d-1111-4222-8333-444455556666"


def failure(resumable=True):
    return ("failed_retryable", {
        "reason": "session failed", "reason_code": PROVIDER_HTTP_400, "resumable": resumable})


class TransientFailureMonitorTests(unittest.TestCase):
    setUp = tm.MonitorTests.setUp
    authorized_monitor = tm.MonitorTests.authorized_monitor

    def events(self, snapshot):
        path = self.paths.vibe / "runs" / snapshot.run_id / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def run_once(self, resumable=True):
        monitor, record = self.authorized_monitor([tm.node("n1")])
        runner = tm.FakeRunner(events={("n1", "developer"): [failure(resumable)]})
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        return monitor, runner, snapshot

    def test_400_is_retried_not_blocked_and_code_is_in_events(self):
        monitor, runner, snapshot = self.run_once()
        self.assertNotIn(snapshot.nodes["n1"]["status"], {"blocked_unknown", "failed", "stopped"})
        codes = [e.get("data", e).get("reason_code") for e in self.events(snapshot)]
        self.assertIn(PROVIDER_HTTP_400, codes)

    def test_same_task_continuation_creates_no_new_session(self):
        monitor, runner, snapshot = self.run_once(resumable=True)
        retry = snapshot.nodes["n1"].get("retryable_action") or {}
        self.assertTrue(retry.get("same_task") or snapshot.nodes["n1"]["status"] == "running")
        self.assertIsNot(retry.get("successor"), True)
        # every start after the failure is a continuation of the same task
        self.assertTrue(all(c.get("successor") is not True for c in runner.start_calls[1:]))
        self.assertEqual(len(runner.start_calls), 2)
        self.assertIs(runner.start_calls[1].get("continuation"), True)

    def test_non_resumable_session_gets_a_successor_after_exit_is_recorded(self):
        monitor, runner, snapshot = self.run_once(resumable=False)
        node = snapshot.nodes["n1"]
        self.assertNotEqual(node["status"], "blocked_unknown")
        self.assertEqual(len(runner.start_calls), 2)
        self.assertIs(runner.start_calls[1].get("successor"), True)
        self.assertIs(runner.start_calls[1].get("continuation"), False)
        self.assertEqual(runner.start_calls[1].get("predecessor_task_id"), "developer:n1")

    def test_three_consecutive_failures_escalate_naming_provider_400(self):
        class Always400(tm.FakeRunner):
            def poll(self, handle):
                self.events[("n1", "developer")] = [failure()]
                return super().poll(handle)

        monitor, record = self.authorized_monitor([tm.node("n1")])
        runner = Always400()
        snapshot = monitor.start(record, runner)
        clock = [1000.0]
        with patch("time.time", side_effect=lambda: clock[0]):
            for _ in range(12):
                snapshot = monitor.tick(snapshot.run_id, runner)
                clock[0] += 500
                if snapshot.nodes["n1"]["status"] == "blocked_unknown":
                    break
        node = snapshot.nodes["n1"]
        self.assertEqual(node["status"], "blocked_unknown")
        self.assertIn("provider 400", node["reason"])
        self.assertEqual(node.get("reason_code"), PROVIDER_HTTP_400)
        names = [e.get("event") or e.get("type") for e in self.events(snapshot)]
        self.assertIn("provider_failure_escalated", names)
        reloaded = load_snapshot(self.paths, snapshot.run_id).nodes["n1"]
        self.assertEqual(reloaded["status"], "blocked_unknown")
        escalated = [e for e in self.events(snapshot) if e.get("event") == "provider_failure_escalated"][0]
        self.assertIn("provider 400", escalated["data"]["error"])
        self.assertEqual(escalated["data"]["reason_code"], PROVIDER_HTTP_400)

    def test_other_failed_events_are_unchanged(self):
        monitor, record = self.authorized_monitor([tm.node("n1")])
        runner = tm.FakeRunner(events={("n1", "developer"): [("failed_retryable", {"reason": "session failed"})]})
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "blocked_unknown")
        self.assertNotIn("provider_transient_failure",
                         [e.get("event") or e.get("type") for e in self.events(snapshot)])


class RetireDeadSessionTests(unittest.TestCase):
    setUp = tm.MonitorTests.setUp
    authorized_monitor = tm.MonitorTests.authorized_monitor

    def stuck(self):
        monitor, record = self.authorized_monitor([tm.node("n1")])
        runner = tm.FakeRunner()
        snapshot = monitor.start(record, runner)
        return monitor, runner, snapshot

    def test_dead_session_is_retired_and_node_redispatches(self):
        monitor, runner, snapshot = self.stuck()
        runner.session_state = lambda handle: "failed"
        result = monitor.retire_dead_session("n1", snapshot.run_id, runner)
        self.assertTrue(result["retired"])
        after = load_snapshot(self.paths, snapshot.run_id)
        self.assertNotIn("n1", after.handles)
        self.assertIs(after.nodes["n1"]["retryable_action"]["successor"], True)
        after = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(len(runner.start_calls), 2)
        self.assertIs(runner.start_calls[1].get("successor"), True)
        self.assertEqual(after.nodes["n1"]["status"], "running")

    def refuses(self, probe):
        monitor, runner, snapshot = self.stuck()
        runner.session_state = probe
        result = monitor.retire_dead_session("n1", snapshot.run_id, runner)
        self.assertFalse(result["retired"])
        self.assertTrue(result["reason"])
        monitor.tick(snapshot.run_id, runner)
        self.assertEqual(len(runner.start_calls), 1)
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).handles, {"n1": "fake-n1-1"})

    def test_live_session_is_not_retired(self):
        self.refuses(lambda handle: "working")

    def test_unreadable_session_is_not_retired(self):
        self.refuses(lambda handle: 1 / 0)


class FakeLauncher:
    def __init__(self, state="failed", transcript=None):
        self.state = state
        self.transcript = transcript

    def launch(self, role, issue_id, contract_path):
        return {"task_id": UUID}

    def status(self, task_id):
        return self.state

    def failure_class(self, task_id):
        return PROVIDER_HTTP_400 if self.transcript == "400" else None

    def can_resume(self, task_id):
        return self.transcript is not None


class RunnerClassificationTests(unittest.TestCase):
    def poll(self, launcher):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        runner = ClaudeBgRunner(
            lambda c, w: launcher, delivery_reader=lambda claims: None,
            paths=ProjectPaths(Path(tmp.name)))
        handle = runner.start({"node_id": "n1", "role": "developer", "run_id": "r1",
                               "generation": 1, "contract_path": "/c.md"}, Path("/tmp"))
        return runner.poll(handle)[0]

    def test_400_failure_carries_code_and_resumable(self):
        event = self.poll(FakeLauncher(transcript="400"))
        self.assertEqual(event.event, "failed_retryable")
        self.assertEqual(event.data["reason_code"], PROVIDER_HTTP_400)
        self.assertIs(event.data["resumable"], True)

    def test_unclassified_failure_keeps_old_shape(self):
        event = self.poll(FakeLauncher(transcript=None))
        self.assertEqual(event.data["reason"], "session failed")
        self.assertNotIn("reason_code", event.data)


class TranscriptClassifierTests(unittest.TestCase):
    def launcher(self, lines):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "t.jsonl"
        path.write_text("\n".join(json.dumps(i) for i in lines), encoding="utf-8")
        launcher = ClaudeBgLauncher.__new__(ClaudeBgLauncher)
        launcher.transcript_locator = lambda task_id: path
        return launcher

    def test_api_error_400_is_classified(self):
        launcher = self.launcher([
            {"type": "user"},
            {"type": "assistant", "isApiErrorMessage": True,
             "message": {"content": [{"type": "text", "text": "API Error: 400 model not supported"}]}}])
        self.assertEqual(launcher.failure_class(UUID), PROVIDER_HTTP_400)
        self.assertTrue(launcher.can_resume(UUID))

    def test_other_errors_and_plain_text_are_not(self):
        other = self.launcher([{"type": "assistant", "isApiErrorMessage": True,
                                "message": {"content": "API Error: 529 overloaded"}}])
        self.assertIsNone(other.failure_class(UUID))
        plain = self.launcher([{"type": "assistant",
                                "message": {"content": "I fixed HTTP 400 handling"}}])
        self.assertIsNone(plain.failure_class(UUID))


if __name__ == "__main__":
    unittest.main()
