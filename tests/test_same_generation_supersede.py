"""A leftover same-generation create no longer needs a person to move a file (#344 #363)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events
from vibe_guide.task_registry import TaskBinding, save_task_binding

RUN = "run-1"
NODE = "n1"


class SameGenerationSupersedeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text("{}")
        gate = patch("vibe_guide.adapters.task_provider.require_entry")
        gate.start()
        self.addCleanup(gate.stop)
        self.store = ProviderActionStore(self.paths)
        self.old = self.emit({"prompt": "old", "model": "m1", "issue_id": NODE})

    def emit(self, request, generation=1):
        return self.store.request(
            operation="create", provider="claude-code-visible", run_id=RUN,
            issue_id=NODE, role="developer", generation=generation,
            native_tool="ccd_session__spawn_task", request=request)

    def binding(self, task_id, status):
        return TaskBinding(
            provider="claude-code-visible", mode="visible", issue_id=NODE, role="developer",
            task_id=task_id, host="local", worktree=".worktrees/n1", branch="node/n1",
            run_id=RUN, generation=1, status=status)

    def events(self):
        return [e for e in load_events(self.paths, RUN) if e["event"] == "create_request_superseded"]

    def new_request(self):
        return {"prompt": "new", "model": "m2", "issue_id": NODE}

    def test_old_task_stopped_is_superseded_with_an_event(self):
        self.store.complete(self.old["action_id"], {"binding": {"task_id": "t-old", "host": "local"}})
        save_task_binding(self.paths, self.binding("t-old", "stopped"))
        fresh = self.emit(self.new_request())
        self.assertEqual(fresh["request"]["model"], "m2")
        self.assertEqual(fresh["action_id"], self.old["action_id"])
        self.assertIsNone(self.store.result(fresh["action_id"]))
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["superseded_creates"], [self.old["action_id"]])
        self.assertEqual(events[0]["data"]["reason_code"], "old_task_terminated")

    def test_never_answered_request_is_superseded_with_an_event(self):
        fresh = self.emit(self.new_request())
        self.assertEqual(fresh["request"]["prompt"], "new")
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["reason_code"], "old_request_never_produced_a_task")

    def test_live_old_task_is_refused_and_names_the_missing_evidence(self):
        self.store.complete(self.old["action_id"], {"binding": {"task_id": "t-old", "host": "local"}})
        save_task_binding(self.paths, self.binding("t-old", "running"))
        path = self.store.root / "requests" / (self.old["action_id"] + ".json")
        before = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "identity drift.*termination evidence"):
            self.emit(self.new_request())
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.events(), [])
        self.assertIsNotNone(self.store.result(self.old["action_id"]))

    def test_answered_but_unbound_old_create_is_refused(self):
        # The result exists but no binding yet: the old task may be alive.
        self.store.complete(self.old["action_id"], {"binding": {"task_id": "t-old", "host": "local"}})
        with self.assertRaisesRegex(ValueError, "termination evidence"):
            self.emit(self.new_request())
        self.assertEqual(self.events(), [])

    def test_redispatch_leaves_exactly_one_live_binding(self):
        self.store.complete(self.old["action_id"], {"binding": {"task_id": "t-old", "host": "local"}})
        save_task_binding(self.paths, self.binding("t-old", "running"))
        # A second live binding for the node is refused while the old one lives.
        successor = self.binding("t-new", "running")
        successor.generation = 2
        successor.successor_of = "t-old"
        with self.assertRaises(ValueError):
            save_task_binding(self.paths, successor)
        save_task_binding(self.paths, self.binding("t-old", "stopped"))
        self.emit(self.new_request())
        save_task_binding(self.paths, successor)
        rows = json.loads((self.paths.vibe / "runs" / RUN / "tasks.json").read_text())["bindings"]
        live = [r for r in rows if r["issue_id"] == NODE and r["role"] == "developer"
                and r["status"] not in ("stopped", "failed", "archived", "blocked_design")]
        self.assertEqual([r["task_id"] for r in live], ["t-new"])

    def test_non_create_drift_is_still_refused(self):
        resume = self.store.request(
            operation="resume", provider="claude-code-visible", run_id=RUN, issue_id=NODE,
            role="developer", generation=1, native_tool="t",
            request={"threadId": "t1", "prompt": "a"})
        with self.assertRaisesRegex(ValueError, "identity drift"):
            self.store.request(
                operation="resume", provider="claude-code-visible", run_id=RUN, issue_id=NODE,
                role="developer", generation=1, native_tool="t",
                request={"threadId": "t1", "prompt": "b"})
        self.assertEqual(resume["request"]["prompt"], "a")


if __name__ == "__main__":
    unittest.main()
