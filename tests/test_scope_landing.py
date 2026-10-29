"""A developer delivery that landed outside its file scope is checked, not waved through.

Recorded failure (run-5e9174c5, 2026-10-28): ``supervisor-runtime`` changed
``vibe_guide/cli.py`` and ``cli-publish`` changed ``protocols/prd-guide.md``,
both outside their nodes' files; vibe accepted both deliveries and
``vibe status`` listed no scope expansion for either.
"""
import json
import subprocess
import unittest
from pathlib import Path

from vibe_guide import scope_landing, worker_questions
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import _snapshot_result
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.state import load_events, load_snapshot

# Imported as modules: a test class bound at module level would run again here.
from tests import test_claude_bg_delivery_gate as _gate
from tests.test_claude_bg_reconnect import GOOD, FakeLauncher


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


class LandingTests(unittest.TestCase):
    tearDown = _gate.BackgroundDeliveryGateTests.tearDown
    _engine_plan = _gate.BackgroundDeliveryGateTests._engine_plan
    monitor = _gate.BackgroundDeliveryGateTests.monitor
    generation = _gate.BackgroundDeliveryGateTests.generation

    def setUp(self):
        _gate.BackgroundDeliveryGateTests.setUp(self)
        root = self.paths.root
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.email", "dev@example.com")
        git(root, "config", "user.name", "Dev")
        (root / ".gitignore").write_text(".vibe/\n.worktrees/\n", encoding="utf-8")
        (root / "bg.py").write_text("base\n", encoding="utf-8")
        git(root, "add", ".gitignore", "bg.py")
        git(root, "commit", "-qm", "baseline")
        self.snapshot = self.monitor().start(self.record, self.runner())
        for node_id in ("bg", "bg2"):
            node = self.snapshot.nodes[node_id]
            git(root, "worktree", "add", "-q", node["worktree"], "-b", node["branch"])

    def runner(self):
        return ClaudeBgRunner(lambda c, w: FakeLauncher("working"), paths=self.paths)

    def land(self, node_id, files):
        worktree = self.paths.root / self.snapshot.nodes[node_id]["worktree"]
        for name, text in files.items():
            (worktree / name).parent.mkdir(parents=True, exist_ok=True)
            (worktree / name).write_text(text, encoding="utf-8")
            git(worktree, "add", name)
        git(worktree, "commit", "-qm", "work")

    def deliver(self, node_id="bg"):
        ProviderActionStore(self.paths).record_worker_delivery(
            self.snapshot.run_id, node_id, "developer", dict(GOOD),
            self.generation(self.snapshot, node_id))
        return self.monitor().resume(self.snapshot.run_id, self.runner())

    def events(self, name):
        return [record for record in load_events(self.paths, self.snapshot.run_id)
                if record["event"] == name]

    def test_in_scope_delivery_goes_to_review_unchanged(self):
        self.land("bg", {"bg.py": "work\n"})
        snapshot = self.deliver()
        self.assertEqual(snapshot.nodes["bg"]["status"], "review", snapshot.nodes["bg"].get("reason"))
        self.assertEqual(snapshot.nodes["bg"].get("scope_expansions", []), [])
        self.assertEqual(self.events("scope_auto_expanded"), [])

    def test_unauthorized_source_file_is_rejected_with_its_name(self):
        self.land("bg", {"bg.py": "work\n", "vibe_guide/cli.py": "edited\n"})
        snapshot = self.deliver()

        node = snapshot.nodes["bg"]
        self.assertNotIn(node["status"], {"review", "delivered", "accepted"})
        self.assertEqual(node["status"], worker_questions.AWAITING_STATUS)
        self.assertEqual(node["scope_landing_rejection"],
                         [{"path": "vibe_guide/cli.py", "cause": scope_landing.CAUSE_NOT_EXPANDABLE}])
        self.assertEqual(self.events("delivered"), [])
        [rejected] = self.events("scope_landing_rejected")
        self.assertIn("vibe_guide/cli.py", json.dumps(rejected, ensure_ascii=False))
        # The supervisor sees the readable reason in the beat and in status.
        [question] = worker_questions.pending_questions(self.paths, snapshot)
        self.assertIn("vibe_guide/cli.py", question["excerpt"])
        status = _snapshot_result("status", load_snapshot(self.paths, snapshot.run_id), False,
                                  paths=self.paths)
        self.assertIn("vibe_guide/cli.py", status.text)

    def test_new_test_file_is_auto_expanded_and_visible_in_status(self):
        self.land("bg", {"bg.py": "work\n", "tests/test_x.py": "def test(): pass\n"})
        snapshot = self.deliver()

        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "review", node.get("reason"))
        self.assertEqual(node["scope_expansions"], ["tests/test_x.py"])
        [expanded] = self.events("scope_auto_expanded")
        self.assertEqual(expanded["data"]["files"], ["tests/test_x.py"])
        self.assertEqual(expanded["data"]["scope_rules"],
                         [{"path": "tests/test_x.py", "rule": "tests_dir"}])
        status = _snapshot_result("status", load_snapshot(self.paths, snapshot.run_id), True,
                                  paths=self.paths)
        self.assertEqual(status.payload["nodes"]["bg"]["scope_expansions"], ["tests/test_x.py"])
        self.assertIn("tests/test_x.py", status.text)

    def stage_only(self, node_id, files):
        worktree = self.paths.root / self.snapshot.nodes[node_id]["worktree"]
        for name, text in files.items():
            (worktree / name).write_text(text, encoding="utf-8")
            git(worktree, "add", name)

    def test_staged_but_uncommitted_delivery_is_refused_with_its_name(self):
        # Rehearsal run-01202945: usage-docs left docs/usage.md staged and
        # uncommitted, vibe accepted it, and the integration baseline never
        # contained the file.
        self.stage_only("bg", {"bg.py": "work\n"})
        snapshot = self.deliver()

        node = snapshot.nodes["bg"]
        self.assertNotIn(node["status"], {"review", "delivered", "accepted"})
        self.assertEqual(node["status"], worker_questions.AWAITING_STATUS)
        self.assertEqual(node["scope_landing_rejection"],
                         [{"path": "bg.py", "cause": scope_landing.CAUSE_UNCOMMITTED}])
        self.assertEqual(self.events("delivered"), [])
        [question] = worker_questions.pending_questions(self.paths, snapshot)
        self.assertIn("bg.py", question["excerpt"])
        self.assertIn("commit", question["excerpt"])

    def test_unstaged_edit_of_a_tracked_file_is_refused_too(self):
        worktree = self.paths.root / self.snapshot.nodes["bg"]["worktree"]
        (worktree / "bg.py").write_text("edited, never added\n", encoding="utf-8")
        snapshot = self.deliver()
        self.assertEqual(snapshot.nodes["bg"]["scope_landing_rejection"],
                         [{"path": "bg.py", "cause": scope_landing.CAUSE_UNCOMMITTED}])

    def test_untracked_marker_file_does_not_block_a_committed_delivery(self):
        self.land("bg", {"bg.py": "work\n"})
        worktree = self.paths.root / self.snapshot.nodes["bg"]["worktree"]
        (worktree / "delivery.md").write_text("done\n", encoding="utf-8")
        snapshot = self.deliver()
        self.assertEqual(snapshot.nodes["bg"]["status"], "review",
                         snapshot.nodes["bg"].get("reason"))

    def test_a_file_held_by_another_active_node_is_rejected(self):
        self.land("bg", {"bg.py": "work\n", "bg2.py": "stolen\n"})
        snapshot = self.deliver()
        self.assertEqual(snapshot.nodes["bg"]["scope_landing_rejection"],
                         [{"path": "bg2.py", "cause": scope_landing.CAUSE_HELD}])

    def test_a_refused_delivery_keeps_a_delivered_binding_not_a_claimed_stop(self):
        from vibe_guide.task_registry import load_task_binding

        self.land("bg", {"bg.py": "work\n", "vibe_guide/cli.py": "edited\n"})
        snapshot = self.deliver()
        binding = load_task_binding(self.paths, "bg", "developer", run_id=snapshot.run_id)
        self.assertEqual(binding.status, "delivered")

    def test_main_merged_into_the_node_branch_is_not_the_nodes_landing(self):
        root = self.paths.root
        (root / "docs").mkdir(exist_ok=True)
        (root / "docs" / "main-only.md").write_text("main moved on\n", encoding="utf-8")
        git(root, "add", "docs/main-only.md")
        git(root, "commit", "-qm", "main moves")
        # vibe built the worktree, so the base is recorded.
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        snapshot.nodes["bg"]["worktree_base"] = git(root, "rev-parse", "HEAD~1")
        self.monitor()._save_snapshot(snapshot)
        self.land("bg", {"bg.py": "work\n"})
        git(root / ".worktrees" / "bg", "merge", "-q", "--no-edit", "main")

        snapshot = self.deliver()

        self.assertEqual(snapshot.nodes["bg"]["status"], "review", snapshot.nodes["bg"].get("reason"))

    def test_configured_mechanical_path_is_auto_expanded(self):
        (self.paths.vibe / "config.json").write_text(
            json.dumps({"auto_scope_paths": ["docs/notes.md"]}), encoding="utf-8")
        self.land("bg", {"bg.py": "work\n", "docs/notes.md": "note\n"})
        snapshot = self.deliver()
        self.assertEqual(snapshot.nodes["bg"]["scope_expansions"], ["docs/notes.md"])

    def test_approved_acceptance_rule_expands_a_low_risk_source_file(self):
        (self.paths.vibe / "config.json").write_text(
            json.dumps({"acceptance_scope_rule_approved": True}), encoding="utf-8")
        self.land("bg", {"bg.py": "work\n", "vibe_guide/cli.py": "edited\n",
                         "requirements.txt": "requests\n"})
        snapshot = self.deliver()
        # A dependency manifest outside tests/ never auto-expands.
        self.assertEqual(snapshot.nodes["bg"]["scope_landing_rejection"],
                         [{"path": "requirements.txt", "cause": scope_landing.CAUSE_NOT_EXPANDABLE}])

        self.land("bg", {"requirements.txt": ""})
        git(self.paths.root / ".worktrees" / "bg", "rm", "-q", "requirements.txt")
        git(self.paths.root / ".worktrees" / "bg", "commit", "-qm", "drop manifest")
        result = scope_landing.landing_report(self.monitor(), load_snapshot(
            self.paths, self.snapshot.run_id), "bg")
        self.assertEqual(result["rejected"], [])
        self.assertEqual(result["expanded"],
                         [{"path": "vibe_guide/cli.py", "rule": "acceptance_unchanged"}])


