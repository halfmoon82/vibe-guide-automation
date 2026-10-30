import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from vibe_guide.adapters.claude_bg import (
    CLAUDE_BG_PROVIDER,
    ClaudeBgConfig,
    ClaudeBgConfigRefused,
    ClaudeBgLauncher,
    ClaudeBgUnknown,
    ClaudeBgWorktreeBlocked,
    WORKTREE_MISSING,
    CommandResult,
    load_claude_bg_config,
    subprocess_executor,
)
from vibe_guide.adapters.task_provider import BackgroundTaskProvider, BackgroundTaskRouting
from vibe_guide.paths import ProjectPaths

UUID = "1a2b3c4d-1111-4222-8333-444455556666"
SHORT = "1a2b3c4d"


class FakeClaude:
    def __init__(self, worktree, state="done", launch_out=None, agents_cwd=None):
        self.calls = []
        self.git_calls = []
        self.state = state
        self.launch_out = launch_out if launch_out is not None else "backgrounded · %s · node\n" % SHORT
        self.agents_cwd = agents_cwd or str(worktree)
        self.agents_out = None
        self.error = None
        self.returncodes = {}
        self.agents_error = None
        self.resume_out = "backgrounded · %s · node\n" % SHORT
        self.resume_err = ""
        self.resume_outs = []
        self.bg_error = None

    def __call__(self, argv, cwd, timeout):
        argv = list(argv)
        if argv[:1] == ["git"]:
            # Worktree preflight and identity writes exercise the real git
            # repository; only the Claude process is fake in these tests.
            self.git_calls.append((argv, Path(cwd)))
            return subprocess_executor(argv, cwd, timeout)
        self.calls.append((argv, Path(cwd)))
        if self.error:
            raise self.error
        if argv[:2] == ["claude", "agents"]:
            if self.agents_error:
                raise self.agents_error
            if self.agents_out is not None:
                return CommandResult(0, self.agents_out)
            entry = {"kind": "background", "sessionId": UUID, "cwd": self.agents_cwd, "state": self.state}
            return CommandResult(0, json.dumps([entry]))
        if "--resume" in argv:
            if self.resume_outs:
                out, err = self.resume_outs.pop(0)
                return CommandResult(0, out, err)
            return CommandResult(self.returncodes.get("resume", 0), self.resume_out, self.resume_err)
        if "--bg" in argv:
            if self.bg_error:
                raise self.bg_error
            return CommandResult(self.returncodes.get("bg", 0), self.launch_out)
        key = argv[1] if len(argv) > 1 else argv[0]
        return CommandResult(self.returncodes.get(key, 0), "")


class LauncherCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()

        def git(*args):
            return subprocess.run(
                ["git", "-C", str(self.repo), *args],
                check=True,
                capture_output=True,
                text=True,
            )

        git("init", "-q", "-b", "main")
        git("config", "user.email", "dev@example.com")
        git("config", "user.name", "Dev")
        git("config", "extensions.worktreeConfig", "true")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git("add", "base.txt")
        git("commit", "-qm", "base")
        self.base = git("rev-parse", "HEAD").stdout.strip()
        self.worktree = self.repo / ".worktrees" / "wt"
        git("worktree", "add", "-q", "-b", "node/a", str(self.worktree), self.base)
        self.contract = self.tmp / "contract.md"
        self.contract.write_text("do the thing", encoding="utf-8")
        self.routing = BackgroundTaskRouting("host", str(self.worktree), "node/a", "s.json", "h.json")
        self.config = ClaudeBgConfig(settings_path="/abs/settings.json")
        self.fake = FakeClaude(self.worktree)

    def launcher(self, **kw):
        config = kw.pop("config", self.config)
        kw.setdefault("sleeper", lambda seconds: None)
        return ClaudeBgLauncher(self.routing, config, executor=self.fake, **kw)


