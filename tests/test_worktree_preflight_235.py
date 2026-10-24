"""#235: a dispatch never silently waits for a worktree nobody will build.

Two halves, both pinned here:

* the background launcher builds a node worktree from the run baseline when
  the directory *and* the branch are absent, and refuses -- fail-closed, never
  overwriting -- as soon as the existing directory contradicts the contract;
* the block is a structured ``worktree_missing`` family code that survives
  persistence, so ``vibe status`` can name the command instead of showing
  自动修复中 for a retry that can never converge.
"""

import importlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

from vibe_guide import cli
from vibe_guide.adapters.claude_bg import (
    WORKTREE_BLOCKED_EVENT,
    WORKTREE_CREATE_FAILED,
    WORKTREE_CREATED_EVENT,
    WORKTREE_MISMATCH,
    WORKTREE_MISSING,
    WORKTREE_REASON_CODES,
    ClaudeBgConfig,
    ClaudeBgLauncher,
    ClaudeBgUnknown,
    ClaudeBgWorktreeBlocked,
    CommandResult,
    subprocess_executor,
)
from vibe_guide.adapters.task_provider import BackgroundTaskRouting
from vibe_guide.models import node_slug
from vibe_guide.state import (
    WORKTREE_ACTION_STATUS,
    WORKTREE_PREFLIGHT_KEY,
    WORKTREE_REASON_CODES as STATE_WORKTREE_REASON_CODES,
    append_event,
    load_events,
    load_snapshot,
    map_user_status,
    worktree_action,
    worktree_reason_code,
)
from vibe_guide.contracts import RunEvent
from vibe_guide.paths import ProjectPaths

UUID = "1a2b3c4d-1111-4222-8333-444455556666"
SHORT = "1a2b3c4d"


def git(repo, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=check,
        capture_output=True,
        text=True,
    )


