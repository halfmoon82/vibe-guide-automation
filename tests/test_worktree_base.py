"""A downstream node starts from its accepted upstream nodes' code.

Recorded failure (run-5e9174c5, 2026-10-28): a node with ``depends_on`` got a
worktree cut from main, so the code of its already-accepted upstream nodes was
not there; every ``worktree_created`` event named main's SHA as the base.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide import cli
from vibe_guide.adapters.claude_bg import (
    WORKTREE_BLOCKED_EVENT,
    WORKTREE_CREATED_EVENT,
    WORKTREE_MERGE_CONFLICT,
    ClaudeBgConfig,
    ClaudeBgLauncher,
    ClaudeBgWorktreeBlocked,
)
from vibe_guide.adapters.task_provider import BackgroundTaskRouting
from vibe_guide.models import DAGNode
from vibe_guide.state import (
    WORKTREE_ACTION_STATUS,
    WORKTREE_PREFLIGHT_KEY,
    WORKTREE_REASON_CODES,
    map_user_status,
    worktree_action,
)
from vibe_guide.worktree_base import upstream_branches
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.task_registry import load_task_binding

# Imported as a module: a test class bound at module level would run again here.
from tests import test_claude_bg_delivery_gate as _gate_module
from tests.test_claude_bg_reconnect import GOOD, FakeLauncher as ReconnectLauncher
from tests.test_claude_bg_reconnect import background_node as _fixture_background_node
from tests.test_worktree_preflight_235 import FakeLauncher, FakeMonitor, FakeSnapshot, RepoCase, git


def commit_on_branch(repo, branch, name, text, start="main"):
    git(repo, "checkout", "-q", "-b", branch, start)
    (repo / name).write_text(text, encoding="utf-8")
    git(repo, "add", name)
    git(repo, "commit", "-qm", "change " + name)
    tip = git(repo, "rev-parse", "HEAD").stdout.strip()
    git(repo, "checkout", "-q", "main")
    return tip


class ComposedBaseTest(RepoCase):
    def launcher(self, upstream=()):
        routing = BackgroundTaskRouting("host", str(self.worktree), self.branch, "s.json", "h.json")
        return ClaudeBgLauncher(routing, ClaudeBgConfig(settings_path="/abs/settings.json"),
                                executor=self.executor, upstream_branches=list(upstream))

    def test_two_accepted_upstream_branches_are_both_in_the_new_worktree(self):
        tip_a = commit_on_branch(self.repo, "node/up-a", "a.txt", "from a\n")
        tip_b = commit_on_branch(self.repo, "node/up-b", "b.txt", "from b\n")

        launcher = self.launcher(["node/up-a", "node/up-b"])
        launcher._prepare_worktree()

        self.assertEqual((self.worktree / "a.txt").read_text(encoding="utf-8"), "from a\n")
        self.assertEqual((self.worktree / "b.txt").read_text(encoding="utf-8"), "from b\n")
        self.assertEqual(self.branch_of(self.worktree), self.branch)
        head = self.head_of(self.worktree)
        report = launcher.worktree_preflight
        self.assertEqual(report["event"], WORKTREE_CREATED_EVENT)
        # The recorded base is the composed baseline, not main.
        self.assertEqual(report["worktree_base"], head)
        self.assertNotEqual(head, self.base)
        for tip in (self.base, tip_a, tip_b):
            self.assertEqual(git(self.repo, "merge-base", "--is-ancestor", tip, head,
                                 check=False).returncode, 0, tip)
        # Composing the base never moves main.
        self.assertEqual(git(self.repo, "rev-parse", "main").stdout.strip(), self.base)

    def test_an_upstream_already_in_main_adds_no_merge(self):
        commit_on_branch(self.repo, "node/up-a", "a.txt", "from a\n")
        git(self.repo, "merge", "-q", "--ff-only", "node/up-a")
        main = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        launcher = self.launcher(["node/up-a"])
        launcher._prepare_worktree()

        self.assertEqual(launcher.worktree_preflight["worktree_base"], main)

    def test_without_upstream_the_base_stays_main(self):
        launcher = self.launcher()
        launcher._prepare_worktree()
        self.assertEqual(launcher.worktree_preflight["worktree_base"], self.base)

    def test_conflicting_upstreams_block_with_the_conflicting_files(self):
        commit_on_branch(self.repo, "node/up-a", "file.txt", "a wins\n")
        commit_on_branch(self.repo, "node/up-b", "file.txt", "b wins\n")

        launcher = self.launcher(["node/up-a", "node/up-b"])
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            launcher._prepare_worktree()

        self.assertEqual(ctx.exception.reason_code, WORKTREE_MERGE_CONFLICT)
        self.assertIn("file.txt", ctx.exception.reason)
        self.assertIn("node/up-b", ctx.exception.reason)
        report = launcher.worktree_preflight
        self.assertEqual(report["event"], WORKTREE_BLOCKED_EVENT)
        self.assertEqual(report["conflict_files"], ["file.txt"])
        self.assertIn("file.txt", report["worktree_command"])
        # Nothing was built: a retry starts from a clean place, not from main.
        self.assertFalse(self.worktree.exists())
        self.assertNotEqual(git(self.repo, "show-ref", "--verify", "--quiet",
                                "refs/heads/" + self.branch, check=False).returncode, 0)

    def test_a_missing_upstream_branch_is_readable_and_builds_nothing(self):
        launcher = self.launcher(["node/never-built"])
        with self.assertRaises(ClaudeBgWorktreeBlocked) as ctx:
            launcher._prepare_worktree()
        self.assertIn("node/never-built", ctx.exception.reason)
        self.assertFalse(self.worktree.exists())


class UpstreamOrderTest(unittest.TestCase):
    def node(self, node_id, depends_on=()):
        return DAGNode(node_id, node_id, list(depends_on), [], "g", {}, "planned")

    def test_accepted_ancestors_in_topological_order(self):
        nodes = {item.id: item for item in (
            self.node("base"), self.node("a", ["base"]), self.node("b"),
            self.node("down", ["b", "a"]), self.node("other"))}
        statuses = {node_id: {"status": "accepted", "branch": "node/" + node_id}
                    for node_id in ("base", "a", "b", "other")}
        statuses["down"] = {"status": "planned", "branch": "node/down"}
        self.assertEqual(upstream_branches(nodes, statuses, "down"),
                         ["node/b", "node/base", "node/a"])

    def test_unaccepted_or_branchless_upstreams_are_skipped(self):
        nodes = {item.id: item for item in (
            self.node("a"), self.node("b"), self.node("down", ["a", "b"]))}
        statuses = {"a": {"status": "running", "branch": "node/a"},
                    "b": {"status": "accepted", "branch": ""},
                    "down": {"status": "planned"}}
        self.assertEqual(upstream_branches(nodes, statuses, "down"), [])


class ConflictPersistenceTest(unittest.TestCase):
    def test_the_conflict_code_is_shared_with_the_state_layer(self):
        self.assertIn(WORKTREE_MERGE_CONFLICT, WORKTREE_REASON_CODES)

    def test_a_conflict_block_is_persisted_and_named_in_status(self):
        report = {
            "event": WORKTREE_BLOCKED_EVENT,
            "reason_code": WORKTREE_MERGE_CONFLICT,
            "worktree": "/repo/.worktrees/node-n1",
            "branch": "node/n1",
            "conflict_files": ["file.txt"],
            "worktree_command": "上游分支 node/up-b 合并冲突（冲突文件：file.txt）",
        }
        monitor = FakeMonitor()
        snapshot = FakeSnapshot({"node-n1": {"status": "blocked_unknown"}})
        with mock.patch.object(cli, "save_snapshot", lambda paths, snap: None):
            cli._record_worktree_preflight(Path("/repo"), monitor, snapshot,
                                           [("node-n1", FakeLauncher(report))])
        self.assertEqual(monitor.records[0][1]["conflict_files"], ["file.txt"])
        node = snapshot.nodes["node-n1"]
        self.assertEqual(node[WORKTREE_PREFLIGHT_KEY]["conflict_files"], ["file.txt"])
        self.assertEqual(map_user_status(node), WORKTREE_ACTION_STATUS)
        self.assertIn("file.txt", worktree_action(node))

    def test_a_created_worktree_keeps_its_base_on_the_node(self):
        report = {"event": WORKTREE_CREATED_EVENT, "worktree": "/repo/.worktrees/node-n1",
                  "branch": "node/n1", "worktree_base": "b" * 40}
        snapshot = FakeSnapshot({"node-n1": {"status": "running"}})
        with mock.patch.object(cli, "save_snapshot", lambda paths, snap: None):
            cli._record_worktree_preflight(Path("/repo"), FakeMonitor(), snapshot,
                                           [("node-n1", FakeLauncher(report))])
        self.assertEqual(snapshot.nodes["node-n1"]["worktree_base"], "b" * 40)


class FactoryWiringTest(unittest.TestCase):
    def test_the_factory_hands_the_contract_upstreams_to_the_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            from vibe_guide.paths import ProjectPaths

            paths = ProjectPaths(Path(directory))
            launchers = []
            with mock.patch("vibe_guide.adapters.claude_bg.load_claude_bg_config",
                            return_value=ClaudeBgConfig(settings_path="/abs/s.json")):
                factory = cli._claude_bg_launcher_factory(paths, launchers)
                launcher = factory({"node_id": "down", "branch": "node/down", "run_id": "r",
                                    "generation": 1, "upstream_branches": ["node/a"]},
                                   Path(directory))
        self.assertEqual(launcher.upstream_branches, ["node/a"])


def _chained_node(node_id="bg"):
    """``bg2`` depends on ``bg``; both otherwise the gated fixture's nodes."""
    node = _fixture_background_node(node_id)
    if node_id == "bg2":
        node.depends_on = ["bg"]
        node.parallel_group = "g2"
    return node