class LaunchTest(LauncherCase):
    def test_acceptance_returns_full_uuid_and_matches_routing(self):
        launcher = self.launcher()
        raw = launcher.launch("developer", "n1", self.contract)
        self.assertEqual(raw["task_id"], UUID)
        self.assertEqual(raw["short_id"], SHORT)
        provider = BackgroundTaskProvider(CLAUDE_BG_PROVIDER, launcher, self.routing)
        binding = provider.create("developer", "n1", self.contract)
        self.assertEqual(binding.task_id, UUID)
        self.assertEqual(binding.mode, "background")

    def test_launch_denies_ask_user_question_before_the_prompt_separator(self):
        self.launcher().launch("developer", "n1", self.contract)
        argv = self.fake.calls[0][0]
        self.assertEqual(argv[argv.index("--disallowedTools") + 1], "AskUserQuestion")
        # Variadic flag: after `--` the prompt would be read as a tool name.
        self.assertLess(argv.index("--disallowedTools"), argv.index("--"))
        self.assertEqual(argv.count("--disallowedTools"), 1)

    def test_launch_argv_and_cwd(self):
        self.launcher(model="opus").launch("developer", "n1", self.contract)
        argv, cwd = self.fake.calls[0]
        self.assertEqual(cwd, self.worktree)
        for token in ("--bg", "--model", "--settings", "/abs/settings.json", "--permission-mode", "bypassPermissions", "do the thing"):
            self.assertIn(token, argv)
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "bypassPermissions")

    def test_prompt_follows_double_dash(self):
        # Live: a prompt starting with "-" is parsed as an option without "--".
        self.contract.write_text("-- starts with a dash", encoding="utf-8")
        self.launcher().launch("developer", "n1", self.contract)
        argv, _ = self.fake.calls[0]
        self.assertEqual(argv[-2:], ["--", "-- starts with a dash"])

    def test_ansi_in_output_is_tolerated(self):
        self.fake.launch_out = "\x1b[32mbackgrounded\x1b[0m · %s · n\n" % SHORT
        self.assertEqual(self.launcher().launch("developer", "n1", self.contract)["task_id"], UUID)

    def test_git_identity_set_in_worktree(self):
        config = ClaudeBgConfig("/abs/s.json", git_user_name="Bot", git_user_email="bot@x.io")
        self.launcher(config=config).launch("developer", "n1", self.contract)
        git = [(a, c) for a, c in self.fake.git_calls if a[:2] == ["git", "config"]]
        # --worktree: plain `git config` in a linked worktree writes the shared
        # .git/config and changes the identity of every other worktree.
        self.assertEqual([a for a, _ in git], [["git", "config", "--worktree", "user.name", "Bot"], ["git", "config", "--worktree", "user.email", "bot@x.io"]])
        self.assertTrue(all(c == self.worktree for _, c in git))

    def test_no_git_identity_means_no_git_config_call(self):
        self.launcher().launch("developer", "n1", self.contract)
        self.assertFalse([a for a, _ in self.fake.git_calls if a[:2] == ["git", "config"]])

    def test_plain_directory_is_worktree_missing(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        routing = BackgroundTaskRouting("host", str(plain), "node/a", "s.json", "h.json")
        launcher = ClaudeBgLauncher(routing, self.config, executor=self.fake)
        with self.assertRaises(ClaudeBgWorktreeBlocked) as caught:
            launcher.launch("developer", "n1", self.contract)
        self.assertEqual(caught.exception.reason_code, WORKTREE_MISSING)
        self.assertEqual(self.fake.calls, [])

    def test_unparseable_output_is_unknown(self):
        self.fake.launch_out = "something else"
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertIn("short id", ctx.exception.reason)

    def test_agents_entry_missing_is_unknown(self):
        self.fake.agents_out = "[]"
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertIn("not found", ctx.exception.reason)

    def test_agents_bad_json_is_unknown(self):
        self.fake.agents_out = "not json"
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().launch("developer", "n1", self.contract)

    def test_wrong_cwd_is_unknown(self):
        self.fake.agents_cwd = str(self.tmp)
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().launch("developer", "n1", self.contract)

    def test_nonzero_exit_is_unknown(self):
        self.fake.returncodes["bg"] = 1
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().launch("developer", "n1", self.contract)

    def test_executor_errors_propagate_with_reason(self):
        self.fake.error = ClaudeBgUnknown("claude command not found")
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertTrue(ctx.exception.reason.startswith("claude command not found"))

    def test_missing_worktree_is_unknown(self):
        routing = BackgroundTaskRouting("h", str(self.tmp / "nope"), "b", "s", "h")
        launcher = ClaudeBgLauncher(routing, self.config, executor=self.fake)
        with self.assertRaises(ClaudeBgUnknown):
            launcher.launch("developer", "n1", self.contract)


class DefaultExecutorTest(unittest.TestCase):
    def test_missing_command_is_unknown(self):
        def missing(*args, **kwargs):
            raise FileNotFoundError("claude")

        with mock.patch("vibe_guide.adapters.claude_bg.subprocess.run", missing):
            with self.assertRaises(ClaudeBgUnknown) as ctx:
                subprocess_executor(["claude", "agents"], Path.cwd(), 5)
        self.assertIn("not found", ctx.exception.reason)

    def test_timeout_is_unknown(self):
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            subprocess_executor(["sleep", "5"], Path.cwd(), 0.2)
        self.assertIn("timed out", ctx.exception.reason)

    def test_desktop_host_auth_flag_is_not_inherited(self):
        # Live 2026-10-14: launched from a Claude Code desktop session, the
        # inherited flag makes every background session "Not logged in".
        import os
        import sys
        env = {"CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST": "1", "VIBE_KEEP_ME": "yes"}
        with mock.patch.dict(os.environ, env):
            out = subprocess_executor(
                [sys.executable, "-I", "-c",
                 "import os; print(os.environ.get('CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST', '-'),"
                 " os.environ.get('VIBE_KEEP_ME', '-'))"],
                Path.cwd(), 10,
            )
        self.assertEqual(out.stdout.split(), ["-", "yes"])


class StatusTest(LauncherCase):
    def test_five_states(self):
        for state in ("working", "done", "failed", "blocked", "stopped"):
            self.fake.state = state
            self.assertEqual(self.launcher().status(UUID), state)

    def test_unknown_state(self):
        self.fake.state = "weird"
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().status(UUID)

    def test_not_found(self):
        self.fake.agents_out = "[]"
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().status(UUID)


class ControlTest(LauncherCase):
    def test_resume_stops_then_resumes_with_full_uuid_only(self):
        self.launcher().resume(UUID, "rework please")
        self.assertEqual(self.fake.calls[0][0], ["claude", "stop", SHORT])
        self.assertEqual(self.fake.calls[1][0], ["claude", "--bg", "--resume", UUID, "--", "rework please"])
        self.assertEqual(len(self.fake.calls), 2)

    def test_resume_passes_no_options_so_the_session_is_continued_not_copied(self):
        # A session keeps the options it was launched with (including
        # --disallowedTools); any option on --resume starts a copy (live).
        self.launcher().resume(UUID, "rework please")
        argv = self.fake.calls[1][0]
        self.assertNotIn("--disallowedTools", argv)
        self.assertEqual(argv, ["claude", "--bg", "--resume", UUID, "--", "rework please"])

    def test_resume_rejects_short_id(self):
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().resume(SHORT, "x")
        self.assertEqual(self.fake.calls, [])

    def test_stop_and_remove_use_short_id(self):
        launcher = self.launcher()
        launcher.stop(UUID)
        launcher.remove(UUID)
        self.assertEqual([a for a, _ in self.fake.calls], [["claude", "stop", SHORT], ["claude", "rm", SHORT]])

    def test_visibility_and_locate(self):
        launcher = self.launcher()
        vis = launcher.visibility()
        self.assertFalse(vis.visible)
        self.assertFalse(vis.direct_enter)
        self.assertEqual(launcher.locate(UUID), {"command": "claude attach " + SHORT, "terminal_only": True})

    def test_final_reply_last_assistant_text(self):
        transcript = self.tmp / "t.jsonl"
        rows = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "first"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "DONE"}]}},
        ]
        transcript.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n", encoding="utf-8")
        launcher = self.launcher(transcript_locator=lambda task_id: transcript)
        self.assertEqual(launcher.final_reply(UUID), "DONE")
        self.assertIsNone(self.launcher(transcript_locator=lambda t: None).final_reply(UUID))


