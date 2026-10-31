"""IR-5 / IR-6 regression: landing discipline vs fake fixtures vs removed worktrees.

IR-5: fake-provider fixtures (a repo-relative plain-directory worktree in a
project that is not a git checkout) keep the legacy pass-through, so the
invariant contract-test gate coverage in tests/test_monitor.py stays
reachable -- without weakening IR-3: absolute worktree claims and broken
claims inside a real git project still fail closed.

IR-6: a configured writer worktree that disappears after dispatch (its branch
still exists in the project) fails closed before review; a never-built
worktree in a fake/non-git project stays a compatibility pass.
"""
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide.scope_landing import check_delivery
from vibe_guide.paths import ProjectPaths

from tests.test_landing_required_before_review import (
    EmptyLandingGateTests,
    git,
)


class _FakeMonitor:
    """What a fixture/fake-provider monitor looks like at the check site."""

    def __init__(self, root):
        self.paths = ProjectPaths(root)
        self.events = []

    def _set_binding_status(self, snapshot, node_id, role, status):
        return None

    def _release_node_lease(self, snapshot, node_id):
        return None

    def _record(self, snapshot, event, data, active):
        self.events.append((event, dict(data)))


def _snapshot(worktree):
    return SimpleNamespace(
        run_id="run-ir5",
        nodes={"node-a": {"worktree": worktree, "branch": "branch-node-a"}},
        handles={},
    )


def _active():
    return {"role": "developer", "generation": 1, "task_id": "task-ir5"}


class Ir5FakeFixtureCompatibilityTests(unittest.TestCase):
    """tests/test_monitor.py ContractTestExecutionTests fixture shape."""

    def test_relative_plain_directory_in_non_git_project_passes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".worktrees" / "node-a").mkdir(parents=True)
            monitor = _FakeMonitor(root)
            self.assertTrue(
                check_delivery(monitor, _snapshot(".worktrees/node-a"), "node-a", _active())
            )
            self.assertEqual(monitor.events, [])

    def test_absolute_plain_directory_still_fails_closed(self):
        import tempfile
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plain = root / "ordinary-directory"
            plain.mkdir()
            monitor = _FakeMonitor(root)
            self.assertFalse(
                check_delivery(monitor, _snapshot(str(plain)), "node-a", _active())
            )
            self.assertEqual(monitor.events[0][0], "scope_landing_rejected")


class Ir6RemovedWorktreeTests(EmptyLandingGateTests):
    """Real git project: a worktree removed after dispatch fails closed."""

    def test_removed_worktree_fails_closed_before_review(self):
        # Remove bg2's worktree after dispatch, as if a cleanup wiped it;
        # the branch itself survives in the main repository.
        worktree = self.paths.root / self.snapshot.nodes["bg2"]["worktree"]
        git(self.paths.root, "worktree", "remove", "--force", str(worktree))
        self.assertFalse(worktree.exists())

        snapshot = self.report(self.snapshot, "bg2")

        node = snapshot.nodes["bg2"]
        self.assertNotIn(node["status"], {"review", "delivered", "accepted"},
                         node.get("reason"))
        self.assertTrue(
            self.events(snapshot.run_id, "scope_landing_rejected"),
            "a removed writer worktree must refuse through the landing park path",
        )


if __name__ == "__main__":
    unittest.main()
