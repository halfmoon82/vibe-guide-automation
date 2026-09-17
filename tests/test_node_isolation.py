"""Contract: each node gets its own worktree and branch, and never main.

`complete_node_contracts` defaulted every node to worktree "." and branch
"main".  A product spec carries no engineering fields by design, so on the
product path every node took those defaults: two parallel developer sessions
were dispatched into the same directory on the trunk.  The writer lease is
keyed per node, so it does not catch the collision, and monitor's own default
(`node/<id>`) never applies because the spec's literal value wins.
"""
import subprocess
import unittest

from vibe_guide.models import node_branch, node_worktree
from vibe_guide.node_spec import complete_node_contracts

# Node ids that differ only in trailing or repeated punctuation.  `_ID` in
# models.py accepts "." and "-" anywhere but the first character, so these are
# all legal ids a product spec may carry.
NEAR_MISS_IDS = ("a", "a.", "x", "x..", "node", "node-", "b", "b--", "A.B..C")


def _nodes(*ids):
    return [{"id": node_id, "title": node_id, "contract": {}} for node_id in ids]


class NodeIsolationTests(unittest.TestCase):
    def test_parallel_nodes_do_not_share_a_worktree(self):
        nodes = _nodes("export-button", "date-range-filter", "integration-review")
        complete_node_contracts(nodes, "claude-code", "proj")
        trees = [node["contract"]["worktree"] for node in nodes]
        self.assertEqual(len(set(trees)), len(trees), trees)

    def test_parallel_nodes_do_not_share_a_branch(self):
        nodes = _nodes("export-button", "date-range-filter")
        complete_node_contracts(nodes, "claude-code", "proj")
        branches = [node["contract"]["branch"] for node in nodes]
        self.assertEqual(len(set(branches)), len(branches), branches)

    def test_no_node_is_dispatched_onto_a_trunk_branch(self):
        nodes = _nodes("export-button", "date-range-filter")
        complete_node_contracts(nodes, "claude-code", "proj")
        for node in nodes:
            branch = node["contract"]["branch"]
            self.assertNotIn(branch, {"main", "master", "trunk"}, node["id"])

    def test_no_node_is_dispatched_into_the_project_root(self):
        nodes = _nodes("export-button")
        complete_node_contracts(nodes, "claude-code", "proj")
        self.assertNotIn(nodes[0]["contract"]["worktree"], {".", "", "./"})

    def test_derived_paths_stay_inside_the_project(self):
        """A derived tree must not point outside the project root.

        `../<slug>` would resolve to a sibling of the project root, which no
        code provisions and which the codebase's own convention
        (`.worktrees/<id>`, monitor's fallback) does not use.
        """
        nodes = _nodes("export-button", "date-range-filter")
        complete_node_contracts(nodes, "claude-code", "proj")
        for node in nodes:
            tree = node["contract"]["worktree"]
            self.assertFalse(tree.startswith(".."), tree)
            self.assertFalse(tree.startswith("/"), tree)
            self.assertNotIn("/../", tree, tree)

    def test_the_publisher_derives_the_names_monitor_would_fall_back_to(self):
        """One vocabulary: both sides derive through the same function.

        monitor's fallback and the publisher's default name the same thing, so
        they must agree by construction rather than by two copies of a string
        that happen to match today.  Comparing the derived values (not the two
        modules' source text) keeps this a contract test: an equivalent rewrite
        on either side stays green, a diverging name does not.
        """
        node_id = "export-button"
        nodes = _nodes(node_id)
        complete_node_contracts(nodes, "claude-code", "proj")
        self.assertEqual(nodes[0]["contract"]["worktree"], node_worktree(node_id))
        self.assertEqual(nodes[0]["contract"]["branch"], node_branch(node_id))
        # The name stays legible: the id is still readable in it, under the
        # conventional prefixes the rest of the codebase uses.
        self.assertTrue(node_worktree(node_id).startswith(".worktrees/export-button"))
        self.assertTrue(node_branch(node_id).startswith("node/export-button"))

    def test_ids_that_differ_only_in_punctuation_get_different_trees(self):
        """The defect this PR fixes, reachable a second way through the slug.

        Normalising away trailing "." and "-" folded distinct legal ids onto one
        name, putting two writers back in one directory -- the lease is keyed per
        node id, so it reports both as active and never notices.
        """
        nodes = _nodes(*NEAR_MISS_IDS)
        complete_node_contracts(nodes, "claude-code", "proj")
        trees = [node["contract"]["worktree"] for node in nodes]
        branches = [node["contract"]["branch"] for node in nodes]
        self.assertEqual(len(set(trees)), len(NEAR_MISS_IDS), trees)
        self.assertEqual(len(set(branches)), len(NEAR_MISS_IDS), branches)

    def test_every_derived_branch_is_a_name_git_accepts(self):
        """A derived branch that git rejects fails the run at `switch -c`."""
        ids = NEAR_MISS_IDS + ("a..b", "mynode.lock", "HEAD", "a-b-..-..-c", "导出按钮")
        nodes = _nodes(*ids)
        complete_node_contracts(nodes, "claude-code", "proj")
        for node in nodes:
            branch = node["contract"]["branch"]
            checked = subprocess.run(
                ["git", "check-ref-format", "--branch", branch],
                capture_output=True,
            )
            self.assertEqual(
                checked.returncode, 0,
                "git rejects the branch derived from %r: %r" % (node["id"], branch),
            )

    def test_a_derived_name_fits_a_filesystem_component(self):
        """A long id must not derive a directory name the OS cannot create."""
        nodes = _nodes("n" * 400, "x" * 300 + "-tail")
        complete_node_contracts(nodes, "claude-code", "proj")
        trees = [node["contract"]["worktree"] for node in nodes]
        for tree in trees:
            for part in tree.split("/"):
                self.assertLessEqual(len(part.encode("utf-8")), 255, tree)
        self.assertEqual(len(set(trees)), 2, trees)

    def test_a_hand_written_node_spec_keeps_its_own_answer(self):
        """The legacy --node-spec path still declares its own placement.

        Those specs carry engineering fields by design, and existing projects
        depend on the values they name; only the product path forbids them.
        """
        nodes = [{
            "id": "export-button",
            "title": "export-button",
            "contract": {"worktree": ".worktrees/custom", "branch": "feature/custom"},
        }]
        complete_node_contracts(nodes, "claude-code", "proj")
        self.assertEqual(nodes[0]["contract"]["worktree"], ".worktrees/custom")
        self.assertEqual(nodes[0]["contract"]["branch"], "feature/custom")

    def test_a_product_spec_may_not_name_its_own_worktree_or_branch(self):
        """The last unguarded way back into one tree on the trunk.

        `complete_node_contracts` only fills what is missing, so a product spec
        that names `worktree: "."` keeps it and every derivation added here is
        bypassed -- the defect this change fixes, reached through the door the
        engineering-field guard left open.
        """
        from vibe_guide.node_spec import reject_engineering_fields
        for field, value in (
            ("worktree", "."),
            ("branch", "main"),
            # Aliases the fill reads to set the guarded keys: guarding only the
            # destination leaves the alias as a way in.
            ("writer", "codex-app-visible-developer"),
            ("reviewer", "codex-app-visible-reviewer"),
        ):
            spec = {
                "title": "t", "objective": "o",
                "nodes": [{
                    "id": "export-button", "title": "export-button",
                    "contract": {field: value},
                }],
            }
            with self.assertRaises(ValueError) as caught:
                reject_engineering_fields(spec)
            self.assertIn(field, str(caught.exception))

    def test_derived_names_are_stable_across_calls(self):
        """The same node must map to the same tree on every dispatch."""
        first = _nodes("export-button")
        second = _nodes("export-button")
        complete_node_contracts(first, "claude-code", "proj")
        complete_node_contracts(second, "claude-code", "proj")
        self.assertEqual(first[0]["contract"]["worktree"], second[0]["contract"]["worktree"])
        self.assertEqual(first[0]["contract"]["branch"], second[0]["contract"]["branch"])


if __name__ == "__main__":
    unittest.main()