COPY = "aaaaaaaa"


class ResumeSafetyTest(LauncherCase):
    def argvs(self):
        return [a for a, _ in self.fake.calls]

    def test_stop_failed_and_still_working_does_not_resume(self):
        self.fake.returncodes["stop"] = 1
        self.fake.state = "working"
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().resume(UUID, "x")
        self.assertFalse([a for a in self.argvs() if "--resume" in a])

    def test_stop_failed_and_status_unknown_does_not_resume(self):
        self.fake.returncodes["stop"] = 1
        self.fake.agents_out = "[]"
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().resume(UUID, "x")
        self.assertFalse([a for a in self.argvs() if "--resume" in a])

    def test_stop_failed_but_done_resumes(self):
        self.fake.returncodes["stop"] = 1
        self.fake.state = "done"
        self.launcher().resume(UUID, "x")
        self.assertTrue([a for a in self.argvs() if "--resume" in a])

    def test_stop_of_an_already_ended_session_is_a_proven_stop(self):
        # `claude stop` may fail for a session that already exited after
        # delivering (#262 follow-up); its listed end state proves the stop.
        for state in ("done", "stopped", "failed"):
            with self.subTest(state=state):
                self.fake.returncodes["stop"] = 1
                self.fake.state = state
                self.launcher().stop(UUID)

    def test_stop_failed_and_session_not_ended_still_raises(self):
        # `blocked` waits for input: alive, and could write again.
        for state in ("working", "blocked"):
            with self.subTest(state=state):
                self.fake.returncodes["stop"] = 1
                self.fake.state = state
                with self.assertRaises(ClaudeBgUnknown):
                    self.launcher().stop(UUID)

    def test_stop_failed_and_status_unknown_still_raises(self):
        self.fake.returncodes["stop"] = 1
        self.fake.agents_out = "[]"
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().stop(UUID)

    def test_copy_message_in_stderr_detected_and_copy_stopped(self):
        self.fake.resume_out = "backgrounded · %s · node\n" % COPY
        self.fake.resume_err = "started a copy of the session"
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().resume(UUID, "x")
        self.assertIn(COPY, ctx.exception.reason)
        self.assertIn(["claude", "stop", COPY], self.argvs())

    def test_copy_message_in_stderr_alone_detected(self):
        self.fake.resume_err = "started a copy"
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().resume(UUID, "x")
        self.assertEqual(self.argvs().count(["claude", "stop", SHORT]), 1)

    def test_short_id_mismatch_detected_and_copy_stopped(self):
        self.fake.resume_out = "backgrounded · %s · node\n" % COPY
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().resume(UUID, "x")
        self.assertIn(COPY, ctx.exception.reason)
        self.assertIn(["claude", "stop", COPY], self.argvs())

    def test_unparseable_resume_output_is_copy_suspicion(self):
        self.fake.resume_out = ""
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().resume(UUID, "x")