class FakeClaude:
    """Only the claude surface; every git call goes to the real binary."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        argv = list(argv)
        self.calls.append((argv, Path(cwd)))
        if argv[:2] == ["claude", "agents"]:
            entry = {"kind": "background", "sessionId": UUID, "cwd": str(cwd), "state": "working"}
            return CommandResult(0, json.dumps([entry]))
        if "--bg" in argv:
            return CommandResult(0, "backgrounded · %s · node\n" % SHORT)
        return CommandResult(0, "")


class SplitExecutor:
    """Real git, fake claude: the preflight must finish before dispatch."""

    def __init__(self, claude=None):
        self.claude = claude or FakeClaude()
        self.git_calls = []

    def __call__(self, argv, cwd, timeout):
        if list(argv)[:1] == ["git"]:
            self.git_calls.append((list(argv), Path(cwd)))
            return subprocess_executor(argv, cwd, timeout)
        return self.claude(argv, cwd, timeout)

    @property
    def claude_calls(self):
        return self.claude.calls


class RepoCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "dev@example.com")
        git(self.repo, "config", "user.name", "Dev")
        (self.repo / "file.txt").write_text("baseline\n", encoding="utf-8")
        git(self.repo, "add", "file.txt")
        git(self.repo, "commit", "-qm", "baseline")
        self.base = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.worktree = self.repo / ".worktrees" / "node-n1"
        self.branch = "node/n1"
        self.contract = self.tmp / "contract.md"
        self.contract.write_text("do the thing", encoding="utf-8")
        self.executor = SplitExecutor()

    def launcher(self, worktree=None, branch=None):
        routing = BackgroundTaskRouting(
            "host",
            str(worktree or self.worktree),
            branch or self.branch,
            "s.json",
            "h.json",
        )
        return ClaudeBgLauncher(
            routing, ClaudeBgConfig(settings_path="/abs/settings.json"), executor=self.executor
        )

    def branch_of(self, path):
        return git(path, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()

    def head_of(self, path):
        return git(path, "rev-parse", "HEAD").stdout.strip()


class CreatesMissingWorktreeTest(RepoCase):
    def test_missing_directory_and_branch_is_created_from_the_run_baseline(self):
        launcher = self.launcher()
        self.assertEqual(launcher._prepare_worktree(), self.worktree)

        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.branch_of(self.worktree), self.branch)
        self.assertEqual(self.head_of(self.worktree), self.base)
        # The baseline is the commit the supervisor itself is running from,
        # so a repaired node never starts from a moving target.
        add = [a for a, _ in self.executor.git_calls if "worktree" in a and "add" in a]
        self.assertEqual(add, [["git", "worktree", "add", str(self.worktree), "-b", self.branch, self.base]])

    def test_creation_is_reported_as_a_structured_event_payload(self):
        launcher = self.launcher()
        launcher._prepare_worktree()
        self.assertEqual(
            launcher.worktree_preflight,
            {
                "event": WORKTREE_CREATED_EVENT,
                "worktree": str(self.worktree),
                "branch": self.branch,
                "worktree_base": self.base,
            },
        )

    def test_launch_builds_the_worktree_and_then_starts_the_session_in_it(self):
        # The acceptance example: the background session starts directly,
        # in the directory that did not exist a moment ago.
        result = self.launcher().launch("developer", "n1", self.contract)
        self.assertEqual(result["task_id"], UUID)
        self.assertTrue(self.worktree.is_dir())
        bg = [cwd for argv, cwd in self.executor.claude_calls if "--bg" in argv]
        self.assertEqual(bg, [self.worktree.resolve()])

    def test_existing_branch_without_a_directory_is_not_created(self):
        git(self.repo, "branch", self.branch)
        launcher = self.launcher()
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            launcher._prepare_worktree()
        error = ctx.exception
        self.assertEqual(error.reason_code, WORKTREE_MISSING)
        self.assertFalse(self.worktree.exists())
        # The branch is untouched: a second writer is never invented.
        self.assertEqual(git(self.repo, "rev-parse", self.branch).stdout.strip(), self.base)
        self.assertEqual(
            error.command, "git worktree add %s %s" % (self.worktree, self.branch)
        )
        self.assertIn("already exists", error.reason)

    def test_remote_only_branch_counts_as_existing(self):
        origin = self.tmp / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
        git(self.repo, "remote", "add", "origin", str(origin))
        git(self.repo, "push", "-q", "origin", "main")
        git(self.repo, "push", "-q", "origin", "main:refs/heads/" + self.branch)
        git(self.repo, "branch", "-q", "-D", self.branch, check=False)
        self.assertNotEqual(
            git(self.repo, "show-ref", "--verify", "--quiet", "refs/heads/" + self.branch, check=False).returncode,
            0,
        )

        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            self.launcher()._prepare_worktree()
        self.assertEqual(ctx.exception.reason_code, WORKTREE_MISSING)
        self.assertFalse(self.worktree.exists())

    def test_launch_of_a_blocked_worktree_starts_no_session(self):
        git(self.repo, "branch", self.branch)
        with self.assertRaises(ClaudeBgWorktreeBlocked):
            self.launcher().launch("developer", "n1", self.contract)
        self.assertFalse([argv for argv, _ in self.executor.claude_calls if "--bg" in argv])

    def test_failed_git_worktree_add_is_readable_and_fail_closed(self):
        class Refusing(SplitExecutor):
            def __call__(self, argv, cwd, timeout):
                if "add" in argv and "worktree" in argv:
                    self.git_calls.append((list(argv), Path(cwd)))
                    return CommandResult(1, "", "fatal: could not create work tree dir")
                return super().__call__(argv, cwd, timeout)

        self.executor = Refusing()
        launcher = self.launcher()
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            launcher._prepare_worktree()
        self.assertEqual(ctx.exception.reason_code, WORKTREE_CREATE_FAILED)
        self.assertIn("could not create work tree dir", ctx.exception.reason)
        # The baseline was read before the add, so the command a supervisor
        # gets names the real commit -- not a <base> hole to guess (#235).
        self.assertEqual(
            ctx.exception.command,
            "git worktree add %s -b %s %s" % (self.worktree, self.branch, self.base),
        )
        # Not created is not started: the caller must not read success here.
        self.assertEqual(launcher.worktree_preflight["event"], WORKTREE_BLOCKED_EVENT)
        self.assertFalse([argv for argv, _ in self.executor.claude_calls if "--bg" in argv])

    def test_a_branch_git_cannot_take_is_reported_without_a_fake_command(self):
        launcher = self.launcher(branch="-not-a-branch")
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            launcher._prepare_worktree()
        self.assertEqual(ctx.exception.reason_code, WORKTREE_MISSING)
        self.assertIn("-not-a-branch", ctx.exception.reason)
        # No baseline was read, so the command must say so instead of
        # leaving a paste-able <base> placeholder (#235).
        self.assertIn("base 不可用", ctx.exception.command)
        self.assertFalse(ctx.exception.command.startswith("git worktree add"))

    def test_a_file_in_the_worktree_place_is_not_created_over(self):
        # A same-named file where the worktree belongs: git refuses, and the
        # launcher reports it readably instead of replacing the file (#235).
        self.worktree.parent.mkdir(parents=True, exist_ok=True)
        self.worktree.write_text("occupied\n", encoding="utf-8")
        launcher = self.launcher()
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            launcher._prepare_worktree()
        error = ctx.exception
        self.assertEqual(error.reason_code, WORKTREE_CREATE_FAILED)
        self.assertIn("already exists", error.reason)
        # The file was neither removed nor overwritten.
        self.assertTrue(self.worktree.is_file())
        self.assertEqual(self.worktree.read_text(encoding="utf-8"), "occupied\n")
        self.assertEqual(
            error.command,
            "git worktree add %s -b %s %s" % (self.worktree, self.branch, self.base),
        )
        self.assertEqual(launcher.worktree_preflight["event"], WORKTREE_BLOCKED_EVENT)
        self.assertFalse([argv for argv, _ in self.executor.claude_calls if "--bg" in argv])

    def test_launch_with_a_file_in_the_worktree_place_starts_no_session(self):
        self.worktree.parent.mkdir(parents=True, exist_ok=True)
        self.worktree.write_text("occupied\n", encoding="utf-8")
        with self.assertRaises(ClaudeBgWorktreeBlocked):
            self.launcher().launch("developer", "n1", self.contract)
        self.assertFalse([argv for argv, _ in self.executor.claude_calls if "--bg" in argv])
        self.assertEqual(self.worktree.read_text(encoding="utf-8"), "occupied\n")


class NeverOverwritesTest(RepoCase):
    def prepare(self):
        git(self.repo, "worktree", "add", "-q", "-b", self.branch, str(self.worktree), self.base)

    def test_existing_worktree_on_the_contract_branch_is_left_alone(self):
        self.prepare()
        launcher = self.launcher()
        self.assertEqual(launcher._prepare_worktree(), self.worktree)
        self.assertIsNone(launcher.worktree_preflight["event"])
        self.assertEqual(
            [a for a, _ in self.executor.git_calls if "worktree" in a and "add" in a], []
        )
        self.assertEqual(self.branch_of(self.worktree), self.branch)
        self.assertEqual(self.head_of(self.worktree), self.base)

    def test_existing_worktree_on_another_branch_is_reported_not_switched(self):
        self.prepare()
        git(self.worktree, "switch", "-q", "-c", "someone/else")
        before = self.head_of(self.worktree)
        launcher = self.launcher()
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            launcher._prepare_worktree()
        self.assertEqual(ctx.exception.reason_code, WORKTREE_MISMATCH)
        self.assertIn("someone/else", ctx.exception.reason)
        self.assertIn(self.branch, ctx.exception.reason)
        # Nothing was switched, reset or removed.
        self.assertEqual(self.branch_of(self.worktree), "someone/else")
        self.assertEqual(self.head_of(self.worktree), before)

    def test_detached_head_is_a_mismatch(self):
        git(self.repo, "worktree", "add", "-q", "--detach", str(self.worktree), self.base)
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            self.launcher()._prepare_worktree()
        self.assertEqual(ctx.exception.reason_code, WORKTREE_MISMATCH)
        self.assertIn("detached", ctx.exception.reason)

    def test_blocked_worktree_never_starts_a_session(self):
        self.prepare()
        git(self.worktree, "switch", "-q", "-c", "someone/else")
        with self.assertRaises(ClaudeBgWorktreeBlocked):
            self.launcher().launch("developer", "n1", self.contract)
        self.assertEqual(self.executor.claude_calls, [])

    def test_plain_directory_keeps_its_previous_behaviour(self):
        # A directory that is not a worktree root of its own makes no branch
        # claim, so there is nothing to contradict and no git call to make.
        plain = self.tmp / "plain"
        plain.mkdir()
        launcher = self.launcher(worktree=plain)
        self.assertEqual(launcher._prepare_worktree(), plain)
        self.assertEqual(self.executor.git_calls, [])

    def test_worktree_outside_any_repository_is_readable(self):
        outside = self.tmp / "outside" / "node-n1"
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            self.launcher(worktree=outside)._prepare_worktree()
        self.assertEqual(ctx.exception.reason_code, WORKTREE_MISSING)
        self.assertIn("no git repository", ctx.exception.reason)
        self.assertFalse(outside.exists())
        # There is no repository to read a baseline from, so the command says
        # the base is unavailable instead of faking a runnable one (#235).
        self.assertIn("base 不可用", ctx.exception.command)
        self.assertIn("不在任何 git 仓库内", ctx.exception.command)
        self.assertFalse(ctx.exception.command.startswith("git worktree add"))


class StructuredReasonTest(RepoCase):
    def blocked(self):
        git(self.repo, "branch", self.branch)
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            self.launcher()._prepare_worktree()
        return ctx.exception

    def test_reason_code_is_a_fixed_enum_shared_with_the_state_layer(self):
        self.assertEqual(tuple(WORKTREE_REASON_CODES), tuple(STATE_WORKTREE_REASON_CODES))
        self.assertIn(WORKTREE_MISSING, WORKTREE_REASON_CODES)
        self.assertIn(WORKTREE_MISMATCH, WORKTREE_REASON_CODES)
        self.assertIn(WORKTREE_CREATE_FAILED, WORKTREE_REASON_CODES)

    def test_the_observation_carries_no_provider_text(self):
        error = self.blocked()
        # Free-text classification must stay fail-closed: only structural
        # fields, so nothing in here can be read as a provider sentence.
        self.assertEqual(
            sorted(error.observation),
            ["branch", "reason_code", "worktree", "worktree_command"],
        )
        self.assertEqual(error.observation["reason_code"], WORKTREE_MISSING)
        self.assertEqual(error.to_dict(), error.observation)

    def test_it_is_still_a_provider_unknown_for_existing_callers(self):
        self.assertIsInstance(self.blocked(), ClaudeBgUnknown)


class EventPersistenceTest(unittest.TestCase):
    """The defect was an unreadable reason; persistence must keep the code."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / ".vibe").mkdir()
        self.paths = ProjectPaths(self.root)
        self.run_id = "run-235"

    def test_worktree_facts_survive_sanitization_verbatim(self):
        data = {
            "run_id": self.run_id,
            "node_id": "node-n1",
            "reason_code": WORKTREE_MISSING,
            "worktree": "/repo/.worktrees/node-n1",
            "branch": "node/n1",
            "worktree_base": "a" * 40,
            "worktree_command": "git worktree add /repo/.worktrees/node-n1 -b node/n1 <base>",
            # Provider text next to it must still be redacted: the point is
            # the structured code, not a hole in the redaction rules.
            "reason": "worktree is missing: /repo/.worktrees/node-n1",
        }
        append_event(self.paths, RunEvent(WORKTREE_BLOCKED_EVENT, data))
        stored = load_events(self.paths, self.run_id)[-1]["data"]
        self.assertEqual(stored["reason_code"], WORKTREE_MISSING)
        self.assertEqual(stored["worktree"], "/repo/.worktrees/node-n1")
        self.assertEqual(stored["branch"], "node/n1")
        self.assertEqual(stored["worktree_base"], "a" * 40)
        self.assertEqual(stored["worktree_command"], data["worktree_command"])
        self.assertEqual(stored["reason"], "[REDACTED_PROVIDER_TEXT]")


