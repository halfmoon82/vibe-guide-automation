"""#379: workers never push, whatever the card says.

Recorded failure (run-3218a7e6, v5.2.4): under ``remote_git_actions: allow``
two of seven developer nodes pushed their node branches to the remote; the
ban text and launch flags were only added under ``deny``.  Only the
monitor-driven remote-delivery ring may push.  At closeout a node branch that
reached the remote is a deviation listed in ``vibe status --json`` and in the
delivery report; an unreadable remote is reported as "not checked", never as
"no deviation".
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide.adapters.claude_bg import ClaudeBgConfig, ClaudeBgLauncher, CommandResult
from vibe_guide.adapters.task_provider import BackgroundTaskRouting
from vibe_guide.authorization import (
    REMOTE_GIT_BAN_TEXT,
    WORKER_PUSH_BAN_TEXT,
    WORKER_PUSH_DISALLOWED_TOOLS,
)
from vibe_guide.cli import _snapshot_result
from vibe_guide.paths import ProjectPaths
from vibe_guide.remote_delivery import (
    SubprocessRemoteDeliveryExecutor,
    _write_delivery_report,
    remote_node_branch_deviations,
)
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.state import load_snapshot

from tests import test_claude_bg_delivery_gate as _gate

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True, env=_GIT_ENV).stdout.strip()


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, timeout, env=None):
        self.calls.append((list(argv), env))
        if "--bg" in argv and "--resume" not in argv:
            return CommandResult(0, "backgrounded · abc12345\n", "")
        return CommandResult(0, "", "")


def _bg_call(root, deny, role):
    contract = Path(root) / "c.md"
    contract.write_text("do the work", encoding="utf-8")
    recorder = Recorder()
    routing = BackgroundTaskRouting(host="local", worktree=str(root), branch="b",
                                    status_file="s", handoff_file="h")
    launcher = ClaudeBgLauncher(routing, ClaudeBgConfig(settings_path="/nonexistent"),
                                executor=recorder, remote_git_deny=deny)
    launcher._prepare_worktree = lambda: Path(root)
    launcher._bind = lambda short, role, issue: {"task_id": short}
    launcher.launch(role, "n1", contract)
    return [call for call in recorder.calls if "--bg" in call[0]][0]


class AllowCardLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_allow_developer_and_reviewer_get_ban_text_and_disallowed_push(self):
        for role in ("developer", "reviewer"):
            argv, env = _bg_call(self.tmp.name, False, role)
            # Still one option (a second one would start a copy on resume).
            self.assertEqual(argv.count("--disallowedTools"), 1, role)
            self.assertLess(argv.index("--disallowedTools"), argv.index("--"))
            for tool in ("AskUserQuestion",) + tuple(WORKER_PUSH_DISALLOWED_TOOLS):
                self.assertIn(tool, argv, (role, tool))
            self.assertIn("Bash(git push:*)", argv)
            self.assertIn("Bash(gh:*)", argv)
            self.assertIn(WORKER_PUSH_BAN_TEXT, argv[-1], role)
            self.assertIn("git push", WORKER_PUSH_BAN_TEXT)
            # allow keeps fetch usable: no protocol lock-out environment.
            self.assertIsNone(env)

    def test_deny_keeps_the_stricter_remote_git_ban(self):
        argv, env = _bg_call(self.tmp.name, True, "developer")
        self.assertIn(REMOTE_GIT_BAN_TEXT, argv[-1])
        self.assertIn("Bash(git fetch:*)", argv)
        self.assertIn("Bash(git push:*)", argv)
        self.assertEqual(env, {"GIT_ALLOW_PROTOCOL": "vibe-denied"})

    def test_provider_prompt_bans_push_under_allow_and_deny(self):
        runner = ProviderActionRunner(ProjectPaths(Path(self.tmp.name)), "claude-code",
                                      "claude-code-visible")
        for role in ("developer", "reviewer"):
            allow = runner._remote_git_ban({"remote_git_actions": "allow", "role": role})
            deny = runner._remote_git_ban({"remote_git_actions": "deny", "role": role})
            self.assertIn(WORKER_PUSH_BAN_TEXT, allow)
            self.assertIn(REMOTE_GIT_BAN_TEXT, deny)
        # No card at all still bans the push.
        self.assertIn(WORKER_PUSH_BAN_TEXT, runner._remote_git_ban({}))


def _repo_with_origin(base: Path):
    origin = base / "origin.git"
    repo = base / "repo"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-qm", "base")
    git(repo, "remote", "add", "origin", str(origin))
    git(repo, "push", "-q", "origin", "main")
    return repo


class RemoteBranchCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = _repo_with_origin(Path(self.tmp.name))

    def test_a_pushed_node_branch_is_listed(self):
        git(self.repo, "branch", "node/alpha-1")
        git(self.repo, "branch", "node/beta-2")
        git(self.repo, "push", "-q", "origin", "node/alpha-1")
        result = remote_node_branch_deviations(self.repo, ["node/alpha-1", "node/beta-2"])
        self.assertTrue(result["checked"])
        self.assertEqual(result["branches"], ["node/alpha-1"])

    def test_no_pushed_node_branch_is_checked_and_empty(self):
        result = remote_node_branch_deviations(self.repo, ["node/alpha-1"])
        self.assertEqual(result, {"checked": True, "branches": []})

    def test_unreadable_remote_says_not_checked(self):
        git(self.repo, "remote", "set-url", "origin", str(Path(self.tmp.name) / "gone.git"))
        result = remote_node_branch_deviations(self.repo, ["node/alpha-1"])
        self.assertFalse(result["checked"])
        self.assertEqual(result["branches"], [])
        self.assertIn("未检查", result["reason"])

    def test_remote_delivery_ring_push_is_not_banned(self):
        # The ring pushes its integration branch itself; nothing in the
        # worker ban reaches the monitor's own git process.
        git(self.repo, "branch", "vibe/integration-candidate-plan-1")
        facts = SubprocessRemoteDeliveryExecutor(self.repo, plan_id="plan-1").execute(
            "push", {"source_branch": "vibe/integration-candidate-plan-1"})
        self.assertFalse(facts["already_pushed"])
        listed = git(self.repo, "ls-remote", "origin", "refs/heads/vibe/integration-candidate-plan-1")
        self.assertIn(facts["head_sha"], listed)
        # And the integration branch is not a node-branch deviation.
        result = remote_node_branch_deviations(self.repo, [])
        self.assertEqual(result["branches"], [])

    def test_delivery_report_lists_the_deviation(self):
        git(self.repo, "branch", "node/alpha-1")
        git(self.repo, "push", "-q", "origin", "node/alpha-1")
        snapshot = SimpleNamespace(
            run_id="run-1", plan_id="plan-1",
            nodes={"alpha": {"status": "accepted", "branch": "node/alpha-1"},
                   "integration-review": {"status": "accepted"}},
        )
        paths = ProjectPaths(self.repo)
        _write_delivery_report(paths, snapshot, {"steps": {}})
        report = json.loads((paths.vibe / "runs" / "run-1" / "remote-delivery-report.json")
                            .read_text(encoding="utf-8"))
        self.assertEqual(report["deviations"]["remote_node_branches"],
                         {"checked": True, "branches": ["node/alpha-1"]})


class StatusDeviationTests(unittest.TestCase):
    tearDown = _gate.BackgroundDeliveryGateTests.tearDown
    _engine_plan = _gate.BackgroundDeliveryGateTests._engine_plan
    monitor = _gate.BackgroundDeliveryGateTests.monitor

    def setUp(self):
        _gate.BackgroundDeliveryGateTests.setUp(self)
        from tests.test_claude_bg_reconnect import FakeLauncher
        from vibe_guide.runners.claude_bg import ClaudeBgRunner

        root = self.paths.root
        git(root, "init", "-q", "-b", "main")
        (root / ".gitignore").write_text(".vibe/\n.worktrees/\n", encoding="utf-8")
        git(root, "add", ".gitignore")
        git(root, "commit", "-qm", "baseline")
        origin_dir = tempfile.TemporaryDirectory()
        self.addCleanup(origin_dir.cleanup)
        self.origin = Path(origin_dir.name) / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True, capture_output=True)
        git(root, "remote", "add", "origin", str(self.origin))
        git(root, "push", "-q", "origin", "main")
        runner = ClaudeBgRunner(lambda c, w: FakeLauncher("working"), paths=self.paths)
        self.snapshot = self.monitor().start(self.record, runner)
        self.branch = self.snapshot.nodes["bg"]["branch"]
        self.assertTrue(self.branch.startswith("node/"), self.branch)
        git(root, "branch", self.branch)

    def _closed_out(self):
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        for node in snapshot.nodes.values():
            node["status"] = "accepted"
        return snapshot

    def test_status_json_lists_a_pushed_node_branch(self):
        git(self.paths.root, "push", "-q", "origin", self.branch)
        result = _snapshot_result("status", self._closed_out(), True, paths=self.paths)
        deviation = result.payload["deviations"]["remote_node_branches"]
        self.assertEqual(deviation, {"checked": True, "branches": [self.branch]})
        self.assertIn(self.branch, result.text)

    def test_status_json_says_not_checked_when_remote_is_unreadable(self):
        git(self.paths.root, "remote", "set-url", "origin", str(self.origin) + "-gone")
        result = _snapshot_result("status", self._closed_out(), True, paths=self.paths)
        deviation = result.payload["deviations"]["remote_node_branches"]
        self.assertFalse(deviation["checked"])
        self.assertIn("未检查", deviation["reason"])
        self.assertIn("未检查", result.text)

    def test_status_before_closeout_does_not_query_the_remote(self):
        git(self.paths.root, "push", "-q", "origin", self.branch)
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        result = _snapshot_result("status", snapshot, True, paths=self.paths)
        self.assertNotIn("deviations", result.payload)


if __name__ == "__main__":
    unittest.main()