RUNNING_COPY = (
    "backgrounded · %s · node\n" % COPY,
    "note: session %s is already running in the background, so this started a copy as %s." % (SHORT, COPY),
)
CONTINUED = ("backgrounded · %s · node\n" % SHORT, "note: woke session %s with its saved options" % SHORT)


class ResumeSettleTest(LauncherCase):
    """Live 2026-10-14: right after `claude stop` the session still counts as
    running for a few seconds and an immediate resume starts a copy."""

    def setUp(self):
        super().setUp()
        self.sleeps = []

    def launcher(self, **kw):
        return super().launcher(sleeper=self.sleeps.append, **kw)

    def argvs(self):
        return [a for a, _ in self.fake.calls]

    def test_waits_after_stop_before_resuming(self):
        self.launcher().resume(UUID, "x")
        self.assertTrue(self.sleeps and self.sleeps[0] > 0)

    def test_still_running_copy_is_removed_and_resume_retried(self):
        self.fake.resume_outs = [RUNNING_COPY, CONTINUED]
        self.launcher().resume(UUID, "x")
        argvs = self.argvs()
        self.assertEqual([a for a in argvs if "--resume" in a], [["claude", "--bg", "--resume", UUID, "--", "x"]] * 2)
        self.assertIn(["claude", "stop", COPY], argvs)
        self.assertIn(["claude", "rm", COPY], argvs)
        self.assertEqual(len(self.sleeps), 2)
        self.assertGreater(self.sleeps[1], self.sleeps[0])

    def test_copy_stop_failure_is_not_reported_as_removed(self):
        self.fake.resume_outs = [RUNNING_COPY] * 10
        self.fake.returncodes["stop"] = 1
        self.fake.returncodes["rm"] = 1
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().resume(UUID, "x")
        self.assertIn("cleanup failed", ctx.exception.reason)

    def test_gives_up_after_bounded_retries(self):
        self.fake.resume_outs = [RUNNING_COPY] * 10
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().resume(UUID, "x")
        self.assertIn("still running", ctx.exception.reason)
        self.assertEqual(len([a for a in self.argvs() if "--resume" in a]), 3)

    def test_saved_options_copy_is_not_retried(self):
        flags = ("backgrounded · %s · node\n" % COPY,
                 "note: background session %s keeps its own saved options, so the flags you passed started a copy as %s." % (SHORT, COPY))
        self.fake.resume_outs = [flags, CONTINUED]
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().resume(UUID, "x")
        self.assertEqual(len([a for a in self.argvs() if "--resume" in a]), 1)