class MonitorUpstreamDispatchTest(unittest.TestCase):
    tearDown = _gate_module.BackgroundDeliveryGateTests.tearDown
    _engine_plan = _gate_module.BackgroundDeliveryGateTests._engine_plan
    monitor = _gate_module.BackgroundDeliveryGateTests.monitor
    generation = _gate_module.BackgroundDeliveryGateTests.generation

    def setUp(self):
        with mock.patch.object(_gate_module, "background_node", _chained_node):
            _gate_module.BackgroundDeliveryGateTests.setUp(self)

    def runner(self, contracts):
        def factory(contract, worktree):
            contracts.append(dict(contract))
            return ReconnectLauncher("working")
        return ClaudeBgRunner(factory, paths=self.paths)

    def test_the_downstream_dispatch_carries_the_accepted_upstream_branch(self):
        contracts = []
        snapshot = self.monitor().start(self.record, self.runner(contracts))
        self.assertEqual(snapshot.nodes["bg2"]["status"], "planned")
        first = [c for c in contracts if c.get("node_id") == "bg" and "upstream_branches" in c]
        self.assertEqual(first[0]["upstream_branches"], [])

        store = ProviderActionStore(self.paths)
        store.record_worker_delivery(snapshot.run_id, "bg", "developer", dict(GOOD),
                                     self.generation(snapshot))
        snapshot = self.monitor().resume(snapshot.run_id, self.runner(contracts))
        reviewer = load_task_binding(self.paths, "bg", "reviewer", run_id=snapshot.run_id)
        store.record_worker_delivery(snapshot.run_id, "bg", "reviewer",
                                     dict(GOOD, event="accepted", evidence="P0-P2 clear"),
                                     reviewer.generation)
        snapshot = self.monitor().resume(snapshot.run_id, self.runner(contracts))
        self.assertEqual(snapshot.nodes["bg"]["status"], "accepted", snapshot.nodes["bg"].get("reason"))
        if snapshot.nodes["bg2"]["status"] == "planned":
            snapshot = self.monitor().resume(snapshot.run_id, self.runner(contracts))

        downstream = [c for c in contracts if c.get("node_id") == "bg2" and "upstream_branches" in c]
        self.assertTrue(downstream, contracts)
        self.assertEqual(downstream[-1]["upstream_branches"], [snapshot.nodes["bg"]["branch"]])