class LandedFilesTest(unittest.TestCase):
    """Upstream content merged into a node branch is not the node's own landing."""

    def setUp(self):
        import tempfile

        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "config", "user.email", "dev@example.com")
        git(self.repo, "config", "user.name", "Dev")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        git(self.repo, "add", "base.txt")
        git(self.repo, "commit", "-qm", "base")
        self.base = git(self.repo, "rev-parse", "HEAD")

    def commit(self, name, text):
        (self.repo / name).write_text(text, encoding="utf-8")
        git(self.repo, "add", name)
        git(self.repo, "commit", "-qm", name)

    def runner(self, args):
        return subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True)

    def test_merged_upstream_files_are_excluded_but_own_edits_on_them_are_not(self):
        git(self.repo, "checkout", "-q", "-b", "node/up")
        self.commit("up.txt", "upstream\n")
        self.commit("shared.txt", "upstream\n")
        git(self.repo, "checkout", "-q", "-b", "node/down", "main")
        git(self.repo, "merge", "-q", "--no-edit", "node/up")
        self.commit("own.txt", "mine\n")
        self.commit("shared.txt", "downstream edit\n")

        files = scope_landing.landed_files(self.runner, self.base, "HEAD", ["refs/heads/node/up"])

        self.assertEqual(files, ["own.txt", "shared.txt"])

    def test_glob_scope_entries_stay_inside_one_directory(self):
        self.assertTrue(scope_landing._inside("src/a.py", ["src/*.py"]))
        self.assertFalse(scope_landing._inside("src/deep/a.py", ["src/*.py"]))
        self.assertTrue(scope_landing._inside("src/deep/a.py", ["src/**/*.py"]))
        self.assertTrue(scope_landing._inside("src/deep/a.py", ["src"]))
        self.assertFalse(scope_landing._inside("srcx/a.py", ["src"]))


if __name__ == "__main__":
    unittest.main()
