"""The node baseline has one source, and preset baseline commits are not the worker's work.

A node branch can carry baseline commits vibe composed in before the worker
started (main plus accepted upstreams) that touch other nodes' files.  The
delivery checks must start counting at the node baseline: recorded when
there is one, derived from the composed commit otherwise, and -- only when
nothing better exists -- the merge-base, which says so in every refusal.
"""
import json
import unittest

from vibe_guide import scope_landing
from vibe_guide.cli import _record_composed_bases
from vibe_guide.state import load_events, load_snapshot, save_snapshot
from vibe_guide.worktree_base import BASELINE_AUTHOR_EMAIL

# Imported as a module: a test class bound at module level would run again here.
from tests import test_scope_landing as _landing

git = _landing.git


class LandingBaseUnifiedTests(_landing.LandingTests):
    # Reuses the scaffold; the inherited cases are blanked below (they run in test_scope_landing).

    def preset(self, node_id, files, vibe=True):
        """Commit ``files`` on the node branch before the worker starts."""
        worktree = self.paths.root / self.snapshot.nodes[node_id]["worktree"]
        author = ["-c", "user.name=vibe-guide", "-c", "user.email=" + BASELINE_AUTHOR_EMAIL] if vibe else []
        for index, (name, text) in enumerate(sorted(files.items())):
            (worktree / name).parent.mkdir(parents=True, exist_ok=True)
            (worktree / name).write_text(text, encoding="utf-8")
            git(worktree, "add", name)
            git(worktree, *author, "commit", "-qm", "preset %d" % index)
        return git(worktree, "rev-parse", "HEAD")

    def record_base(self, node_id, sha):
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        snapshot.nodes[node_id]["worktree_base"] = sha
        save_snapshot(self.paths, snapshot)
        self.snapshot = snapshot

    def report(self, node_id="bg"):
        return scope_landing.landing_report(
            self.monitor(), load_snapshot(self.paths, self.snapshot.run_id), node_id)

    PRESET = {"bg2.py": "other\n", "vibe_guide/cli.py": "other\n", "docs/other.md": "other\n"}

    def test_recorded_base_ignores_three_preset_commits_on_others_files(self):
        tip = self.preset("bg", self.PRESET, vibe=False)
        self.record_base("bg", tip)
        self.land("bg", {"bg.py": "work\n"})
        snapshot = self.deliver()
        self.assertEqual(snapshot.nodes["bg"]["status"], "review", snapshot.nodes["bg"].get("reason"))
        self.assertEqual(self.events("scope_auto_expanded"), [])
        self.assertFalse(self.report()["base_inferred"])

    def test_unrecorded_base_finds_the_composed_baseline_commit(self):
        self.preset("bg", self.PRESET, vibe=True)
        self.land("bg", {"bg.py": "work\n"})
        snapshot = self.deliver()
        self.assertEqual(snapshot.nodes["bg"]["status"], "review", snapshot.nodes["bg"].get("reason"))
        self.assertEqual(self.events("scope_auto_expanded"), [])

    def test_real_edit_of_another_nodes_file_names_the_commit_that_introduced_it(self):
        tip = self.preset("bg", {"docs/other.md": "other\n"}, vibe=False)
        self.record_base("bg", tip)
        self.land("bg", {"bg.py": "work\n", "bg2.py": "stolen\n"})
        introduced = git(self.paths.root / self.snapshot.nodes["bg"]["worktree"],
                         "log", "-1", "--format=%h", "--", "bg2.py")
        snapshot = self.deliver()
        node = snapshot.nodes["bg"]
        self.assertEqual(node["scope_landing_rejection"],
                         [{"path": "bg2.py", "cause": scope_landing.CAUSE_HELD}])
        from vibe_guide import worker_questions
        [question] = worker_questions.pending_questions(self.paths, snapshot)
        self.assertIn(introduced, question["excerpt"])
        self.assertNotIn("推算", question["excerpt"])
        [rejected] = self.events("scope_landing_rejected")
        self.assertEqual(rejected["data"]["commits"], ["bg2.py:" + introduced])
        self.assertFalse(rejected["data"]["base_inferred"])

    def test_fallback_base_says_so_in_refusal_and_event(self):
        # Preset by someone else, nothing recorded: only the merge-base is left.
        self.preset("bg", {"docs/other.md": "other\n"}, vibe=False)
        self.land("bg", {"bg.py": "work\n"})
        snapshot = self.deliver()
        node = snapshot.nodes["bg"]
        self.assertEqual([item["path"] for item in node["scope_landing_rejection"]], ["docs/other.md"])
        from vibe_guide import worker_questions
        [question] = worker_questions.pending_questions(self.paths, snapshot)
        self.assertIn("推算", question["excerpt"])
        [rejected] = self.events("scope_landing_rejected")
        self.assertTrue(rejected["data"]["base_inferred"])
        self.assertEqual(len(rejected["data"]["commits"]), 1)

    def test_fallback_base_marks_an_auto_expansion_as_inferred(self):
        self.land("bg", {"bg.py": "work\n", "tests/test_y.py": "def test(): pass\n"})
        self.deliver()
        [expanded] = self.events("scope_auto_expanded")
        [rule] = expanded["data"]["scope_rules"]
        self.assertEqual(rule["base"], scope_landing.BASE_INFERRED)
        self.assertTrue(rule["commits"])

    def test_recorded_base_expansion_carries_no_inferred_mark(self):
        tip = git(self.paths.root / self.snapshot.nodes["bg"]["worktree"], "rev-parse", "HEAD")
        self.record_base("bg", tip)
        self.land("bg", {"bg.py": "work\n", "tests/test_y.py": "def test(): pass\n"})
        self.deliver()
        [expanded] = self.events("scope_auto_expanded")
        self.assertEqual(expanded["data"]["scope_rules"],
                         [{"path": "tests/test_y.py", "rule": "tests_dir"}])

    # -- empty landing under a derived or inferred baseline ----------------
    def test_composed_base_keeps_a_truly_empty_delivery_empty(self):
        self.preset("bg", self.PRESET, vibe=True)
        self.assertEqual(self.report()["files"], [])   # not the three preset commits

    def test_composed_base_still_sees_the_workers_real_change(self):
        self.preset("bg", self.PRESET, vibe=True)
        self.land("bg", {"bg.py": "work\n"})
        self.assertEqual(self.report()["files"], ["bg.py"])

    def test_inferred_base_empty_delivery_is_empty_and_change_is_not(self):
        self.assertEqual(self.report()["files"], [])
        self.assertTrue(self.report()["base_inferred"])
        self.land("bg", {"bg.py": "work\n"})
        self.assertEqual(self.report()["files"], ["bg.py"])

    def test_empty_delivery_under_inferred_base_is_refused_as_empty_landing(self):
        snapshot = self.deliver()
        self.assertEqual(self.events("delivered"), [])
        self.assertNotEqual(snapshot.nodes["bg"]["status"], "review")
        self.assertIn(scope_landing.EMPTY_LANDING_CODE, json.dumps(
            [record["data"] for record in load_events(self.paths, snapshot.run_id)]))

    # -- one getter ---------------------------------------------------------
    def test_recorder_stores_the_composed_base_for_a_dispatched_node(self):
        tip = self.preset("bg", self.PRESET, vibe=True)
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        snapshot.nodes["bg"]["active_role"] = "developer"
        self.assertTrue(_record_composed_bases(self.paths, self.monitor(), snapshot))
        self.assertEqual(snapshot.nodes["bg"]["worktree_base"], tip)
        self.assertNotIn("worktree_base", snapshot.nodes["bg2"])

    def test_recorder_leaves_a_node_without_composed_commit_to_inference(self):
        self.preset("bg", self.PRESET, vibe=False)
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        snapshot.nodes["bg"]["active_role"] = "developer"
        self.assertFalse(_record_composed_bases(self.paths, self.monitor(), snapshot))
        self.assertNotIn("worktree_base", snapshot.nodes["bg"])

    def test_node_base_prefers_a_recorded_commit_and_ignores_a_stale_one(self):
        worktree = self.paths.root / self.snapshot.nodes["bg"]["worktree"]
        run = scope_landing._git_in(worktree)
        head = git(worktree, "rev-parse", "HEAD")
        self.assertEqual(scope_landing.node_base(run, head, head), (head, False))
        base, inferred = scope_landing.node_base(run, "0" * 40, head)
        self.assertTrue(inferred)


# The inherited LandingTests cases are covered by their own module.
for _name in [n for n in dir(_landing.LandingTests) if n.startswith("test_")]:
    setattr(LandingBaseUnifiedTests, _name, None)

if __name__ == "__main__":
    unittest.main()