class HardeningTest(LauncherCase):
    def argvs(self):
        return [a for a, _ in self.fake.calls]

    def test_agents_timeout_after_launch_stops_session(self):
        self.fake.agents_error = ClaudeBgUnknown("claude timed out after 60s")
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertIn(SHORT, ctx.exception.reason)
        self.assertIn("stop", ctx.exception.reason)
        self.assertIn(["claude", "stop", SHORT], self.argvs())

    def test_launch_timeout_stops_stray_session_it_started(self):
        # Live: a launch that times out client-side still leaves a working
        # bypassPermissions session behind.
        old = "bbbbbbbb-1111-4222-8333-444455556666"
        self.fake.bg_error = ClaudeBgUnknown("claude timed out after 60s")
        self.fake.agents_out = json.dumps([
            {"sessionId": UUID, "name": "n1-developer", "cwd": str(self.worktree),
             "state": "working", "startedAt": 10 ** 13},
            {"sessionId": old, "name": "n1-developer", "cwd": str(self.worktree),
             "state": "working", "startedAt": 0},
        ])
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertIn("timed out", ctx.exception.reason)
        self.assertIn(SHORT, ctx.exception.reason)
        self.assertIn(["claude", "stop", SHORT], self.argvs())
        self.assertNotIn(["claude", "stop", old[:8]], self.argvs())

    def stray(self, **override):
        entry = {"sessionId": UUID, "name": "n1-developer", "cwd": str(self.worktree),
                 "state": "working", "startedAt": 10 ** 13}
        entry.update(override)
        return entry

    def launch_failing(self, entries):
        self.fake.bg_error = ClaudeBgUnknown("claude timed out after 60s")
        self.fake.agents_out = json.dumps(entries)
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        return ctx.exception.reason

    def test_stray_check_spares_other_names_dirs_and_stopped(self):
        # Each guard alone must keep a session out of the stop list.
        for override in ({"name": "n2-developer"}, {"cwd": str(self.tmp)}, {"state": "stopped"}):
            with self.subTest(override=override):
                self.fake.calls.clear()
                reason = self.launch_failing([self.stray(**override)])
                self.assertNotIn(["claude", "stop", SHORT], self.argvs())
                self.assertIn("stopped: none", reason)

    def test_stray_stop_failure_is_not_reported_as_stopped(self):
        self.fake.returncodes["stop"] = 1
        reason = self.launch_failing([self.stray()])
        self.assertIn(SHORT + "(stop failed)", reason)

    def test_stray_with_unreadable_start_time_is_unknown_not_none(self):
        reason = self.launch_failing([self.stray(startedAt="2026-10-14T09:00:00Z")])
        self.assertNotIn(["claude", "stop", SHORT], self.argvs())
        self.assertIn("stray check unknown", reason)

    def test_launch_failure_cleanup_query_failure_is_reported(self):
        self.fake.bg_error = ClaudeBgUnknown("claude timed out after 60s")
        self.fake.agents_error = ClaudeBgUnknown("claude agents timed out")
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertIn("stray check unknown", ctx.exception.reason)

    def test_failed_stop_during_cleanup_is_ignored(self):
        self.fake.agents_out = "[]"
        self.fake.returncodes["stop"] = 1
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertIn(SHORT, ctx.exception.reason)

    def test_multiple_matches_unknown_and_stops(self):
        entries = [
            {"sessionId": UUID, "cwd": str(self.worktree), "state": "done"},
            {"sessionId": SHORT + "-2222-4333-8444-555566667777", "cwd": str(self.worktree), "state": "done"},
        ]
        self.fake.agents_out = json.dumps(entries)
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            self.launcher().launch("developer", "n1", self.contract)
        self.assertIn("several", ctx.exception.reason)
        self.assertIn(["claude", "stop", SHORT], self.argvs())

    def test_other_session_not_matched(self):
        other = "ffffffff-1111-4222-8333-444455556666"
        entries = [
            {"sessionId": other, "cwd": str(self.worktree), "state": "done"},
            {"sessionId": UUID, "cwd": str(self.worktree), "state": "done"},
        ]
        self.fake.agents_out = json.dumps(entries)
        self.assertEqual(self.launcher().launch("developer", "n1", self.contract)["task_id"], UUID)
        self.fake.agents_out = json.dumps(entries[:1])
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().launch("developer", "n1", self.contract)

    def test_missing_cwd_fails_closed(self):
        self.fake.agents_out = json.dumps([{"sessionId": UUID, "state": "done"}])
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().launch("developer", "n1", self.contract)

    def test_malformed_uuid_entry_unknown(self):
        self.fake.agents_out = json.dumps([{"sessionId": SHORT + "-bad", "cwd": str(self.worktree)}])
        with self.assertRaises(ClaudeBgUnknown):
            self.launcher().launch("developer", "n1", self.contract)

    def test_uuid_with_trailing_newline_rejected(self):
        launcher = self.launcher()
        for call in (launcher.status, launcher.stop, launcher.remove, launcher.final_reply, launcher.locate):
            with self.assertRaises(ClaudeBgUnknown):
                call(UUID + "\n")
        with self.assertRaises(ClaudeBgUnknown):
            launcher.resume(UUID + "\n", "x")

    def test_launch_argv_has_name(self):
        self.launcher().launch("developer", "n1", self.contract)
        argv = self.fake.calls[0][0]
        self.assertEqual(argv[argv.index("-n") + 1], "n1-developer")


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name).resolve()
        self.root = base / "proj"
        self.home = base / "home"
        (self.root / ".vibe").mkdir(parents=True)
        self.home.mkdir()
        self.paths = ProjectPaths(self.root, vibe_home=self.home)

    def write(self, where, data):
        (where / "config.json").write_text(json.dumps(data), encoding="utf-8")

    def test_project_credentials_refused(self):
        self.write(self.root / ".vibe", {"claude_bg": {"settings_path": "/x/s.json"}})
        self.write(self.home, {"claude_bg": {"settings_path": "/abs/s.json"}})
        with self.assertRaises(ClaudeBgConfigRefused) as ctx:
            load_claude_bg_config(self.paths)
        self.assertIn("user-level", str(ctx.exception))
        self.assertIn("settings_path", str(ctx.exception))

    def test_user_missing_settings_path_is_unknown(self):
        self.write(self.root / ".vibe", {"claude_bg": {"enabled": True}})
        with self.assertRaises(ClaudeBgUnknown) as ctx:
            load_claude_bg_config(self.paths)
        self.assertIn("credentials not configured", ctx.exception.reason)

    def test_enabled_only_in_project_is_allowed(self):
        self.write(self.root / ".vibe", {"claude_bg": {"enabled": True}})
        self.write(self.home, {"claude_bg": {"settings_path": "/abs/s.json", "model": "opus", "git_user_name": "B"}})
        config = load_claude_bg_config(self.paths)
        self.assertEqual((config.settings_path, config.model, config.git_user_name), ("/abs/s.json", "opus", "B"))

    def good_user(self):
        self.write(self.home, {"claude_bg": {"settings_path": "/abs/s.json"}})

    def test_project_section_not_a_dict_refused(self):
        self.good_user()
        for bad in ("x", ["settings_path"], True):
            self.write(self.root / ".vibe", {"claude_bg": bad})
            with self.assertRaises(ClaudeBgConfigRefused):
                load_claude_bg_config(self.paths)

    def test_project_config_symlink_rejected(self):
        self.good_user()
        real = self.root / ".vibe" / "real.json"
        real.write_text("{}", encoding="utf-8")
        (self.root / ".vibe" / "config.json").symlink_to(real)
        with self.assertRaises((ClaudeBgConfigRefused, ClaudeBgUnknown)) as ctx:
            load_claude_bg_config(self.paths)
        self.assertIn("symlink", str(ctx.exception))

    def test_vibe_dir_escape_rejected(self):
        self.good_user()
        outside = self.root.parent / "outside"
        outside.mkdir()
        (outside / "config.json").write_text("{}", encoding="utf-8")
        (self.root / ".vibe").rmdir()
        (self.root / ".vibe").symlink_to(outside)
        with self.assertRaises((ClaudeBgConfigRefused, ClaudeBgUnknown)):
            load_claude_bg_config(self.paths)

    def test_project_config_corrupt_or_not_object_is_unknown(self):
        self.good_user()
        for text in ("{not json", "[1, 2]"):
            (self.root / ".vibe" / "config.json").write_text(text, encoding="utf-8")
            with self.assertRaises(ClaudeBgUnknown) as ctx:
                load_claude_bg_config(self.paths)
            self.assertIn("cannot verify project config", ctx.exception.reason)

    def test_project_config_absent_is_fine(self):
        self.good_user()
        self.assertEqual(load_claude_bg_config(self.paths).settings_path, "/abs/s.json")

    def test_default_model_and_relative_path_rejected(self):
        self.write(self.home, {"claude_bg": {"settings_path": "/abs/s.json"}})
        self.assertEqual(load_claude_bg_config(self.paths).model, "sonnet")
        self.write(self.home, {"claude_bg": {"settings_path": "rel/s.json"}})
        with self.assertRaises(ClaudeBgUnknown):
            load_claude_bg_config(self.paths)


if __name__ == "__main__":
    unittest.main()