class UserStatusTest(unittest.TestCase):
    def blocked_node(self, **extra):
        record = {
            "status": "blocked_unknown",
            "binding_phase": "binding_repair_pending",
            "retryable_action": {"role": "developer", "reason_class": "binding_unknown"},
            WORKTREE_PREFLIGHT_KEY: {
                "reason_code": WORKTREE_MISSING,
                "worktree": "/repo/.worktrees/node-n1",
                "branch": "node/n1",
                "worktree_command": "git worktree add /repo/.worktrees/node-n1 -b node/n1 <base>",
            },
        }
        record.update(extra)
        return record

    def test_a_blocked_worktree_does_not_read_as_self_healing(self):
        self.assertEqual(map_user_status(self.blocked_node()), WORKTREE_ACTION_STATUS)
        self.assertNotEqual(map_user_status(self.blocked_node()), "自动修复中")

    def test_the_same_node_without_the_marker_still_reads_as_recovery(self):
        record = self.blocked_node()
        record.pop(WORKTREE_PREFLIGHT_KEY)
        self.assertEqual(map_user_status(record), "自动修复中")

    def test_a_bound_session_keeps_its_real_status(self):
        # The worktree label is only for a node no session is bound to; a
        # live session keeps whatever the pre-existing mapping says.
        record = self.blocked_node(active_task={"task_id": UUID, "role": "developer"})
        self.assertNotEqual(map_user_status(record), WORKTREE_ACTION_STATUS)
        running = self.blocked_node(status="running", active_task={"task_id": UUID})
        self.assertNotEqual(map_user_status(running), WORKTREE_ACTION_STATUS)

    def test_unknown_codes_are_not_trusted(self):
        record = self.blocked_node()
        record[WORKTREE_PREFLIGHT_KEY]["reason_code"] = "something_else"
        self.assertIsNone(worktree_reason_code(record))
        self.assertEqual(map_user_status(record), "自动修复中")

    def test_a_node_that_moved_on_keeps_its_own_status(self):
        # A marker left behind by an earlier block must not outlive the work.
        for status in ("delivered", "accepted", "review"):
            with self.subTest(status=status):
                record = self.blocked_node(status=status)
                self.assertNotEqual(map_user_status(record), WORKTREE_ACTION_STATUS)

    def test_a_decision_state_node_keeps_its_decision_label(self):
        # #235: the worktree label must never shadow a decision the user
        # still owes -- blocked_design / blocked_deploy read 需要你决定 even
        # with a stale worktree marker in the record, because a supervisor
        # cannot act on a worktree while the decision is open.
        for status in ("blocked_design", "blocked_deploy"):
            with self.subTest(status=status):
                record = {
                    "status": status,
                    WORKTREE_PREFLIGHT_KEY: {
                        "reason_code": WORKTREE_MISSING,
                        "worktree": "/repo/.worktrees/node-n1",
                        "branch": "node/n1",
                    },
                }
                self.assertEqual(map_user_status(record), "需要你决定")

    def test_the_action_names_the_command(self):
        self.assertEqual(
            worktree_action(self.blocked_node()),
            "git worktree add /repo/.worktrees/node-n1 -b node/n1 <base>",
        )
        self.assertIsNone(worktree_action({"status": "blocked_unknown"}))


