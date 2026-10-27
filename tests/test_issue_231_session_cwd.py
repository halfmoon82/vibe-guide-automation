"""ISSUE-231: projectless takeover sessions must be caught and warned about.

A projectless Codex Desktop session starts in a host-generated directory
whose creation-time sandbox profile lacks the project root, so every read or
write of ``.vibe/`` raises its own approval prompt; re-filing the thread into
the project afterwards does not backfill the profile.  Two follow-ups:

- the handoff text must require the new session to be created inside the
  project (Codex: create_thread with the project target);
- ``supervisor-register`` warns -- never blocks -- when the session record's
  cwd sits outside the project root.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import session_cwd_warning


def _codex_record(directory, cwd):
    path = Path(directory) / "rollout.jsonl"
    path.write_text(
        json.dumps({"type": "session_meta", "payload": {"cwd": cwd}}) + "\n"
        + json.dumps({"type": "event_msg", "payload": {"type": "task_started"}}) + "\n",
        encoding="utf-8",
    )
    return str(path)


def _claude_record(directory, cwd):
    path = Path(directory) / "session.jsonl"
    path.write_text(
        json.dumps({"type": "queue-operation", "sessionId": "s1"}) + "\n"
        + json.dumps({"type": "user", "cwd": cwd}) + "\n",
        encoding="utf-8",
    )
    return str(path)


class SessionCwdWarningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)
        self.outside = Path(self.tmp.name) / "Documents" / "Codex" / "auto"
        self.outside.mkdir(parents=True)

    def test_codex_record_outside_root_warns(self):
        record = _codex_record(self.tmp.name, str(self.outside))
        warning = session_cwd_warning(self.paths, record)
        self.assertIsNotNone(warning)
        self.assertIn(str(self.outside.resolve()), warning)
        self.assertIn(str(self.root.resolve()), warning)
        self.assertIn("#231", warning)

    def test_claude_record_outside_root_warns(self):
        record = _claude_record(self.tmp.name, str(self.outside))
        self.assertIsNotNone(session_cwd_warning(self.paths, record))

    def test_record_inside_root_is_silent(self):
        inside = self.root / "sub" / "dir"
        inside.mkdir(parents=True)
        record = _codex_record(self.tmp.name, str(inside))
        self.assertIsNone(session_cwd_warning(self.paths, record))

    def test_record_at_root_is_silent(self):
        record = _codex_record(self.tmp.name, str(self.root))
        self.assertIsNone(session_cwd_warning(self.paths, record))

    def test_missing_record_is_silent(self):
        self.assertIsNone(
            session_cwd_warning(self.paths, str(Path(self.tmp.name) / "none.jsonl"))
        )
        self.assertIsNone(session_cwd_warning(self.paths, None))

    def test_record_without_cwd_is_silent(self):
        path = Path(self.tmp.name) / "plain.json"
        path.write_text(json.dumps({"token_count": 1234}), encoding="utf-8")
        self.assertIsNone(session_cwd_warning(self.paths, str(path)))

    def test_relative_cwd_is_silent(self):
        record = _codex_record(self.tmp.name, "relative/dir")
        self.assertIsNone(session_cwd_warning(self.paths, record))

    def _linked_worktree_root(self):
        """Project root as a linked worktree of ``main`` (run-63e7cfc2 layout)."""
        main = Path(self.tmp.name) / "main"
        (main / ".git" / "worktrees" / "project").mkdir(parents=True)
        (self.root / ".git").write_text(
            "gitdir: {}\n".format(main / ".git" / "worktrees" / "project"), encoding="utf-8")
        return main

    def test_session_in_main_checkout_of_same_repo_is_silent(self):
        # 2026-10-27 14:26: the successor ran from the main checkout while the
        # run lived in the host worktree; it wrote .vibe fine, so the
        # "projectless" warning was a false alarm.
        main = self._linked_worktree_root()
        record = _codex_record(self.tmp.name, str(main / "sub"))
        (main / "sub").mkdir()
        self.assertIsNone(session_cwd_warning(self.paths, record))

    def test_session_in_sibling_worktree_of_same_repo_is_silent(self):
        main = self._linked_worktree_root()
        sibling = Path(self.tmp.name) / "sibling"
        sibling.mkdir()
        (main / ".git" / "worktrees" / "sibling").mkdir()
        (sibling / ".git").write_text(
            "gitdir: {}\n".format(main / ".git" / "worktrees" / "sibling"), encoding="utf-8")
        self.assertIsNone(session_cwd_warning(self.paths, _codex_record(self.tmp.name, str(sibling))))

    def test_session_in_another_repo_still_warns(self):
        self._linked_worktree_root()
        other = Path(self.tmp.name) / "other-repo"
        (other / ".git").mkdir(parents=True)
        self.assertIsNotNone(session_cwd_warning(self.paths, _codex_record(self.tmp.name, str(other))))

    def test_register_cli_warns_but_still_succeeds(self):
        record = _codex_record(self.tmp.name, str(self.outside))
        result = run_cli([
            "supervisor-register", "--run-id", "run-1",
            "--provider", "codex", "--session-id", "A", "--host", "mac",
            "--session-record", record, "--json",
        ], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["status"], "ok")
        self.assertIn("session_cwd_warning", result.payload)
        self.assertIn("#231", result.payload["session_cwd_warning"])

    def test_register_cli_inside_root_has_no_warning(self):
        record = _codex_record(self.tmp.name, str(self.root))
        result = run_cli([
            "supervisor-register", "--run-id", "run-1",
            "--provider", "codex", "--session-id", "A", "--host", "mac",
            "--session-record", record, "--json",
        ], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertNotIn("session_cwd_warning", result.payload)


class HandoffTextTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)

    def _handoff_text(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from vibe_guide.state import run_dir
        from vibe_guide.supervisor import supervisor_handoff

        directory = run_dir(self.paths, "run-1", create=True)
        (directory / "state.json").write_text('{"status":"running"}')
        snapshot = SimpleNamespace(
            plan_id="plan-1", run_id="run-1", status="running", nodes={},
            authorization_digest="a" * 64, node_contract_digest="b" * 64,
        )
        with patch("vibe_guide.supervisor.load_snapshot", return_value=snapshot):
            _, text = supervisor_handoff(self.paths, "run-1")
        return text

    def test_handoff_requires_in_project_session(self):
        text = self._handoff_text()
        self.assertIn("禁止 projectless", text)
        self.assertIn("create_thread 的 project 目标", text)
        self.assertIn("#231", text)


if __name__ == "__main__":
    unittest.main()