if __name__ == "__main__":
    unittest.main()


class ComposedBaseWithoutGitIdentityTest(RepoCase):
    """Recorded in the 5.2.1 rehearsal: no user.name/user.email anywhere, so
    ``git commit-tree`` died and the downstream node never got a worktree."""

    def setUp(self):
        super().setUp()
        for key in ("user.name", "user.email"):
            git(self.repo, "config", "--unset", key)
        isolated = {
            "HOME": str(self.tmp / "nohome"), "XDG_CONFIG_HOME": str(self.tmp / "nohome"),
            "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
        }
        patcher = mock.patch.dict("os.environ", isolated)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
            clean = mock.patch.dict("os.environ", {}, clear=False)
            clean.start()
            self.addCleanup(clean.stop)
            __import__("os").environ.pop(name, None)

    def launcher(self, upstream=()):
        routing = BackgroundTaskRouting("host", str(self.worktree), self.branch, "s.json", "h.json")
        return ClaudeBgLauncher(routing, ClaudeBgConfig(settings_path="/abs/settings.json"),
                                executor=self.executor, upstream_branches=list(upstream))

    def commit_upstream(self):
        env = {"GIT_AUTHOR_NAME": "Up", "GIT_AUTHOR_EMAIL": "up@example.com",
               "GIT_COMMITTER_NAME": "Up", "GIT_COMMITTER_EMAIL": "up@example.com"}
        with mock.patch.dict("os.environ", env):
            return commit_on_branch(self.repo, "node/up-a", "a.txt", "from a\n")

    def test_the_composed_base_builds_without_a_configured_git_identity(self):
        self.commit_upstream()
        launcher = self.launcher(["node/up-a"])
        launcher._prepare_worktree()
        self.assertEqual((self.worktree / "a.txt").read_text(encoding="utf-8"), "from a\n")
        self.assertEqual(launcher.worktree_preflight["event"], WORKTREE_CREATED_EVENT)

    def test_the_machine_identity_is_not_borrowed_for_the_merge_commit(self):
        self.commit_upstream()
        launcher = self.launcher(["node/up-a"])
        launcher._prepare_worktree()
        head = self.head_of(self.worktree)
        author = git(self.repo, "log", "-1", "--format=%an <%ae>", head).stdout.strip()
        self.assertIn("vibe", author.lower())
