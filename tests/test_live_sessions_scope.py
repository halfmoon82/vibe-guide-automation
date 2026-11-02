"""Live-session check before archiving counts only this run's own sessions (#377)."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from vibe_guide.live_sessions import probe_live_sessions
from vibe_guide.paths import ProjectPaths

RUN = "run-" + "a" * 32
OTHER_RUN = "run-" + "b" * 32
BOUND = "3c2b5d0c-4959-456a-8713-d8ee40a5b380"
OLD = "aaaaaaaa-0000-0000-0000-000000000000"
UNBOUND = "bbbbbbbb-0000-0000-0000-000000000000"


class LiveSessionScope(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        self.records = self.paths.vibe / "provider-actions" / "claude-bg-sessions"

    def snapshot(self, bound=True):
        tasks = {"n:developer": {"mode": "background", "task_id": BOUND, "status": "running"}} if bound else {}
        return SimpleNamespace(run_id=RUN, status="blocked_unknown", tasks=tasks, nodes={
            "n": {"status": "blocked_unknown", "worktree": str(Path(self.tmp.name) / ".worktrees" / "n")}})

    def launch_record(self, session, run=RUN, node="n", generation=1):
        self.records.mkdir(parents=True, exist_ok=True)
        (self.records / ("claude-bg-%s-1-abcd1234.json" % node)).write_text(json.dumps({
            "claims": {"run_id": run, "node_id": node, "generation": generation, "session_id": session}}))

    def probe(self, entries, snapshot=None, error=None):
        from vibe_guide.adapters.claude_bg import ClaudeBgUnknown

        def listing(config, cwd, executor=None):
            if error:
                raise ClaudeBgUnknown(error)
            return entries
        with mock.patch.multiple("vibe_guide.adapters.claude_bg", list_agent_sessions=listing,
                                 load_claude_bg_config=lambda paths: SimpleNamespace()):
            return probe_live_sessions(self.paths, snapshot or self.snapshot())

    def test_old_run_session_inside_same_named_worktree_does_not_defer(self):
        cwd = str(Path(self.tmp.name) / ".worktrees" / "n")
        result = self.probe([{"sessionId": OLD, "state": "working", "cwd": cwd},
                             {"sessionId": BOUND, "state": "stopped", "cwd": cwd}])
        self.assertEqual(result, {"live": [], "unknown": None})

    def test_launched_but_not_yet_bound_session_defers(self):
        self.launch_record(UNBOUND)
        result = self.probe([{"sessionId": UNBOUND, "state": "working", "cwd": "/x"}],
                            self.snapshot(bound=False))
        self.assertEqual(result, {"live": [UNBOUND[:8]], "unknown": None})

    def test_launch_record_of_another_run_is_not_counted(self):
        self.launch_record(OLD, run=OTHER_RUN)
        result = self.probe([{"sessionId": OLD, "state": "working", "cwd": "/x"}], self.snapshot(bound=False))
        self.assertEqual(result, {"live": [], "unknown": None})

    def test_unreadable_listing_is_unknown(self):
        self.launch_record(UNBOUND)
        result = self.probe(None, self.snapshot(bound=False), error="boom")
        self.assertTrue(result["unknown"])

    def test_unreadable_launch_record_is_unknown(self):
        self.records.mkdir(parents=True)
        (self.records / "claude-bg-n-1-abcd1234.json").write_text("{not json")
        result = self.probe([], self.snapshot(bound=False))
        self.assertIn("unreadable", result["unknown"])


if __name__ == "__main__":
    unittest.main()
