"""Contract: each node gets its own worktree and branch, and never main.

`complete_node_contracts` defaulted every node to worktree "." and branch
"main".  A product spec carries no engineering fields by design, so on the
product path every node took those defaults: two parallel developer sessions
were dispatched into the same directory on the trunk.  The writer lease is
keyed per node, so it does not catch the collision, and monitor's own default
(`node/<id>`) never applies because the spec's literal value wins.
"""
import unittest
from pathlib import Path

from vibe_guide.node_spec import complete_node_contracts


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

    def test_derived_names_match_the_conventions_monitor_falls_back_to(self):
        """One vocabulary: the derived names are the ones monitor would pick.

        monitor's own fallbacks were already safe; the spec's literal values
        shadowed them.  Deriving different names would reintroduce two
        vocabularies for the same thing.
        """
        node_id = "export-button"
        nodes = _nodes(node_id)
        complete_node_contracts(nodes, "claude-code", "proj")
        monitor_source = (
            Path(__file__).resolve().parent.parent / "vibe_guide" / "monitor.py"
        ).read_text(encoding="utf-8")
        self.assertIn('"worktree": node.contract.get("worktree", ".worktrees/" + node_id)', monitor_source)
        self.assertIn('"branch": node.contract.get("branch", "node/" + node_id)', monitor_source)
        self.assertEqual(nodes[0]["contract"]["worktree"], ".worktrees/" + node_id)
        self.assertEqual(nodes[0]["contract"]["branch"], "node/" + node_id)

    def test_an_explicit_contract_value_is_preserved(self):
        """A spec that does say where to work keeps its own answer."""
        nodes = [{
            "id": "export-button",
            "title": "export-button",
            "contract": {"worktree": "../custom-tree", "branch": "feature/custom"},
        }]
        complete_node_contracts(nodes, "claude-code", "proj")
        self.assertEqual(nodes[0]["contract"]["worktree"], "../custom-tree")
        self.assertEqual(nodes[0]["contract"]["branch"], "feature/custom")

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
