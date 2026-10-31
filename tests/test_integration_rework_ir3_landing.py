"""IR-3 same-gate regression: landing_report fail-closes on a plain directory.

Companion to worktree-preflight's tests/test_integration_rework_ir3.py: that
file pins ``check_delivery``; this one pins the ``landing_report`` contract
itself and the second gate that consumes it (``check_delivery_content``,
the empty-landing gate added for #336).  A configured worktree that exists
without a ``.git`` raises ``ScopeLandingUnknown`` -- "cannot confirm the
landing" is never "nothing landed".  Fake-provider compatibility (no
worktree configured, no monitor paths, not-yet-created directory) is
unchanged and keeps returning None / passing.
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from vibe_guide.paths import ProjectPaths
from vibe_guide.scope_landing import (
    ScopeLandingUnknown,
    check_delivery_content,
    landing_report,
)


class _Monitor:
    def __init__(self, root):
        self.paths = ProjectPaths(root)
        self.events = []

    def _record(self, snapshot, event, data, active):
        self.events.append((event, dict(data)))

    def _set_binding_status(self, snapshot, node_id, role, status):
        return None

    def _release_node_lease(self, snapshot, node_id):
        return None


def _snapshot(worktree):
    return SimpleNamespace(
        run_id="run-ir3",
        nodes={"node-a": {"worktree": worktree}},
        handles={},
    )


def _active():
    return {"role": "developer", "generation": 1, "task_id": "task-ir3"}


class LandingReportPlainDirectoryTests(unittest.TestCase):
    def test_landing_report_raises_for_plain_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "ordinary-directory"
            plain.mkdir()
            monitor = _Monitor(Path(directory))
            with self.assertRaises(ScopeLandingUnknown):
                landing_report(monitor, _snapshot(str(plain)), "node-a")

    def test_landing_report_raises_for_relative_plain_directory(self):
        import subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".worktrees" / "node-a").mkdir(parents=True)
            # A repo-relative broken claim fails closed inside a real git
            # project; the non-git-project compat shape lives in
            # tests/test_integration_rework_ir5_ir6.py (IR-5).
            subprocess.run(["git", "-C", str(root), "init", "-q", "-b", "main"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "user.email", "t@e.c"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "config", "user.name", "T"],
                           check=True, capture_output=True)
            subprocess.run(["git", "-C", str(root), "commit", "-q", "--allow-empty",
                            "-m", "base"], check=True, capture_output=True)
            monitor = _Monitor(root)
            with self.assertRaises(ScopeLandingUnknown):
                landing_report(monitor, _snapshot(".worktrees/node-a"), "node-a")

    def test_missing_directory_and_fake_compatibility_still_return_none(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = _Monitor(Path(directory))
            # Not yet created: the launcher or supervisor may still build it.
            self.assertIsNone(
                landing_report(monitor, _snapshot(".worktrees/missing"), "node-a")
            )
            # No worktree claim at all (fake providers, guide mode).
            self.assertIsNone(landing_report(monitor, _snapshot(""), "node-a"))
            # Guide-mode fakes can carry a label without monitor project paths.
            bare = SimpleNamespace()
            self.assertIsNone(
                landing_report(bare, _snapshot("fake-provider-worktree"), "node-a")
            )

    def test_content_gate_fails_closed_on_plain_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = Path(directory) / "ordinary-directory"
            plain.mkdir()
            monitor = _Monitor(Path(directory))
            snapshot = _snapshot(str(plain))
            self.assertFalse(
                check_delivery_content(monitor, snapshot, "node-a", _active())
            )
            self.assertEqual(monitor.events[0][0], "scope_landing_rejected")

    def test_content_gate_passes_missing_directory_compatibility(self):
        with tempfile.TemporaryDirectory() as directory:
            monitor = _Monitor(Path(directory))
            self.assertTrue(
                check_delivery_content(
                    monitor, _snapshot(".worktrees/missing"), "node-a", _active()
                )
            )
            self.assertEqual(monitor.events, [])


if __name__ == "__main__":
    unittest.main()
