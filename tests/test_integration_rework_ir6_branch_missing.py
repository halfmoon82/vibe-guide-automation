"""IR-6 residual branch: a removed writer worktree fails closed with the branch gone too.

The delivery-truth gate must not use branch existence as a pass condition:
inside a git project a configured writer worktree that is missing at
delivery time -- whether or not its branch survives -- cannot confirm any
landing and is refused through the existing landing park path.  Fake
compatibility (non-git projects, not-yet-built worktrees) is unchanged.
"""
import unittest

from tests.test_landing_required_before_review import EmptyLandingGateTests, git


class Ir6BranchMissingTests(EmptyLandingGateTests):
    """Real git project: remove the worktree AND its branch before delivery."""

    def test_removed_worktree_and_branch_fail_closed_before_review(self):
        node = self.snapshot.nodes["bg2"]
        worktree = self.paths.root / node["worktree"]
        git(self.paths.root, "worktree", "remove", "--force", str(worktree))
        git(self.paths.root, "branch", "-D", node["branch"])
        self.assertFalse(worktree.exists())

        snapshot = self.report(self.snapshot, "bg2")

        current = snapshot.nodes["bg2"]
        self.assertEqual(current["status"], "blocked_unknown", current.get("reason"))
        self.assertEqual(current.get("reason_class"), "landing_unreadable")
        self.assertTrue(
            self.events(snapshot.run_id, "scope_landing_rejected"),
            "a missing writer worktree must refuse even when its branch is gone",
        )


if __name__ == "__main__":
    unittest.main()
