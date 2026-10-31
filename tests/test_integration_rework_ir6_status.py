"""IR-6 status contract: an unconfirmable landing parks blocked_unknown, not awaiting_supervisor.

#336's output and error behavior both require "cannot confirm the landing /
diff unreadable / non-independent worktree" to be recorded as blocked_unknown
with a structural code.  The landing refusal keeps its scope_landing_rejected
event, but the node state is the blocked_unknown park so the recovery
semantics match the contract.
"""
import unittest

from tests.test_landing_required_before_review import EmptyLandingGateTests, git


class Ir6StatusTests(EmptyLandingGateTests):
    def test_removed_worktree_parks_blocked_unknown_with_structural_code(self):
        worktree = self.paths.root / self.snapshot.nodes["bg2"]["worktree"]
        git(self.paths.root, "worktree", "remove", "--force", str(worktree))

        snapshot = self.report(self.snapshot, "bg2")

        node = snapshot.nodes["bg2"]
        self.assertEqual(node["status"], "blocked_unknown", node.get("reason"))
        self.assertEqual(node.get("reason_class"), "landing_unreadable")
        self.assertEqual(node.get("reason_code"), "scope_landing:landing_unreadable")
        self.assertFalse(node.get("retryable_action"))
        self.assertEqual(node.get("scope_landing_rejection"),
                         [{"path": "*", "cause": "landing_unreadable"}])
        self.assertTrue(self.events(snapshot.run_id, "scope_landing_rejected"))

    def test_removed_worktree_and_branch_parks_blocked_unknown(self):
        node = self.snapshot.nodes["bg2"]
        worktree = self.paths.root / node["worktree"]
        git(self.paths.root, "worktree", "remove", "--force", str(worktree))
        git(self.paths.root, "branch", "-D", node["branch"])

        snapshot = self.report(self.snapshot, "bg2")

        self.assertEqual(snapshot.nodes["bg2"]["status"], "blocked_unknown",
                         snapshot.nodes["bg2"].get("reason"))

    def test_crash_before_save_replays_to_blocked_unknown(self):
        # The refusal event is on disk but the snapshot save missed it; the
        # replay twin must rebuild the same blocked_unknown park instead of
        # the legacy worker-question awaiting_supervisor state.
        from vibe_guide.state import load_snapshot

        snapshot = self.snapshot
        worktree = self.paths.root / snapshot.nodes["bg2"]["worktree"]
        git(self.paths.root, "worktree", "remove", "--force", str(worktree))
        crashed = load_snapshot(self.paths, snapshot.run_id)
        self.monitor()._record(
            crashed, "scope_landing_rejected",
            {"run_id": crashed.run_id, "node_id": "bg2",
             "role": "developer", "generation": 1,
             "handle_id": crashed.nodes["bg2"]["active_task"]["handle_id"],
             "reason_class": "landing_unreadable",
             "reason_code": "scope_landing:landing_unreadable",
             "rejection_code": "scope_landing:landing_unreadable"},
            crashed.nodes["bg2"]["active_task"])

        snapshot = self.monitor().resume(snapshot.run_id, self.runner())
        node = snapshot.nodes["bg2"]
        self.assertEqual(node["status"], "blocked_unknown", node.get("reason"))
        self.assertEqual(node.get("reason_class"), "landing_unreadable")


if __name__ == "__main__":
    unittest.main()
