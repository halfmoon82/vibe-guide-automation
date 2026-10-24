"""issue #276: a rework/continuation dispatch must retire the prior session.

Recorded failure (run-64c826b5): the supervisor-heartbeat-wake run
re-dispatched rework while the earlier ``claude --bg`` session was still
``working``, leaving three live developer sessions in the same worktree.
The runner now stops and awaits a working predecessor, refuses a second
writer when the stop cannot be proven, and marks the superseded handle
stale so it is never polled or reported as an unregistered handle again.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.claude_bg import ClaudeBgUnknown
from vibe_guide.contracts import RunHandle
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.claude_bg import ClaudeBgRunner

# Match the fake's generated session ids (first and second launch).
UUID_A = "00000001-1111-4222-8333-000000000001"
UUID_B = "00000002-1111-4222-8333-000000000002"


class FakeLauncher:
    """One launcher serving every session; ``stop`` flips state like the CLI."""

    def __init__(self):
        self.states = {}
        self.calls = []
        self._launch_count = 0

    def launch(self, role, issue_id, contract_path):
        self._launch_count += 1
        task_id = "%08d-1111-4222-8333-%012d" % (self._launch_count, self._launch_count)
        self.calls.append(("launch", role, issue_id))
        self.states.setdefault(task_id, "working")
        return {"task_id": task_id}

    def status(self, task_id):
        self.calls.append(("status", task_id))
        state = self.states.get(task_id, "working")
        if isinstance(state, Exception):
            raise state
        return state

    def stop(self, task_id):
        self.calls.append(("stop", task_id))
        if self.states.get(task_id) == "working":
            self.states[task_id] = "stopped"

    def resume(self, task_id, message):
        self.calls.append(("resume", task_id, message))


class RetirePriorSessionsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.launcher = FakeLauncher()
        self.runner = ClaudeBgRunner(
            lambda c, w: self.launcher,
            delivery_reader=lambda claims: None,
            paths=self.paths,
            sleeper=lambda seconds: None,
        )
        self.contract = {
            "node_id": "n1", "role": "developer", "run_id": "run-1",
            "generation": 1, "contract_path": "/c.md",
        }

    def start(self, **extra):
        return self.runner.start(dict(self.contract, **extra), Path("/tmp"))

    def test_rework_stops_the_working_prior_session(self):
        first = self.start()
        second = self.start(continuation=True, generation=2)

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertIn(("status", UUID_A), self.launcher.calls)
        self.assertIn(("stop", UUID_A), self.launcher.calls)
        # Exactly one working session remains under the node.
        self.assertEqual(
            [task_id for task_id, state in self.launcher.states.items() if state == "working"],
            [UUID_B],
        )

    def test_resting_prior_session_is_not_stopped_again(self):
        for state in ("done", "stopped", "blocked", "failed"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as root:
                paths = ProjectPaths(Path(root))
                launcher = FakeLauncher()
                runner = ClaudeBgRunner(
                    lambda c, w: launcher,
                    delivery_reader=lambda claims: None,
                    paths=paths,
                    sleeper=lambda seconds: None,
                )
                runner.start(dict(self.contract), Path("/tmp"))
                launcher.states[UUID_A] = state
                runner.start(dict(self.contract, continuation=True, generation=2), Path("/tmp"))
                self.assertIn(("status", UUID_A), launcher.calls)
                self.assertNotIn(("stop", UUID_A), launcher.calls)

    def test_unstoppable_prior_session_blocks_the_new_writer(self):
        self.start()
        # `claude stop` answered but the session never leaves ``working``.
        self.launcher.stop = lambda task_id: self.launcher.calls.append(("stop", task_id))

        with self.assertRaises(ClaudeBgUnknown):
            self.start(continuation=True, generation=2)

        self.assertEqual(
            [call for call in self.launcher.calls if call[0] == "launch"],
            [("launch", "developer", "n1")],
        )

    def test_unreachable_prior_session_blocks_the_new_writer(self):
        self.start()
        self.launcher.states[UUID_A] = ClaudeBgUnknown("session not found")

        with self.assertRaises(ClaudeBgUnknown):
            self.start(continuation=True, generation=2)

        self.assertEqual(
            [call for call in self.launcher.calls if call[0] == "launch"],
            [("launch", "developer", "n1")],
        )

    def test_initial_dispatch_still_starts_beside_a_prior_session(self):
        # Only rework/continuation pays the retirement cost; a plain start is
        # not a second writer for the same in-flight work.
        first = self.start()
        second = self.start()
        self.assertNotEqual(first.run_id, second.run_id)
        self.assertNotIn(("stop", UUID_A), self.launcher.calls)

    def test_superseded_handle_is_stale_and_silent(self):
        first = self.start()
        second = self.start(continuation=True, generation=2)

        # Polling the dead handle emits nothing -- no delivered replay, no
        # state event, no unregistered-handle report.
        self.assertEqual(self.runner.poll(first), [])
        # The live handle still reports normally.
        self.launcher.states[UUID_B] = "blocked"
        self.assertEqual(
            [event.event for event in self.runner.poll(second)], ["needs_attention"]
        )

    def test_stale_handle_stays_silent_across_a_restart(self):
        first = self.start()
        self.start(continuation=True, generation=2)

        restarted = ClaudeBgRunner(
            lambda c, w: FakeLauncher(),
            delivery_reader=lambda claims: None,
            paths=self.paths,
            sleeper=lambda seconds: None,
        )
        self.assertEqual(restarted.poll(first), [])
        record = json.loads(
            self.paths.resolve_vibe_path(
                "provider-actions/claude-bg-sessions/{}.json".format(first.run_id)
            ).read_text(encoding="utf-8")
        )
        self.assertIs(record["stale"], True)

    def test_resume_keeps_the_target_and_retires_its_old_handle(self):
        first = self.start()
        handle = self.start(resume_task_id=UUID_A, rework_message="fix P1", generation=2)

        # The runner does not stop the resume target itself; the launcher's
        # resume path owns that verified stop.
        self.assertIn(("resume", UUID_A, "fix P1"), self.launcher.calls)
        self.assertNotIn(("stop", UUID_A), self.launcher.calls)
        self.assertEqual(self.runner.poll(first), [])
        self.assertNotEqual(handle.run_id, first.run_id)

    def test_resume_still_retires_other_sessions_of_the_node(self):
        self.start()
        second = self.start(continuation=True, generation=2)
        # A third dispatch resumes session B while A's record still exists.
        handle = self.start(
            resume_task_id=UUID_B, rework_message="again", generation=3
        )
        self.assertIn(("resume", UUID_B, "again"), self.launcher.calls)
        self.assertEqual(self.runner.poll(second), [])
        self.assertNotEqual(handle.run_id, second.run_id)

    def test_other_nodes_sessions_are_not_touched(self):
        foreign = self.runner.start(dict(self.contract, node_id="n2"), Path("/tmp"))
        self.start()

        self.start(continuation=True, generation=2)

        # n1's rework stops its own prior session (UUID_B) but never touches
        # the foreign node's session (UUID_A), which stays live and pollable.
        self.assertIn(("stop", UUID_B), self.launcher.calls)
        self.assertNotIn(("stop", UUID_A), self.launcher.calls)
        self.launcher.states[UUID_A] = "blocked"
        self.assertEqual(
            [event.event for event in self.runner.poll(foreign)], ["needs_attention"]
        )


if __name__ == "__main__":
    unittest.main()
