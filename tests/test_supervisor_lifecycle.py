import json
import os
import tempfile
import unittest
from pathlib import Path

from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import Supervisor, SupervisorLease


class _Monitor:
    def __init__(self):
        self.calls = []

    def resume(self, run_id, runner, poll_handles=False):
        self.calls.append(("resume", run_id, poll_handles))
        return {"run_id": run_id, "event_sequence": 1}

    def tick(self, run_id, runner):
        self.calls.append(("tick", run_id))
        # The first cycle is unresolved; the second is terminal.  This
        # models a provider that returns a pending/unknown observation before
        # delivering the final result.
        status = "blocked_unknown" if len(self.calls) < 3 else "complete"
        return {"run_id": run_id, "event_sequence": 2, "status": status}


class SupervisorLifecycleTests(unittest.TestCase):
    def test_stale_pid_is_recovered_and_only_one_supervisor_runs(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            lease = SupervisorLease(paths)
            lease_path = lease.path("run-1")
            lease_path.parent.mkdir(parents=True, exist_ok=True)
            lease_path.write_text(json.dumps({"pid": 999999, "run_id": "run-1", "heartbeat": 0}))
            result = Supervisor(paths, _Monitor(), object(), "run-1").recover_or_start()
            self.assertTrue(result["stale_pid_recovered"])
            self.assertEqual(result["active_supervisors"], 1)

    def test_parent_session_exit_does_not_delete_run_progress(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            monitor = _Monitor()
            supervisor = Supervisor(paths, monitor, object(), "run-1")
            snapshot = supervisor.run_once()
            self.assertEqual(snapshot["run_id"], "run-1")
            self.assertGreater(snapshot["event_sequence"], 0)
            self.assertEqual([item[0] for item in monitor.calls], ["resume", "tick"])

    def test_watch_does_not_exit_on_blocked_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            monitor = _Monitor()
            supervisor = Supervisor(paths, monitor, object(), "run-1")
            snapshot = supervisor.watch(interval=0, max_cycles=3)
            self.assertEqual(snapshot["status"], "complete")
            self.assertEqual([item[0] for item in monitor.calls],
                             ["resume", "tick", "resume", "tick"])


if __name__ == "__main__":
    unittest.main()
