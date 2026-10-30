"""Remote Git ban: command line, environment and prompt text only.

Nothing here runs a real git push or starts a real session; the executor is a
recording fake.
"""

import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.claude_bg import ClaudeBgConfig, ClaudeBgLauncher, CommandResult
from vibe_guide.adapters.task_provider import BackgroundTaskRouting
from vibe_guide.authorization import (
    REMOTE_GIT_BAN_TEXT,
    REMOTE_GIT_ENFORCEMENT,
    remote_git_denied,
)
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import ProviderActionRunner


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, timeout, env=None):
        self.calls.append((list(argv), env))
        if "--bg" in argv and "--resume" not in argv:
            return CommandResult(0, "backgrounded · abc12345\n", "")
        return CommandResult(0, "", "")


def _launcher(root, deny, recorder):
    config = ClaudeBgConfig(settings_path="/nonexistent/settings.json")
    routing = BackgroundTaskRouting(
        host="local", worktree=str(root), branch="b",
        status_file="s", handoff_file="h",
    )
    launcher = ClaudeBgLauncher(routing, config, executor=recorder, remote_git_deny=deny)
    launcher._prepare_worktree = lambda: Path(root)
    launcher._bind = lambda short, role, issue: {"task_id": short}
    return launcher


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.contract = Path(self.tmp.name) / "c.md"
        self.contract.write_text("do the work", encoding="utf-8")

    def _bg_call(self, deny):
        recorder = Recorder()
        _launcher(self.tmp.name, deny, recorder).launch("developer", "n1", self.contract)
        return [c for c in recorder.calls if "--bg" in c[0]][0]

    def test_deny_adds_flags_env_and_prompt(self):
        argv, env = self._bg_call(True)
        self.assertIn("--disallowedTools", argv)
        self.assertEqual(env, {"GIT_ALLOW_PROTOCOL": "vibe-denied"})
        self.assertIn(REMOTE_GIT_BAN_TEXT, argv[-1])
        self.assertLess(argv.index("--disallowedTools"), argv.index("--"))

    def test_allow_adds_nothing(self):
        argv, env = self._bg_call(False)
        self.assertNotIn("--disallowedTools", argv)
        self.assertIsNone(env)
        self.assertNotIn(REMOTE_GIT_BAN_TEXT, argv[-1])


class PromptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))

    def _runner(self, provider):
        return ProviderActionRunner(self.paths, provider, provider + "-visible")

    def test_ban_follows_the_contract_switch_for_all_platforms(self):
        for provider in ("claude-code", "codex", "kimi-code", "workbuddy"):
            runner = self._runner(provider)
            self.assertIn(
                REMOTE_GIT_BAN_TEXT,
                runner._remote_git_ban({"remote_git_actions": "deny"}),
            )
            self.assertEqual(runner._remote_git_ban({"remote_git_actions": "allow"}), "")

    def test_card_on_disk_decides_when_contract_is_silent(self):
        plan = Path(self.tmp.name) / ".vibe/plans/p1"
        plan.mkdir(parents=True)
        (plan / "authorization-card.json").write_text(
            json.dumps({"remote_git_actions": "deny"}), encoding="utf-8")
        self.assertTrue(remote_git_denied(self.paths, {"plan_id": "p1"}))
        (plan / "authorization-card.json").write_text(
            json.dumps({"remote_git_actions": "allow"}), encoding="utf-8")
        self.assertFalse(remote_git_denied(self.paths, {"plan_id": "p1"}))
        self.assertFalse(remote_git_denied(self.paths, {"plan_id": "missing"}))


class CardWordingTests(unittest.TestCase):
    def test_unenforced_platforms_are_labelled(self):
        notes = {e["platform"]: e["note"] for e in REMOTE_GIT_ENFORCEMENT}
        for name in ("codex", "kimi-code", "workbuddy", "claude-code 可见任务"):
            self.assertIn("未强制", notes[name])
        self.assertIn("部分拦截", notes["claude-code 后台派发"])
        self.assertIn("未验证", notes["claude-code 后台派发"])


if __name__ == "__main__":
    unittest.main()