class FakeLauncher:
    def __init__(self, report):
        self.worktree_preflight = report


class FakeMonitor:
    def __init__(self):
        self.records = []

    def _record(self, snapshot, event, data):
        self.records.append((event, dict(data)))


class FakeSnapshot:
    def __init__(self, nodes):
        self.run_id = "run-235"
        self.nodes = nodes


class RecorderTest(unittest.TestCase):
    """The launcher cannot record events; the CLI writes what it left behind."""

    def run_recorder(self, report, nodes):
        monitor = FakeMonitor()
        snapshot = FakeSnapshot(nodes)
        saved = []
        with mock.patch.object(cli, "save_snapshot", lambda paths, snap: saved.append(snap)):
            changed = cli._record_worktree_preflight(Path("/repo"), monitor, snapshot, [("node-n1", FakeLauncher(report))])
        return changed, monitor.records, snapshot, saved

    def test_a_created_worktree_is_recorded_and_clears_a_stale_marker(self):
        report = {
            "event": WORKTREE_CREATED_EVENT,
            "worktree": "/repo/.worktrees/node-n1",
            "branch": "node/n1",
            "worktree_base": "a" * 40,
        }
        node = {WORKTREE_PREFLIGHT_KEY: {"reason_code": WORKTREE_MISSING}, "status": "running"}
        changed, records, snapshot, saved = self.run_recorder(report, {"node-n1": node})
        self.assertTrue(changed)
        self.assertEqual(len(records), 1)
        event, data = records[0]
        self.assertEqual(event, WORKTREE_CREATED_EVENT)
        self.assertEqual(data["node_id"], "node-n1")
        self.assertEqual(data["worktree_base"], "a" * 40)
        self.assertNotIn(WORKTREE_PREFLIGHT_KEY, snapshot.nodes["node-n1"])
        self.assertEqual(len(saved), 1)

    def test_a_blocked_worktree_is_persisted_with_its_command(self):
        report = {
            "event": WORKTREE_BLOCKED_EVENT,
            "reason_code": WORKTREE_MISSING,
            "worktree": "/repo/.worktrees/node-n1",
            "branch": "node/n1",
            "worktree_command": "git worktree add /repo/.worktrees/node-n1 -b node/n1 <base>",
        }
        node = {"status": "blocked_unknown"}
        changed, records, snapshot, saved = self.run_recorder(report, {"node-n1": node})
        self.assertTrue(changed)
        self.assertEqual(records[0][0], WORKTREE_BLOCKED_EVENT)
        self.assertEqual(records[0][1]["reason_code"], WORKTREE_MISSING)
        marker = snapshot.nodes["node-n1"][WORKTREE_PREFLIGHT_KEY]
        self.assertEqual(marker["worktree_command"], report["worktree_command"])
        self.assertEqual(snapshot.nodes["node-n1"]["user_status"], WORKTREE_ACTION_STATUS)
        self.assertEqual(len(saved), 1)

    def test_an_existing_worktree_records_nothing(self):
        report = {"event": None, "worktree": "/repo/.worktrees/node-n1", "branch": "node/n1"}
        changed, records, _snapshot, saved = self.run_recorder(report, {"node-n1": {"status": "running"}})
        self.assertFalse(changed)
        self.assertEqual(records, [])
        self.assertEqual(saved, [])

    def test_an_existing_worktree_clears_a_stale_marker(self):
        # The supervisor ran the command by hand: the directory is there and
        # matches the contract, so the marker must stop asking for it.
        report = {"event": None, "worktree": "/repo/.worktrees/node-n1", "branch": "node/n1"}
        node = {
            "status": "blocked_unknown",
            WORKTREE_PREFLIGHT_KEY: {"reason_code": WORKTREE_MISSING},
        }
        changed, records, snapshot, saved = self.run_recorder(report, {"node-n1": node})
        self.assertTrue(changed)
        self.assertEqual(records, [])
        self.assertNotIn(WORKTREE_PREFLIGHT_KEY, snapshot.nodes["node-n1"])
        self.assertNotEqual(snapshot.nodes["node-n1"]["user_status"], WORKTREE_ACTION_STATUS)
        self.assertEqual(len(saved), 1)

    def test_a_launcher_without_a_report_is_ignored(self):
        monitor = FakeMonitor()
        snapshot = FakeSnapshot({"node-n1": {"status": "running"}})
        with mock.patch.object(cli, "save_snapshot", lambda *a: self.fail("nothing to save")):
            self.assertFalse(
                cli._record_worktree_preflight(
                    Path("/repo"), monitor, snapshot, [("node-n1", object())]
                )
            )
        self.assertEqual(monitor.records, [])


