"""IR-3: a configured plain directory must not bypass scope-landing checks."""

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from vibe_guide.paths import ProjectPaths
from vibe_guide.scope_landing import check_delivery


class _Monitor:
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
        run_id="run-ir3",
        nodes={"node-a": {"worktree": worktree}},
        handles={},
    )


def _active():
    return {"role": "developer", "generation": 1, "task_id": "task-ir3"}


class PlainDirectoryDeliveryTests(unittest.TestCase):
    def test_plain_directory_worktree_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "ordinary-directory"
            plain.mkdir()
            monitor = _Monitor(Path(directory))
            snapshot = _snapshot(str(plain))

            self.assertFalse(
                check_delivery(monitor, snapshot, "node-a", _active())
            )
            self.assertEqual(monitor.events[0][0], "scope_landing_rejected")

    def test_fake_provider_without_worktree_or_branch_claim_is_not_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = _Monitor(Path(directory))
            # Legacy fake providers have no node worktree claim to inspect.
            self.assertTrue(
                check_delivery(monitor, _snapshot(""), "node-a", _active())
            )
            # Guide-mode fakes can carry a label without a real branch claim;
            # there is no monitor project path through which to inspect it.
            monitor_without_paths = SimpleNamespace()
            self.assertTrue(
                check_delivery(
                    monitor_without_paths,
                    _snapshot("fake-provider-worktree"),
                    "node-a",
                    _active(),
                )
            )
            self.assertEqual(monitor.events, [])


if __name__ == "__main__":
    unittest.main()
