import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.models import DAGNode
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import RunSnapshot


class DispatchRecoveryTests(unittest.TestCase):
    def test_dormant_planned_downstream_does_not_hold_scope(self):
        monitor = Monitor.__new__(Monitor)
        monitor.nodes = {
            "issue-151": DAGNode("issue-151", "current", [], [], None, {"files": ["vibe_guide/protocols/prd-guide.md"]}, "running"),
            "issue-153": DAGNode("issue-153", "downstream", ["issue-151"], [], None, {"files": ["vibe_guide/protocols/prd-guide.md"]}, "planned"),
        }
        snapshot = RunSnapshot("run-1", "plan-1", 1, "running", {
            "issue-151": {"status": "running", "active_task": {"role": "developer"}, "worktree": ".worktrees/issue-151"},
            "issue-153": {"status": "planned", "active_task": None, "start_intent": None, "worktree": ".worktrees/issue-153"},
        }, {})
        self.assertEqual(monitor._files_held_by_other_active_nodes(snapshot, "issue-151"), [])

    def test_unknown_writer_without_lease_still_holds_scope(self):
        monitor = Monitor.__new__(Monitor)
        monitor.nodes = {
            "issue-151": DAGNode("issue-151", "current", [], [], None, {"files": ["vibe_guide/protocols/prd-guide.md"]}, "running"),
            "issue-153": DAGNode("issue-153", "unknown", [], [], None, {"files": ["vibe_guide/protocols/prd-guide.md"]}, "blocked_unknown"),
        }
        snapshot = RunSnapshot("run-1", "plan-1", 1, "blocked_unknown", {
            "issue-151": {"status": "running", "active_task": {"role": "developer"}, "worktree": ".worktrees/issue-151"},
            "issue-153": {"status": "blocked_unknown", "reason": "writer identity unknown", "worktree": ".worktrees/issue-153"},
        }, {})
        self.assertIn("vibe_guide/protocols/prd-guide.md", monitor._files_held_by_other_active_nodes(snapshot, "issue-151"))

    def test_planned_downstream_lease_read_error_holds_scope_fail_closed(self):
        with tempfile.TemporaryDirectory() as root:
            monitor = Monitor.__new__(Monitor)
            monitor.paths = ProjectPaths(Path(root))
            monitor.nodes = {
                "issue-151": DAGNode("issue-151", "current", [], [], None, {"files": ["shared.py"]}, "running"),
                "issue-153": DAGNode("issue-153", "downstream", ["issue-151"], [], None, {"files": ["shared.py"]}, "planned"),
            }
            snapshot = RunSnapshot("run-1", "plan-1", 1, "running", {
                "issue-151": {"status": "running", "active_task": {"role": "developer"}, "worktree": ".worktrees/issue-151"},
                "issue-153": {"status": "planned", "active_task": None, "start_intent": None, "worktree": ".worktrees/issue-153"},
            }, {})
            with patch("vibe_guide.monitor.read_writer_lease", side_effect=OSError("lease store unreadable")):
                held = monitor._files_held_by_other_active_nodes(snapshot, "issue-151")
            self.assertIn("shared.py", held)

    def test_planned_downstream_pending_provider_request_holds_scope(self):
        with tempfile.TemporaryDirectory() as root:
            monitor = Monitor.__new__(Monitor)
            monitor.paths = ProjectPaths(Path(root))
            monitor.nodes = {
                "issue-151": DAGNode("issue-151", "current", [], [], None, {"files": ["shared.py"]}, "running"),
                "issue-153": DAGNode("issue-153", "downstream", ["issue-151"], [], None, {"files": ["shared.py"]}, "planned"),
            }
            snapshot = RunSnapshot("run-1", "plan-1", 1, "running", {
                "issue-151": {"status": "running", "active_task": {"role": "developer"}, "worktree": ".worktrees/issue-151"},
                "issue-153": {"status": "planned", "active_task": None, "start_intent": None, "worktree": ".worktrees/issue-153"},
            }, {})
            store = ProviderActionStore(monitor.paths)
            request_dir = store.root / "requests"
            request_dir.mkdir(parents=True)
            store._atomic(request_dir / "action-pending.json", {
                "schema_version": 1, "action_id": "pending", "operation": "create",
                "provider": "codex", "run_id": "run-1", "issue_id": "issue-153",
                "role": "developer", "generation": 1, "request": {},
                "request_digest": "a" * 64,
            })
            held = monitor._files_held_by_other_active_nodes(snapshot, "issue-151")
            self.assertIn("shared.py", held)

    def test_planned_downstream_malformed_lease_record_holds_scope(self):
        with tempfile.TemporaryDirectory() as root:
            monitor = Monitor.__new__(Monitor)
            monitor.paths = ProjectPaths(Path(root))
            monitor.nodes = {
                "issue-151": DAGNode("issue-151", "current", [], [], None, {"files": ["shared.py"]}, "running"),
                "issue-153": DAGNode("issue-153", "downstream", ["issue-151"], [], None, {"files": ["shared.py"]}, "planned"),
            }
            snapshot = RunSnapshot("run-1", "plan-1", 1, "running", {
                "issue-151": {"status": "running", "active_task": {"role": "developer"}, "worktree": ".worktrees/issue-151"},
                "issue-153": {"status": "planned", "active_task": None, "start_intent": None, "worktree": ".worktrees/issue-153"},
            }, {})
            lease_dir = monitor.paths.vibe / "leases"
            lease_dir.mkdir(parents=True)
            (lease_dir / "corrupt.json").write_text("{", encoding="utf-8")
            held = monitor._files_held_by_other_active_nodes(snapshot, "issue-151")
            self.assertIn("shared.py", held)


if __name__ == "__main__":
    unittest.main()
