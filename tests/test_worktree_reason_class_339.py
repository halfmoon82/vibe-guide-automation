"""#339: visible/background worktree preflight has one machine-readable contract."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from vibe_guide.adapters.claude_bg import (
    WORKTREE_BLOCKED_EVENT,
    WORKTREE_CREATED_EVENT,
    WORKTREE_MISMATCH,
    WORKTREE_MISSING,
    ClaudeBgWorktreeBlocked,
)
from vibe_guide.adapters.task_provider import ProviderPending
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.supervisor import Supervisor


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


class VisibleWorktreePreflightTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve() / "project"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "dev@example.com")
        git(self.root, "config", "user.name", "Dev")
        (self.root / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.root, "add", "base.txt")
        git(self.root, "commit", "-qm", "base")
        self.base = git(self.root, "rev-parse", "HEAD").stdout.strip()
        self.worktree = self.root / ".worktrees" / "node-a"
        self.branch = "node/node-a"
        self.launchers = []
        self.runner = ProviderActionRunner(
            ProjectPaths(self.root),
            "codex-app-visible",
            "codex-app-visible",
            launchers=self.launchers,
        )

    def contract(self):
        return {
            "run_id": "run-339",
            "node_id": "node-a",
            "role": "developer",
            "generation": 1,
            "binding_contract_version": "3.9",
            "binding_probe": True,
            "project_id": "project-a",
            "worktree": str(self.worktree),
            "managed_root": str(self.root),
            "branch": self.branch,
            "base_sha": self.base,
            "files": ["base.txt"],
        }

    def test_missing_safe_worktree_is_created_before_visible_create(self):
        with mock.patch.object(
            self.runner, "_require_result", side_effect=ProviderPending("pending")
        ) as create:
            with self.assertRaises(ProviderPending):
                self.runner.task_binding(
                    self.contract(), self.worktree, "run-339", "start_pending"
                )
        create.assert_called_once()
        self.assertTrue((self.worktree / ".git").exists())
        self.assertEqual(
            git(self.worktree, "rev-parse", "HEAD").stdout.strip(), self.base
        )
        self.assertEqual(self.launchers[0][0], "node-a")
        report = self.launchers[0][1].worktree_preflight
        self.assertEqual(report["event"], WORKTREE_CREATED_EVENT)
        self.assertEqual(report["worktree_base"], self.base)

    def test_unsafe_missing_worktree_has_machine_reason_and_exact_command(self):
        git(self.root, "branch", self.branch)
        with mock.patch.object(self.runner, "_require_result") as create:
            with self.assertRaises(ClaudeBgWorktreeBlocked) as caught:
                self.runner.task_binding(
                    self.contract(), self.worktree, "run-339", "start_pending"
                )
        create.assert_not_called()
        self.assertEqual(caught.exception.reason_code, WORKTREE_MISSING)
        self.assertEqual(
            caught.exception.command,
            "git worktree add %s %s" % (self.worktree, self.branch),
        )
        report = self.launchers[0][1].worktree_preflight
        self.assertEqual(report["event"], WORKTREE_BLOCKED_EVENT)
        self.assertEqual(report["reason_code"], WORKTREE_MISSING)

    def test_nested_non_git_project_keeps_provider_owned_create_flow(self):
        legacy = self.root / "legacy-project"
        legacy.mkdir()
        worktree = legacy / ".worktrees" / "node-a"
        launchers = []
        runner = ProviderActionRunner(
            ProjectPaths(legacy),
            "codex-app-visible",
            "codex-app-visible",
            launchers=launchers,
        )
        contract = dict(
            self.contract(),
            worktree=str(worktree),
            managed_root=str(legacy),
        )
        with mock.patch.object(
            runner, "_require_result", side_effect=ProviderPending("pending")
        ) as create:
            with self.assertRaises(ProviderPending):
                runner.task_binding(contract, worktree, "run-339", "start_pending")
        create.assert_called_once()
        self.assertEqual(launchers, [])
        self.assertFalse(worktree.exists())

    def test_plain_directory_is_not_a_visible_dispatch_target(self):
        self.worktree.mkdir(parents=True)
        with mock.patch.object(self.runner, "_require_result") as create:
            with self.assertRaises(ClaudeBgWorktreeBlocked) as caught:
                self.runner.task_binding(
                    self.contract(), self.worktree, "run-339", "start_pending"
                )
        create.assert_not_called()
        self.assertEqual(caught.exception.reason_code, WORKTREE_MISSING)

    def test_existing_dirty_worktree_is_not_a_visible_dispatch_target(self):
        git(self.root, "worktree", "add", str(self.worktree), "-b", self.branch)
        (self.worktree / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
        with mock.patch.object(self.runner, "_require_result") as create:
            with self.assertRaises(ClaudeBgWorktreeBlocked) as caught:
                self.runner.task_binding(
                    self.contract(), self.worktree, "run-339", "start_pending"
                )
        create.assert_not_called()
        self.assertEqual(caught.exception.reason_code, WORKTREE_MISMATCH)

    def test_existing_worktree_with_drifted_base_is_not_a_visible_dispatch_target(self):
        git(self.root, "worktree", "add", str(self.worktree), "-b", self.branch)
        (self.worktree / "base.txt").write_text("new commit\n", encoding="utf-8")
        git(self.worktree, "add", "base.txt")
        git(self.worktree, "commit", "-qm", "advance beyond contract base")
        with mock.patch.object(self.runner, "_require_result") as create:
            with self.assertRaises(ClaudeBgWorktreeBlocked) as caught:
                self.runner.task_binding(
                    self.contract(), self.worktree, "run-339", "start_pending"
                )
        create.assert_not_called()
        self.assertEqual(caught.exception.reason_code, WORKTREE_MISMATCH)


class _Monitor:
    def __init__(self):
        self.beats = 0

    def resume(self, run_id, runner, poll_handles=False):
        return None

    def tick(self, run_id, runner):
        self.beats += 1
        return {
            "run_id": run_id,
            "status": "complete" if self.beats >= 2 else "running",
        }


class SupervisorWorktreeRecorderTests(unittest.TestCase):
    def test_watch_invokes_worktree_recorder_on_every_beat(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshots = []
            supervisor = Supervisor(
                ProjectPaths(Path(directory)),
                _Monitor(),
                object(),
                "run-339",
                worktree_recorder=snapshots.append,
            )
            supervisor.watch(interval=0, max_cycles=2)
            self.assertEqual(len(snapshots), 2)
            self.assertEqual([s["run_id"] for s in snapshots], ["run-339"] * 2)


if __name__ == "__main__":
    unittest.main()
