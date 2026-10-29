import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide import daemon as dm
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import run_dir
from vibe_guide.supervisor import (
    SupervisorLease,
    SupervisorLeaseConflict,
    read_lease_epoch,
    register_supervisor_address,
)

RUN = "run-" + "b" * 32
PM = {"provider": "claude-code", "session_id": "pm-session-1", "host": "macmini"}


class LeaseSupervisor:
    """A supervisor whose run_once makes the same epoch check the real one does."""

    def __init__(self, lease):
        self.lease = lease
        self.calls = 0

    def recover_or_start(self):
        return {"active_supervisors": 1 if self.lease.acquire(RUN) else 0}

    def run_once(self):
        self.calls += 1
        self.lease.ensure_current(RUN)
        return SimpleNamespace(status="running", nodes={}, tasks={}, authorization={})


class DaemonSurvivesRegisterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name), vibe_home=Path(self.tmp.name) / "home")
        (Path(self.tmp.name) / "home").mkdir()
        run_dir(self.paths, RUN, create=True)
        # The daemon is the parent process of the test runner's register call: a
        # live pid that differs from os.getpid(), as in `daemon start` then `register`.
        self.daemon_pid = os.getppid()
        self.lease = SupervisorLease(self.paths, pid=self.daemon_pid)

    def daemon(self, supervisor):
        return dm.Daemon(self.paths, RUN, supervisor, dm.DaemonConfig(interval=0, heal_interval=10 ** 9),
                         sleep=lambda s: None, notifier=lambda *a, **k: {"status": "sent"},
                         assess=lambda: {"status": "ok", "nodes": {}})

    def record_daemon(self):
        record = {"run_id": RUN, "pid": self.daemon_pid, "state": "running", "heartbeat": 9e12,
                  "stale_seconds": 120.0}
        (run_dir(self.paths, RUN) / dm.DAEMON_FILE).write_text(json.dumps(record), encoding="utf-8")

    def lease_record(self):
        return json.loads((run_dir(self.paths, RUN) / "supervisor.lease").read_text(encoding="utf-8"))

    def test_register_after_daemon_start_keeps_the_daemon_as_holder(self):
        supervisor = LeaseSupervisor(self.lease)
        daemon = self.daemon(supervisor)
        daemon.supervisor.recover_or_start()
        self.record_daemon()
        register_supervisor_address(self.paths, RUN, PM)
        self.assertEqual(read_lease_epoch(self.paths, RUN), 1)
        self.assertEqual(self.lease_record()["pid"], self.daemon_pid)
        self.assertEqual(self.lease_record()["owner"]["session_id"], "pm-session-1")

    def test_register_without_a_live_daemon_takes_the_lease_pid_as_before(self):
        self.lease.acquire(RUN)
        register_supervisor_address(self.paths, RUN, PM)
        self.assertEqual(self.lease_record()["pid"], os.getpid())

    def test_daemon_adopts_the_new_epoch_and_keeps_ticking(self):
        supervisor = LeaseSupervisor(self.lease)
        daemon = self.daemon(supervisor)
        supervisor.recover_or_start()
        with patch.object(os, "getpid", return_value=self.daemon_pid):
            daemon.tick()
        register_supervisor_address(self.paths, RUN, PM)
        with patch.object(os, "getpid", return_value=self.daemon_pid):
            daemon.tick()
            daemon.tick()
        self.assertEqual(self.lease.lease_epoch, 1)
        self.assertEqual(daemon._run_status, "running")
        self.assertIsNone(daemon._last_error)
        self.assertEqual(dm.daemon_status(self.paths, RUN)["lease"]["state"], "held")

    def test_daemon_still_stops_when_another_live_process_took_the_lease(self):
        supervisor = LeaseSupervisor(self.lease)
        daemon = self.daemon(supervisor)
        supervisor.recover_or_start()
        # No daemon record: register writes its own pid, which is alive and not ours.
        register_supervisor_address(self.paths, RUN, PM)
        with self.assertRaises(SupervisorLeaseConflict):
            daemon.tick()

    def test_daemon_never_adopts_a_lower_epoch(self):
        supervisor = LeaseSupervisor(self.lease)
        supervisor.recover_or_start()
        self.lease.lease_epoch = 5
        with self.assertRaises(SupervisorLeaseConflict):
            self.lease.readopt(RUN)


if __name__ == "__main__":
    unittest.main()
