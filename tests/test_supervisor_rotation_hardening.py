"""Supervisor rotation hardening (2026-10-26 run-63e7cfc2 field report).

Three defects turned one authorized run into repeated automation stops:

A. The card's rotation hard cap was not the rotation rule: an unset
   ``supervisor_single_shift`` kept the hidden "grew 80k since takeover" rule,
   so an 800k cap rotated at 291k.  Single shift is now the default (PRD
   #181 N3, product manager approval 2026-10-26); growth rotation stays as an
   explicit ``false`` opt-in.
B. A successor registered from a full-history fork (Codex ``fork_thread``),
   carrying ~297k tokens into the new shift.  Registration now refuses forks
   and successors that start above the soft cap.
C. A successor without write access to ``.vibe`` saw only "session gate
   persistence failed" and retried.  The block now names the unwritable path
   and says not to retry, and a project root inside a linked git worktree
   (where no fresh host session can write) is flagged at registration.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.config import load_project_config, load_supervisor_single_shift
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import (
    DEFAULT_ROTATE_SOFT_CAP,
    SupervisorLeaseConflict,
    project_root_worktree_warning,
    register_supervisor_address,
    supervisor_handoff,
    supervisor_preflight,
)


def _record(directory, name, tokens, *, forked_from=None, cwd=None):
    meta = {"id": name}
    if forked_from:
        meta["forked_from_id"] = forked_from
    if cwd:
        meta["cwd"] = cwd
    path = Path(directory) / name
    path.write_text(
        json.dumps({"type": "session_meta", "payload": meta}) + "\n"
        + json.dumps({"context_tokens": tokens}) + "\n",
        encoding="utf-8",
    )
    return path


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)

    def register(self, session_id, record=None):
        return register_supervisor_address(
            self.paths, "run-1",
            {"provider": "codex-app-visible", "session_id": session_id, "host": "local"},
            session_record=record,
        )


class SingleShiftDefaultTests(_Case):
    """A: with no config the authorized hard cap is the only rotation rule."""

    def preflight(self, tokens, baseline=20000):
        record = _record(self.tmp.name, "shift.jsonl", baseline)
        self.register("shift", record)
        record.write_text(json.dumps({"context_tokens": tokens}), encoding="utf-8")
        with patch("vibe_guide.supervisor._mailbox_state", return_value={"state": "work"}):
            return supervisor_preflight(self.paths, "run-1", record)

    def test_no_config_does_not_rotate_on_growth(self):
        # Field shape (run-63e7cfc2 grew 96,841 per shift), under the default cap.
        result = self.preflight(199999, baseline=103158)
        self.assertNotEqual(result["state"], "rotate", result)
        self.assertTrue(result["single_shift"])

    def test_no_config_still_rotates_at_hard_cap(self):
        result = self.preflight(200000)
        self.assertEqual(result["state"], "rotate")
        self.assertEqual(result["reason"], "context over hard cap")

    def test_explicit_false_keeps_growth_rotation(self):
        (self.root / ".vibe").mkdir(exist_ok=True)
        (self.root / ".vibe" / "config.json").write_text(
            json.dumps({"supervisor_single_shift": False}), encoding="utf-8")
        result = self.preflight(130000)
        self.assertEqual(result["reason"], "context grew past threshold since takeover")

    def test_config_loaders_default_to_single_shift(self):
        self.assertEqual(load_supervisor_single_shift(self.root), (True, None))
        self.assertTrue(load_project_config(self.root).supervisor_single_shift)
        (self.root / ".vibe").mkdir(exist_ok=True)
        (self.root / ".vibe" / "config.json").write_text("{}", encoding="utf-8")
        self.assertTrue(load_project_config(self.root).supervisor_single_shift)

    def test_register_text_names_the_effective_rule(self):
        record = _record(self.tmp.name, "shift.jsonl", 30000)
        result = run_cli([
            "supervisor-register", "--run-id", "run-1", "--provider", "codex",
            "--session-id", "A", "--host", "mac", "--session-record", str(record),
        ], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertNotIn("增长阈值辅助判断", result.text)
        self.assertIn("只在达到硬上限 200,000 时换班", result.text)


class ForkedSuccessorTests(_Case):
    """B: a successor must be a fresh session, not a full-history fork."""

    def setUp(self):
        super().setUp()
        self.register("first", _record(self.tmp.name, "first.jsonl", 190000))

    def test_forked_successor_is_refused(self):
        forked = _record(self.tmp.name, "fork.jsonl", 40000, forked_from="first")
        with self.assertRaises(SupervisorLeaseConflict) as caught:
            self.register("fork", forked)
        self.assertIn("fork", str(caught.exception))
        registry = json.loads(
            (self.paths.vibe_dir / "runs" / "run-1" / "supervisor-registry.json")
            .read_text(encoding="utf-8"))
        self.assertEqual(registry["current"]["session_id"], "first")

    def test_successor_starting_above_soft_cap_is_refused(self):
        heavy = _record(self.tmp.name, "heavy.jsonl", DEFAULT_ROTATE_SOFT_CAP)
        with self.assertRaises(SupervisorLeaseConflict) as caught:
            self.register("heavy", heavy)
        self.assertIn("{:,}".format(DEFAULT_ROTATE_SOFT_CAP), str(caught.exception))

    def test_fresh_successor_registers(self):
        fresh = _record(self.tmp.name, "fresh.jsonl", 35000)
        entry = self.register("fresh", fresh)
        self.assertEqual(entry["baseline_context"], 35000)

    def test_first_supervisor_may_start_heavy(self):
        # The session that published and authorized the run becomes its first
        # supervisor; it is legitimately large and is not a successor.
        (Path(self.tmp.name) / "other").mkdir()
        other = ProjectPaths(Path(self.tmp.name) / "other")
        entry = register_supervisor_address(
            other, "run-1",
            {"provider": "codex", "session_id": "big", "host": "local"},
            session_record=_record(self.tmp.name, "big.jsonl", 300000, forked_from="x"),
        )
        self.assertEqual(entry["baseline_context"], 300000)

    def test_same_shift_reregistering_heavy_is_allowed(self):
        record = _record(self.tmp.name, "first.jsonl", 250000)
        entry = self.register("first", record)
        self.assertEqual(entry["session_id"], "first")

    def test_cli_reports_refusal_as_blocked(self):
        forked = _record(self.tmp.name, "fork.jsonl", 40000, forked_from="first")
        result = run_cli([
            "supervisor-register", "--run-id", "run-1", "--provider", "codex",
            "--session-id", "fork", "--host", "mac", "--session-record", str(forked),
            "--json",
        ], self.root)
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "blocked_invalid")
        self.assertIn("fork", result.payload["reason"])


class WriteAccessTests(_Case):
    """C: unwritable .vibe and host-worktree project roots are named, not retried."""

    def test_gate_persistence_failure_names_path_and_says_stop(self):
        (self.root / ".vibe").mkdir()
        (self.root / ".vibe" / "state.json").write_text(
            json.dumps({"workflow_version": 4}), encoding="utf-8")
        with patch("vibe_guide.diagnostics.open", side_effect=PermissionError(1, "Operation not permitted"), create=True):
            result = run_cli(["supervisor-handoff", "--run-id", "run-1", "--json"], self.root)
        self.assertEqual(result.payload["status"], "session_gate_blocked")
        self.assertIn(str(self.paths.vibe_dir), result.payload["reason"])
        self.assertIn("不要重试", result.text)

    def _linked_worktree(self):
        main = Path(self.tmp.name) / "main"
        (main / ".git" / "worktrees" / "project").mkdir(parents=True)
        (self.root / ".git").write_text(
            "gitdir: {}\n".format(main / ".git" / "worktrees" / "project"), encoding="utf-8")
        return main

    def test_linked_worktree_root_is_flagged(self):
        main = self._linked_worktree()
        warning = project_root_worktree_warning(self.paths)
        self.assertIsNotNone(warning)
        self.assertIn(str(main.resolve()), warning)

    def test_main_checkout_is_silent(self):
        (self.root / ".git").mkdir()
        self.assertIsNone(project_root_worktree_warning(self.paths))
        plain = ProjectPaths(Path(self.tmp.name))
        self.assertIsNone(project_root_worktree_warning(plain))

    def test_register_surfaces_worktree_warning(self):
        self._linked_worktree()
        result = run_cli([
            "supervisor-register", "--run-id", "run-1", "--provider", "codex",
            "--session-id", "A", "--host", "mac", "--json",
        ], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertIn("project_root_worktree_warning", result.payload)

    def test_handoff_text_forbids_fork_and_retry(self):
        with patch("vibe_guide.supervisor.load_snapshot") as snapshot:
            snapshot.return_value.plan_id = "plan-1"
            snapshot.return_value.status = "running"
            snapshot.return_value.nodes = {}
            snapshot.return_value.authorization_digest = "a" * 64
            snapshot.return_value.node_contract_digest = "b" * 64
            _, text = supervisor_handoff(self.paths, "run-1")
        self.assertIn("fork_thread", text)
        self.assertIn("不要重试", text)


if __name__ == "__main__":
    unittest.main()
