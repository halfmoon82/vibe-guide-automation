"""F4: a superseded supervisor session must not write through plain resume."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import run_dir
from vibe_guide.supervisor import (
    SupervisorLeaseConflict,
    register_supervisor_address,
)

ENV = (
    "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_HOST_SESSION_ID",
    "CODEBUDDY_SESSION_ID", "CODEX_THREAD_ID",
)


def _addr(name):
    return {"provider": "claude", "session_id": name, "host": "local"}


class CallerGuardTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.paths = ProjectPaths(Path(self._tmp.name))
        self.monitor = Monitor.__new__(Monitor)
        self.monitor.paths = self.paths
        self.monitor.lease_guard = None

    def _register(self, *names):
        for name in names:
            register_supervisor_address(self.paths, "run-1", _addr(name))

    def _env(self, value):
        env = {key: "" for key in ENV}
        if value is not None:
            env["CLAUDE_CODE_SESSION_ID"] = value
        return patch.dict(os.environ, env, clear=False)

    def _snap(self):
        class Snap:
            run_id = "run-1"
        return Snap()

    def test_a_superseded_session_rejected(self):
        self._register("S1", "S2")
        with self._env("S1"), patch("vibe_guide.monitor.save_snapshot") as saved, \
                patch("vibe_guide.monitor.save_task_binding") as bound:
            with self.assertRaises(SupervisorLeaseConflict) as ctx:
                self.monitor._save_snapshot(self._snap())
            self.assertIn("S1", str(ctx.exception))
            self.assertIn("S2", str(ctx.exception))
            binding = type("B", (), {"run_id": "run-1", "issue_id": "i", "role": "r"})()
            with self.assertRaises(SupervisorLeaseConflict):
                self.monitor._save_task_binding(binding)
            saved.assert_not_called()
            bound.assert_not_called()
            # append_event path shares _guard_lease(run_id); covered by the
            # _save_snapshot check above (constructing a full event write is heavy).
            with self.assertRaises(SupervisorLeaseConflict):
                self.monitor._guard_lease("run-1")

    def test_b_current_session_writes(self):
        self._register("S1", "S2")
        with self._env("S2"), patch("vibe_guide.monitor.save_snapshot") as saved:
            self.monitor._save_snapshot(self._snap())
            saved.assert_called_once()

    def test_c_unknown_identity_writes(self):
        self._register("S1", "S2")
        with self._env(None), patch("vibe_guide.monitor.save_snapshot") as saved:
            self.monitor._save_snapshot(self._snap())
            saved.assert_called_once()

    def test_d_unregistered_session_writes(self):
        self._register("S1", "S2")
        with self._env("OTHER"), patch("vibe_guide.monitor.save_snapshot") as saved:
            self.monitor._save_snapshot(self._snap())
            saved.assert_called_once()

    def test_e_no_registry_writes(self):
        with self._env("S1"), patch("vibe_guide.monitor.save_snapshot") as saved:
            self.monitor._save_snapshot(self._snap())
            saved.assert_called_once()

    def test_f_legacy_history_containing_current_writes(self):
        path = run_dir(self.paths, "run-1", create=True) / "supervisor-registry.json"
        entry = {"provider": "claude", "session_id": "S2", "host": "local"}
        path.write_text(json.dumps({"current": entry, "history": [entry]}))
        with self._env("S2"), patch("vibe_guide.monitor.save_snapshot") as saved:
            self.monitor._save_snapshot(self._snap())
            saved.assert_called_once()

    def test_g_recovered_from_lease_entry_rejected(self):
        path = run_dir(self.paths, "run-1", create=True) / "supervisor-registry.json"
        path.write_text(json.dumps({
            "current": {"provider": "claude", "session_id": "C", "host": "local", "lease_epoch": 3},
            "history": [{"provider": "claude", "session_id": "B", "host": "local",
                         "lease_epoch": 2, "recovered_from_lease": True}],
        }))
        with self._env("B"), patch("vibe_guide.monitor.save_snapshot") as saved:
            with self.assertRaises(SupervisorLeaseConflict):
                self.monitor._save_snapshot(self._snap())
            saved.assert_not_called()

    def test_h_watch_guard_path_skips_identity_check(self):
        self._register("S1", "S2")
        self.monitor.lease_guard = lambda: None
        with self._env("S1"), patch("vibe_guide.monitor.save_snapshot") as saved:
            self.monitor._save_snapshot(self._snap())
            saved.assert_called_once()

    def _env_multi(self, **values):
        env = {key: "" for key in ENV}
        env.update(values)
        return patch.dict(os.environ, env, clear=False)

    def test_i_host_session_id_in_history_rejected(self):
        self._register("local-S1", "S2")
        with self._env_multi(CLAUDE_CODE_SESSION_ID="uuid-X",
                             CLAUDE_CODE_HOST_SESSION_ID="local-S1"), \
                patch("vibe_guide.monitor.save_snapshot") as saved:
            with self.assertRaises(SupervisorLeaseConflict):
                self.monitor._save_snapshot(self._snap())
            saved.assert_not_called()

    def test_j_one_current_one_history_allowed(self):
        self._register("S1", "local-S2")
        with self._env_multi(CLAUDE_CODE_SESSION_ID="S1",
                             CLAUDE_CODE_HOST_SESSION_ID="local-S2"), \
                patch("vibe_guide.monitor.save_snapshot") as saved:
            self.monitor._save_snapshot(self._snap())
            saved.assert_called_once()


if __name__ == "__main__":
    unittest.main()