class StatusTextTest(unittest.TestCase):
    def test_status_names_the_command_a_supervisor_has_to_run(self):
        snapshot = FakeSnapshot({
            "node-n1": {
                "status": "blocked_unknown",
                WORKTREE_PREFLIGHT_KEY: {
                    "reason_code": WORKTREE_MISSING,
                    "worktree_command": "git worktree add /repo/.worktrees/node-n1 -b node/n1 <base>",
                },
            },
            "node-n2": {"status": "running", "active_task": {"task_id": UUID}},
        })
        text = cli._render_worktree_actions(snapshot)
        self.assertIn("node-n1", text)
        self.assertIn(WORKTREE_MISSING, text)
        self.assertIn("git worktree add /repo/.worktrees/node-n1 -b node/n1 <base>", text)
        self.assertNotIn("node-n2", text)

    def test_no_marker_means_no_extra_line(self):
        self.assertEqual(cli._render_worktree_actions(FakeSnapshot({"node-n1": {"status": "running"}})), "")


#: A minimal complex node spec for the CLI-level dispatch tests below; the
#: same shape tests/support_v45_authorize.py publishes, with the engineering
#: fields (adapter_id, worktree, branch) left for vibe to derive.
_BG_NODE_SPEC = {
    "title": "后台派发探针",
    "objective": "verified_fact: 验证 monitor/resume 派发时落盘工作树预检结果",
    "project_id": "bgprobe",
    "remote_git_actions": "deny",
    "spec_path": ".vibe/plans/probe-plan/prd.md",
    "integration_contract": {
        "iteration_context": {
            "kind": "iteration",
            "baseline": "verified_fact: 探针项目为带基线的 git 仓库",
            "target": "verified_fact: 事件流出现工作树预检记录且快照 marker 落盘",
        },
        "compatibility_scope": [
            "verified_fact: 仅本探针项目",
            "verified_fact: 不触碰任何既有受管项目",
        ],
        "agentsmd_acceptance_refs": ["docs/probe-acceptance.md"],
        "integration_acceptance_contract": {
            "criteria": "verified_fact: events.jsonl 出现工作树预检事件",
            "observation": "verified_fact: 读取 run 事件日志",
        },
        "unverified_or_excluded": [
            "verified_fact: 部署与发布不在范围",
            "verified_fact: 生产写入、凭据与外部通信不在范围",
        ],
    },
    "decisions": [
        {
            "question": "如何验证后台派发留证",
            "options": ["实测 monitor 派发后读事件流", "只读代码"],
            "impact": "verified_fact: 决定是否产生可观测的预检事件证据",
            "recommendation": "实测 monitor 派发后读事件流",
            "status": "approved",
            "selected": "实测 monitor 派发后读事件流",
            "field": "verification_method",
            "revision": 1,
        }
    ],
    "nodes": [
        {
            "id": "probe-node-a",
            "title": "探针节点 A",
            "depends_on": [],
            "integration_after": [],
            "parallel_group": "probe-group",
            "contract": {
                "input": "verified_fact: 带基线的仓库",
                "output": "verified_fact: 后台会话在新建工作树中启动",
                "error_behavior": "保留 blocked_unknown 并提供可恢复状态",
                "acceptance_example": "verified_fact: 会话记录与预检事件同时存在",
                "files": ["probe.txt"],
            },
        }
    ],
}


