import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide import daemon as dm
from vibe_guide.paths import ProjectPaths
from unittest import mock

import vibe_guide.monitor as monitor_module
from vibe_guide.monitor import Monitor
from vibe_guide.state import load_events, run_dir, runs_archive_dir

RUN = "run-" + "e" * 32


def node(status, **extra):
    return {"status": status, "worktree": ".worktrees/n", **extra}


class Supervisor:
    """Scripted run_once; archive goes through the real state.archive_run."""

    def __init__(self, snapshots):
        self.snapshots = list(snapshots)
        self.calls = 0
        self.released = False
        self.lease = SimpleNamespace(release=self.release)

    def release(self, run_id):
        self.released = True

    def recover_or_start(self):
        return {"active_supervisors": 1}

    def run_once(self):
        self.calls += 1
        return self.snapshots.pop(0) if len(self.snapshots) > 1 else self.snapshots[0]


def snap(status, nodes=None):
    return SimpleNamespace(status=status, nodes=nodes or {"n": node("accepted")}, tasks={}, authorization={})


class CloseoutBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name), vibe_home=Path(self.tmp.name) / "home")
        (Path(self.tmp.name) / "home").mkdir()
        run_dir(self.paths, RUN, create=True)
        (run_dir(self.paths, RUN) / "state.json").write_text(
            json.dumps({"status": "complete", "run_id": RUN}), encoding="utf-8")
        self.order = []
        self.probe = {"live": [], "unknown": None}
        self.now = [1000.0]
        self.released_while_closing = False
        self.monitor = Monitor.__new__(Monitor)
        self.monitor.paths = self.paths

    def set_run_status(self, status):
        (run_dir(self.paths, RUN) / "state.json").write_text(
            json.dumps({"status": status, "run_id": RUN}), encoding="utf-8")

    def archive(self):
        self.order.append("archive")
        return self.monitor.archive_run(RUN, sessions_verified=True)

    def tick_clock(self, _seconds):
        self.now[0] += 30
        if "lease_release" in self.order:
            self.released_while_closing = True

    def notifier(self, message, kind, node_id=""):
        self.order.append("push:" + kind)
        inbox = run_dir(self.paths, RUN) / dm.PM_INBOX_FILE
        with open(str(inbox), "a", encoding="utf-8") as stream:
            stream.write(json.dumps({"kind": kind, "message": message}) + "\n")
        return {"status": "queued_inbox"}

    def make(self, supervisor, **config):
        values = {"interval": 1, "heal_interval": 10 ** 9, "blocked_grace": 60}
        values.update(config)
        daemon = dm.Daemon(
            self.paths, RUN, supervisor, dm.DaemonConfig(**values), clock=lambda: self.now[0],
            sleep=self.tick_clock, notifier=self.notifier,
            assess=lambda: {"status": "ok", "nodes": {}}, archive=self.archive,
            sessions=lambda snapshot: self.probe)
        original = supervisor.release
        supervisor.release = lambda run_id: (self.order.append("lease_release"), original(run_id))[1]
        supervisor.lease = SimpleNamespace(release=supervisor.release)
        return daemon

    def assertNoResidue(self):
        runs = self.paths.vibe / "runs"
        self.assertEqual(sorted(p.name for p in runs.iterdir()) if runs.is_dir() else [], [])

    def events(self):
        return [e["event"] for e in load_events(self.paths, RUN)]