class CliBackgroundDispatchTest(unittest.TestCase):
    """``vibe monitor`` / ``vibe resume`` persist the worktree preflight (#235).

    The recorder is reachable only through those two commands -- the launcher
    has no run identity -- so a typo at a call site (wrong snapshot, launchers
    never passed) must fail here and not only against a FakeMonitor.  The
    dispatch below is the production path end to end: real git, fake claude,
    the real background runner and the real recorder.
    """

    PLAN_ID = "probe-plan"
    NODE_ID = "probe-node-a"
    REQUEST = "设计并实现探针功能，集成测试与部署 workflow pipeline 迁移"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve() / "project"
        self.root.mkdir()
        git(self.root, "init", "-q", "-b", "main")
        git(self.root, "config", "user.email", "dev@example.com")
        git(self.root, "config", "user.name", "Dev")
        (self.root / "file.txt").write_text("baseline\n", encoding="utf-8")
        git(self.root, "add", "file.txt")
        git(self.root, "commit", "-qm", "baseline")
        self.base = git(self.root, "rev-parse", "HEAD").stdout.strip()
        self.slug = node_slug(self.NODE_ID)
        self.worktree = self.root / ".worktrees" / self.slug
        self.branch = "node/" + self.slug
        self.paths = ProjectPaths(self.root)
        self.executor = SplitExecutor()
        self._inject_background_launcher()
        self._publish_plan()

    # -- harness -----------------------------------------------------------
    def _inject_background_launcher(self):
        """The real launcher class over SplitExecutor: real git, fake claude.

        Only the executor seam and the config lookup are replaced; every
        preflight decision below is the production code's own.
        """
        real = importlib.import_module("vibe_guide.adapters.claude_bg")
        executor = self.executor

        class RecordingLauncher(real.ClaudeBgLauncher):
            def __init__(self, routing, config, **kwargs):
                kwargs.pop("executor", None)
                super().__init__(routing, config, executor=executor, **kwargs)

        module = types.ModuleType(real.__name__)
        for name in dir(real):
            if not name.startswith("_"):
                setattr(module, name, getattr(real, name))
        module.ClaudeBgLauncher = RecordingLauncher
        module.load_claude_bg_config = lambda paths: real.ClaudeBgConfig(
            settings_path="/abs/settings.json"
        )
        patch = mock.patch.dict(sys.modules, {real.__name__: module})
        patch.start()
        self.addCleanup(patch.stop)

    def _publish_plan(self):
        initialized = cli.run_cli(["init", "--confirm", "--json"], self.root)
        self.assertEqual(initialized.payload.get("status"), "ok", initialized.payload)
        store = self.root / ".vibe" / "provider-actions"
        store.mkdir(parents=True, exist_ok=True)
        # A verified background launch: the adapter rules background, so the
        # card discloses it and the CLI dispatches through the background
        # runner (the only runner that fills the launchers list).
        (store / "capabilities.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "adapter_id": "claude-code",
                    "facts": {
                        "claude-code.agent": True,
                        "claude-code.shell": True,
                        "claude-code.subprocess": True,
                        "claude-code.worktree": True,
                        "claude-code.background_task.launch": True,
                        "claude-code.visible_task.create": False,
                        "claude-code.visible_task.enter": False,
                        "claude-code.visible_task.resume": False,
                        "claude-code.visible_task.wait": False,
                        "claude-code.in_session_sdd": False,
                    },
                    "provenance": "test fixture: background launch verified in this session",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (self.root / "node-spec.json").write_text(
            json.dumps(_BG_NODE_SPEC, ensure_ascii=False), encoding="utf-8"
        )
        planned = cli.run_cli(
            [
                "plan", "--json", "--request", self.REQUEST, "--s1", "5,5,5,5,5",
                "--plan-id", self.PLAN_ID, "--node-spec", "node-spec.json",
            ],
            self.root,
        )
        self.assertEqual(planned.payload.get("status"), "ok", planned.payload)
        authorized = cli.run_cli(
            [
                "authorize", "--plan", self.PLAN_ID,
                "--authorize", "AUTHORIZE", "--json",
            ],
            self.root,
        )
        self.assertEqual(authorized.payload.get("status"), "ok", authorized.payload)

    def monitor(self):
        return cli.run_cli(
            ["monitor", "--plan", self.PLAN_ID, "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )

    def resume(self, run_id):
        return cli.run_cli(
            ["resume", "--plan", self.PLAN_ID, "--run-id", run_id, "--json"],
            self.root,
        )

    def events(self, run_id):
        return load_events(self.paths, run_id)

    def node(self, run_id):
        return load_snapshot(self.paths, run_id).nodes[self.NODE_ID]

    def session_records(self):
        folder = self.root / ".vibe" / "provider-actions" / "claude-bg-sessions"
        return sorted(folder.glob("*.json")) if folder.is_dir() else []

    def bg_calls(self):
        return [(argv, cwd) for argv, cwd in self.executor.claude_calls if "--bg" in argv]

    # -- the acceptance example -------------------------------------------
    def test_monitor_builds_the_worktree_starts_the_session_and_records_the_creation(self):
        result = self.monitor()
        run_id = result.payload["run_id"]

        # 1. The background session really started: a durable session record
        #    carrying the fake claude's session UUID, and the --bg call ran
        #    inside the directory the preflight had just built.
        records = self.session_records()
        self.assertEqual(len(records), 1, records)
        claims = json.loads(records[0].read_text(encoding="utf-8"))["claims"]
        self.assertEqual(claims["session_id"], UUID)
        self.assertEqual(claims["node_id"], self.NODE_ID)
        self.assertEqual(len(self.bg_calls()), 1)
        self.assertEqual(self.bg_calls()[0][1].resolve(), self.worktree)
        self.assertTrue(self.worktree.is_dir())
        self.assertEqual(self.branch_of(self.worktree), self.branch)
        self.assertEqual(self.head_of(self.worktree), self.base)

        # 2. The event stream carries the creation with its key fields.
        created = [e for e in self.events(run_id) if e["event"] == WORKTREE_CREATED_EVENT]
        self.assertEqual(len(created), 1)
        data = created[0]["data"]
        self.assertEqual(data["node_id"], self.NODE_ID)
        self.assertEqual(Path(data["worktree"]), self.worktree)
        self.assertEqual(data["branch"], self.branch)
        self.assertEqual(data["worktree_base"], self.base)

        # 3. The persisted node record was rewritten by the recorder: no
        #    marker (nothing is blocked) and the status recomputed, not stale.
        node = self.node(run_id)
        self.assertNotIn(WORKTREE_PREFLIGHT_KEY, node)
        self.assertNotEqual(node["user_status"], WORKTREE_ACTION_STATUS)
        self.assertTrue(node.get("active_task"))

    def test_monitor_persists_a_blocked_worktree_marker_and_recomputes_the_status(self):
        # The branch is already there, so the launcher refuses instead of
        # inventing a second writer.
        git(self.root, "branch", self.branch)
        result = self.monitor()
        run_id = result.payload["run_id"]

        # Not created is not started: the block lands before any claude call.
        self.assertEqual(self.session_records(), [])
        self.assertEqual(self.bg_calls(), [])
        self.assertFalse(self.worktree.exists())

        blocked = [e for e in self.events(run_id) if e["event"] == WORKTREE_BLOCKED_EVENT]
        self.assertEqual(len(blocked), 1)
        data = blocked[0]["data"]
        self.assertEqual(data["reason_code"], WORKTREE_MISSING)
        self.assertEqual(data["node_id"], self.NODE_ID)
        self.assertEqual(data["branch"], self.branch)
        self.assertEqual(
            data["worktree_command"],
            "git worktree add %s %s" % (self.worktree, self.branch),
        )

        # The marker is on disk, and the node no longer reads as self-healing.
        node = self.node(run_id)
        marker = node[WORKTREE_PREFLIGHT_KEY]
        self.assertEqual(marker["reason_code"], WORKTREE_MISSING)
        self.assertEqual(marker["worktree_command"], data["worktree_command"])
        self.assertEqual(node["user_status"], WORKTREE_ACTION_STATUS)
        self.assertEqual(node["status"], "blocked_unknown")

    def test_resume_after_the_supervisor_built_the_worktree_clears_the_marker(self):
        git(self.root, "branch", self.branch)
        blocked = self.monitor()
        run_id = blocked.payload["run_id"]
        self.assertEqual(self.node(run_id)["user_status"], WORKTREE_ACTION_STATUS)

        # The supervisor runs exactly the command the marker named.
        git(self.root, "worktree", "add", str(self.worktree), self.branch)
        result = self.resume(run_id)
        self.assertEqual(result.payload.get("run_id"), run_id)

        # The retry dispatched through the same recorder: the session is up
        # and the stale marker no longer claims an action.
        records = self.session_records()
        self.assertEqual(len(records), 1, records)
        claims = json.loads(records[0].read_text(encoding="utf-8"))["claims"]
        self.assertEqual(claims["session_id"], UUID)
        self.assertEqual(len(self.bg_calls()), 1)
        node = self.node(run_id)
        self.assertNotIn(WORKTREE_PREFLIGHT_KEY, node)
        self.assertNotEqual(node["user_status"], WORKTREE_ACTION_STATUS)

    # -- helpers -----------------------------------------------------------
    def branch_of(self, path):
        return git(path, "symbolic-ref", "--short", "-q", "HEAD", check=False).stdout.strip()

    def head_of(self, path):
        return git(path, "rev-parse", "HEAD").stdout.strip()


class CrashWindowReplayTest(unittest.TestCase):
    """A crash between the event append and the snapshot save (#235 P2).

    The CLI appends ``worktree_created`` / ``worktree_preflight_blocked`` and
    only then saves the snapshot.  A crash in between leaves the event
    unapplied, and before this twin the fall-through ``else`` parked the node
    as ``blocked_unknown`` "unapplied event needs manual reconciliation" --
    the silent 自动修复中 the #235 marker exists to replace, resurrected by
    the very crash the marker was meant to survive.

    The replay is driven through the real ``Monitor`` method with a stub
    ``self``: the branch under test touches no monitor state, and building a
    whole authorized plan to reach it would test the plan, not the branch.
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / ".vibe").mkdir()
        self.paths = ProjectPaths(self.root)
        self.run_id = "run-235"
        self.authorization_digest = "a" * 64
        self.node_contract_digest = "b" * 64

    def _node(self):
        return {
            "status": "blocked_unknown",
            "reason": "[REDACTED_PROVIDER_TEXT]",
            "active_task": None,
        }

    def _snapshot(self):
        class _Snap:
            pass

        snap = _Snap()
        snap.run_id = self.run_id
        snap.authorization_digest = self.authorization_digest
        snap.node_contract_digest = self.node_contract_digest
        snap.event_sequence = 0
        snap.nodes = {"node-n1": self._node()}
        snap.handles = {}
        return snap

    class _Stub:
        """Only ``paths`` is read on the path under test."""

        def __init__(self, paths):
            self.paths = paths

    def _replay(self, event, data):
        from vibe_guide.monitor import Monitor

        append_event(
            self.paths,
            RunEvent(event, dict(data, run_id=self.run_id)),
            provenance={
                "role": "system",
                "authorization_digest": self.authorization_digest,
                "node_contract_digest": self.node_contract_digest,
            },
        )
        snapshot = self._snapshot()
        Monitor._reconcile_unapplied_events(self._Stub(self.paths), snapshot)
        return snapshot.nodes["node-n1"]

    def test_blocked_event_restores_the_marker_and_the_label(self):
        node = self._replay(WORKTREE_BLOCKED_EVENT, {
            "node_id": "node-n1",
            "reason_code": WORKTREE_MISSING,
            "worktree": "/repo/.worktrees/node-n1",
            "branch": "node/n1",
            "worktree_base": "a" * 40,
            "worktree_command": "git worktree add /repo/.worktrees/node-n1 -b node/n1 " + "a" * 40,
        })
        # The marker comes back whole ...
        self.assertEqual(node[WORKTREE_PREFLIGHT_KEY]["reason_code"], WORKTREE_MISSING)
        self.assertEqual(node[WORKTREE_PREFLIGHT_KEY]["worktree"], "/repo/.worktrees/node-n1")
        self.assertEqual(node[WORKTREE_PREFLIGHT_KEY]["branch"], "node/n1")
        self.assertEqual(node[WORKTREE_PREFLIGHT_KEY]["worktree_base"], "a" * 40)
        self.assertIn("git worktree add", node[WORKTREE_PREFLIGHT_KEY]["worktree_command"])
        # ... the label is recomputed from it, not stored twice ...
        self.assertEqual(node["user_status"], WORKTREE_ACTION_STATUS)
        # ... and the node is NOT parked as "needs manual reconciliation",
        # which is the whole defect: the supervisor would read 自动修复中 and
        # wait for an external action that never comes.
        self.assertNotEqual(
            node.get("reason"), "unapplied event needs manual reconciliation"
        )
        self.assertEqual(worktree_reason_code(node), WORKTREE_MISSING)

    def test_created_event_clears_the_marker(self):
        node = self._replay(WORKTREE_CREATED_EVENT, {
            "node_id": "node-n1",
            "worktree": "/repo/.worktrees/node-n1",
            "branch": "node/n1",
            "worktree_base": "a" * 40,
        })
        self.assertNotIn(WORKTREE_PREFLIGHT_KEY, node)
        self.assertNotEqual(node["user_status"], WORKTREE_ACTION_STATUS)
        self.assertIsNone(worktree_reason_code(node))

    def test_replay_is_idempotent(self):
        # Two crash windows from the same durable event: the second replay
        # starts from the same sequence position, so the marker it rebuilds
        # must be byte-identical to the first one's.
        data = {
            "node_id": "node-n1",
            "reason_code": WORKTREE_MISSING,
            "worktree": "/repo/.worktrees/node-n1",
            "branch": "node/n1",
        }
        append_event(
            self.paths,
            RunEvent(WORKTREE_BLOCKED_EVENT, dict(data, run_id=self.run_id)),
            provenance={
                "role": "system",
                "authorization_digest": self.authorization_digest,
                "node_contract_digest": self.node_contract_digest,
            },
        )
        from vibe_guide.monitor import Monitor

        first = self._snapshot()
        Monitor._reconcile_unapplied_events(self._Stub(self.paths), first)
        again = self._snapshot()
        Monitor._reconcile_unapplied_events(self._Stub(self.paths), again)
        self.assertEqual(
            first.nodes["node-n1"][WORKTREE_PREFLIGHT_KEY],
            again.nodes["node-n1"][WORKTREE_PREFLIGHT_KEY],
        )
        self.assertEqual(
            first.nodes["node-n1"]["user_status"],
            again.nodes["node-n1"]["user_status"],
        )

    def test_a_consumed_event_is_not_replayed_twice(self):
        # Advancing the sequence is what makes replay safe to re-run: once the
        # event is applied, the next pass must see an empty pending set.
        data = {"node_id": "node-n1", "reason_code": WORKTREE_MISSING}
        append_event(
            self.paths,
            RunEvent(WORKTREE_BLOCKED_EVENT, dict(data, run_id=self.run_id)),
            provenance={
                "role": "system",
                "authorization_digest": self.authorization_digest,
                "node_contract_digest": self.node_contract_digest,
            },
        )
        from vibe_guide.monitor import Monitor

        first = self._snapshot()
        Monitor._reconcile_unapplied_events(self._Stub(self.paths), first)
        second = self._snapshot()
        second.event_sequence = first.event_sequence
        Monitor._reconcile_unapplied_events(self._Stub(self.paths), second)
        self.assertEqual(second.event_sequence, first.event_sequence)
        self.assertNotIn(WORKTREE_PREFLIGHT_KEY, second.nodes["node-n1"])

    def test_malformed_event_data_fails_closed(self):
        from vibe_guide.monitor import Monitor

        append_event(
            self.paths,
            RunEvent(WORKTREE_BLOCKED_EVENT, {"run_id": self.run_id, "node_id": "node-n1"}),
            provenance={
                "role": "system",
                "authorization_digest": self.authorization_digest,
                "node_contract_digest": self.node_contract_digest,
            },
        )
        snapshot = self._snapshot()
        with self.assertRaises(ValueError):
            Monitor._reconcile_unapplied_events(self._Stub(self.paths), snapshot)

    def test_non_system_provenance_is_refused(self):
        from vibe_guide.monitor import Monitor

        append_event(
            self.paths,
            RunEvent(WORKTREE_BLOCKED_EVENT, {
                "run_id": self.run_id, "node_id": "node-n1",
                "reason_code": WORKTREE_MISSING,
            }),
            provenance={
                "role": "developer",
                "authorization_digest": self.authorization_digest,
                "node_contract_digest": self.node_contract_digest,
            },
        )
        snapshot = self._snapshot()
        with self.assertRaises(ValueError):
            Monitor._reconcile_unapplied_events(self._Stub(self.paths), snapshot)



if __name__ == "__main__":
    unittest.main()