class TerminalRunsArchiveTests(CloseoutBase):
    def test_each_terminal_status_is_announced_then_released_then_archived(self):
        for status in ("complete", "failed", "blocked_design", "stopped"):
            with self.subTest(status=status):
                self.setUp()
                self.set_run_status(status)
                supervisor = Supervisor([snap(status)])
                outcome = self.make(supervisor).run(max_cycles=10)
                self.assertEqual(outcome, {"status": "terminal", "run_status": status})
                self.assertEqual(self.order[:3], ["push:run_" + status, "lease_release", "archive"])
                self.assertNoResidue()
                archive = runs_archive_dir(self.paths, RUN)
                self.assertTrue((archive / "archive-manifest.json").is_file())
                self.assertIn("run_archived", self.events())
                inbox = (archive / dm.PM_INBOX_FILE).read_text(encoding="utf-8")
                self.assertIn("run_" + status, inbox)
                record = json.loads((archive / dm.DAEMON_FILE).read_text(encoding="utf-8"))
                self.assertEqual(record["state"], "exited")

    def test_unanswered_requests_leave_pending_with_the_archive(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore
        store = ProviderActionStore(self.paths)
        request = store.root / "requests" / "action-closeout-1.json"
        request.parent.mkdir(parents=True, exist_ok=True)
        request.write_text(json.dumps({
            "schema_version": 1, "action_id": "action-closeout-1", "request_digest": "d" * 64,
            "operation": "wait", "provider": "claude-code", "run_id": RUN, "issue_id": "n",
            "role": "developer", "generation": 1, "request": {"authorization_digest": "a" * 64}}), encoding="utf-8")
        self.assertEqual(len(store.pending(RUN)), 1)
        self.make(Supervisor([snap("complete")])).run(max_cycles=10)
        self.assertEqual(store.pending(RUN), [])

    def test_archive_failure_leaves_an_event_retries_then_gives_up(self):
        with mock.patch.object(monitor_module, "_archive_run_artifacts", side_effect=OSError("disk full")):
            outcome = self.make(Supervisor([snap("complete")])).run(max_cycles=10)
        self.assertEqual(outcome["status"], "archive_failed")
        self.assertEqual(self.order.count("archive"), 3)
        self.assertEqual(self.events().count("run_archive_failed"), 1)
        self.assertNotIn("run_archived", self.events())
        self.assertTrue((run_dir(self.paths, RUN) / "state.json").is_file())
        self.assertFalse((runs_archive_dir(self.paths, RUN) / "archive-manifest.json").exists())


class LiveSessionTests(CloseoutBase):
    def blocked(self):
        return snap("blocked_unknown", {"n": node("blocked_unknown")})

    def test_live_session_defers_the_archive_and_keeps_the_lease(self):
        self.probe = {"live": ["abcd1234"], "unknown": None}
        daemon = self.make(Supervisor([snap("complete")]))
        outcome = daemon.run(max_cycles=4)
        self.assertEqual(outcome["status"], "max_cycles")
        self.assertNotIn("archive", self.order)
        self.assertFalse(self.released_while_closing)
        self.assertEqual(self.events().count("run_archive_deferred"), 1)
        self.assertTrue((run_dir(self.paths, RUN) / "state.json").is_file())

    def test_unknown_session_state_defers_too(self):
        self.probe = {"live": [], "unknown": "claude agents could not be read"}
        self.make(Supervisor([snap("complete")])).run(max_cycles=3)
        self.assertNotIn("archive", self.order)

    def test_archive_proceeds_once_the_session_is_gone(self):
        self.probe = {"live": ["abcd1234"], "unknown": None}
        supervisor = Supervisor([snap("complete")])
        daemon = self.make(supervisor)
        daemon.run(max_cycles=2)
        self.probe = {"live": [], "unknown": None}
        outcome = daemon.run(max_cycles=5)
        self.assertEqual(outcome, {"status": "terminal", "run_status": "complete"})
        self.assertIn("archive", self.order)

    def test_a_long_deferral_is_reported_once_without_a_decision_card(self):
        self.probe = {"live": ["abcd1234"], "unknown": None}
        daemon = self.make(Supervisor([snap("complete")]), blocked_grace=60)
        daemon.run(max_cycles=8)
        self.assertEqual(self.order.count("push:escalation"), 0)
        self.assertEqual(self.order.count("push:unresolved_report"), 1)
        self.assertNotIn("archive", self.order)


class ParkedRunTests(LiveSessionTests):
    def test_blocked_unknown_waits_for_the_ladder_then_closes(self):
        supervisor = Supervisor([self.blocked()])
        daemon = self.make(supervisor, blocked_grace=10 ** 6)
        outcome = daemon.run(max_cycles=5)
        self.assertEqual(outcome["status"], "max_cycles")
        self.assertNotIn("archive", self.order)

    def test_blocked_unknown_closes_once_every_blocked_node_was_escalated(self):
        supervisor = Supervisor([self.blocked()])
        daemon = self.make(supervisor, blocked_grace=10 ** 6)
        state = daemon._heal_state()
        state["nodes"]["n"] = {"escalated": True}
        daemon._save_heal_state(state)
        outcome = daemon.run(max_cycles=5)
        self.assertEqual(outcome, {"status": "terminal", "run_status": "blocked_unknown"})
        self.assertEqual(self.order[:3], ["push:unresolved_report", "push:run_blocked_unknown", "lease_release"])
        self.assertIn("archive", self.order)
        self.assertNoResidue()

    def test_a_node_tracked_before_parking_is_still_reported_at_close_out(self):
        supervisor = Supervisor([self.blocked()])
        daemon = self.make(supervisor, blocked_grace=10 ** 6)
        state = daemon._heal_state()
        state["nodes"]["n"] = {"fingerprint": "from-an-earlier-stall", "escalated": False, "repairs": 1}
        daemon._save_heal_state(state)
        daemon.run(max_cycles=1)
        daemon._unresolved("n", "run_parked", "parked")
        state = daemon._heal_state()
        self.assertEqual(list(dm.live_unresolved(state, daemon._snapshot)), ["n"])

    def test_a_failed_inbox_write_keeps_the_unresolved_report_pending(self):
        supervisor = Supervisor([self.blocked()])
        daemon = self.make(supervisor, blocked_grace=10 ** 6)
        results = [{"status": "inbox_failed", "reason": "OSError"}]
        daemon._push = lambda text, kind, node_id="": results.pop(0) if results else {"status": "queued_inbox"}
        daemon._unresolved("n", "run_parked", "parked")
        state = daemon._heal_state()
        self.assertFalse(daemon._report_unresolved(None, state, final=True))
        self.assertEqual([k for k in state["pushed"] if k.startswith("unresolved:")], [])
        self.assertTrue(daemon._report_unresolved(None, state, final=True))   # retried, delivered
        self.assertEqual(len([k for k in state["pushed"] if k.startswith("unresolved:")]), 1)

    def test_blocked_unknown_closes_after_the_grace_period_even_unescalated(self):
        supervisor = Supervisor([self.blocked()])
        outcome = self.make(supervisor, blocked_grace=60).run(max_cycles=20)
        self.assertEqual(outcome, {"status": "terminal", "run_status": "blocked_unknown"})
        self.assertNoResidue()

    def test_blocked_unknown_with_a_live_session_is_never_archived(self):
        self.probe = {"live": ["abcd1234"], "unknown": None}
        supervisor = Supervisor([self.blocked()])
        daemon = self.make(supervisor, blocked_grace=60)
        daemon.run(max_cycles=20)
        self.assertNotIn("archive", self.order)
        self.assertFalse(self.released_while_closing)

    def test_a_node_still_moving_keeps_the_run_open(self):
        mixed = snap("blocked_unknown", {"a": node("blocked_unknown"), "b": node("running")})
        outcome = self.make(Supervisor([mixed]), blocked_grace=0).run(max_cycles=4)
        self.assertEqual(outcome["status"], "max_cycles")
        self.assertNotIn("archive", self.order)

    def test_closed_out_run_gets_no_further_ticks(self):
        supervisor = Supervisor([self.blocked()])
        daemon = self.make(supervisor, blocked_grace=60)
        daemon.run(max_cycles=20)
        calls = supervisor.calls
        self.assertGreater(calls, 0)
        self.assertLessEqual(calls, 4)


class LiveSessionProbeTests(CloseoutBase):
    """The archive guard itself, with ``claude agents`` replaced by a scripted list."""

    SESSION = "3c2b5d0c-4959-456a-8713-d8ee40a5b380"

    def snapshot(self):
        return SimpleNamespace(
            status="blocked_unknown",
            nodes={"n": node("blocked_unknown", worktree=str(Path(self.tmp.name) / ".worktrees" / "n"))},
            tasks={"n:developer": {"mode": "background", "task_id": self.SESSION, "status": "start_pending"}})

    def agents(self, entries=None, error=None):
        from vibe_guide.adapters.claude_bg import ClaudeBgUnknown

        def listing(config, cwd, executor=None):
            if error:
                raise ClaudeBgUnknown(error)
            return entries
        return mock.patch.multiple(
            "vibe_guide.adapters.claude_bg", list_agent_sessions=listing,
            load_claude_bg_config=lambda paths: SimpleNamespace())

    def run_probe(self, **kwargs):
        from vibe_guide.live_sessions import probe_live_sessions
        with self.agents(**kwargs):
            return probe_live_sessions(self.paths, self.snapshot())

    def test_working_session_of_the_run_is_live(self):
        result = self.run_probe(entries=[{"sessionId": self.SESSION, "state": "working", "cwd": "/elsewhere"}])
        self.assertEqual(result, {"live": [self.SESSION[:8]], "unknown": None})

    def test_blocked_session_counts_as_live(self):
        result = self.run_probe(entries=[{"sessionId": self.SESSION, "state": "blocked", "cwd": "/elsewhere"}])
        self.assertEqual(result["live"], [self.SESSION[:8]])

    def test_session_with_no_state_counts_as_live(self):
        result = self.run_probe(entries=[{"sessionId": self.SESSION, "cwd": "/elsewhere"}])
        self.assertEqual(result["live"], [self.SESSION[:8]])

    def test_unrelated_session_in_another_directory_is_ignored(self):
        other = "aaaaaaaa-0000-0000-0000-000000000000"
        result = self.run_probe(entries=[{"sessionId": other, "state": "working", "cwd": "/somewhere/else"}])
        self.assertEqual(result, {"live": [], "unknown": None})

    def test_unregistered_session_inside_a_node_worktree_is_live(self):
        other = "bbbbbbbb-0000-0000-0000-000000000000"
        cwd = str(Path(self.tmp.name) / ".worktrees" / "n")
        result = self.run_probe(entries=[{"sessionId": other, "state": "working", "cwd": cwd}])
        self.assertEqual(result["live"], [other[:8]])

    def test_retired_sessions_do_not_block(self):
        entries = [{"sessionId": self.SESSION, "state": state, "cwd": "/x"} for state in ("stopped", "done", "failed")]
        self.assertEqual(self.run_probe(entries=entries), {"live": [], "unknown": None})

    def test_unreadable_agent_list_is_unknown_not_empty(self):
        result = self.run_probe(error="claude agents failed (exit 1): boom")
        self.assertEqual(result["live"], [])
        self.assertIn("claude agents could not be read", result["unknown"])

    def test_missing_snapshot_is_unknown(self):
        from vibe_guide.live_sessions import probe_live_sessions
        self.assertIn("unreadable", probe_live_sessions(self.paths, None)["unknown"])

    def test_visible_binding_stays_live_until_the_registry_retires_it(self):
        from vibe_guide.live_sessions import probe_live_sessions
        snapshot = SimpleNamespace(status="blocked_unknown", nodes={}, tasks={
            "n:developer": {"mode": "visible", "task_id": self.SESSION, "status": "running"}})
        self.assertEqual(probe_live_sessions(self.paths, snapshot)["live"], [self.SESSION[:8]])
        snapshot.tasks["n:developer"]["status"] = "stopped"
        self.assertEqual(probe_live_sessions(self.paths, snapshot)["live"], [])


class ManualArchiveGuardTests(LiveSessionProbeTests):
    def setUp(self):
        super().setUp()
        self.set_run_status("blocked_unknown")

    def archive_manually(self, **agents):
        from vibe_guide import monitor as monitor_module
        with self.agents(**agents), mock.patch.object(monitor_module, "load_snapshot", return_value=self.snapshot()):
            return self.monitor.archive_run(RUN)

    def test_live_session_refuses_the_archive_and_leaves_the_run_in_place(self):
        from vibe_guide.live_sessions import RunArchiveDeferred
        with self.assertRaises(RunArchiveDeferred) as caught:
            self.archive_manually(entries=[{"sessionId": self.SESSION, "state": "working", "cwd": "/x"}])
        self.assertIn(self.SESSION[:8], str(caught.exception))
        self.assertTrue((self.paths.vibe / "runs" / RUN / "state.json").is_file())
        self.assertNotIn("run_archived", self.events())

    def test_unreadable_agent_list_refuses_the_archive(self):
        from vibe_guide.live_sessions import RunArchiveDeferred
        with self.assertRaises(RunArchiveDeferred):
            self.archive_manually(error="claude agents failed (exit 1): boom")
        self.assertTrue((self.paths.vibe / "runs" / RUN).is_dir())

    def test_no_live_session_archives(self):
        manifest = self.archive_manually(entries=[{"sessionId": self.SESSION, "state": "stopped", "cwd": "/x"}])
        self.assertTrue(manifest["archived"])
        self.assertNoResidue()
        self.assertIn("run_archived", self.events())

    def test_complete_run_needs_no_session_check(self):
        self.set_run_status("complete")
        manifest = self.monitor.archive_run(RUN)
        self.assertTrue(manifest["archived"])


if __name__ == "__main__":
    unittest.main()
