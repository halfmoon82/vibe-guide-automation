import unittest

from vibe_guide import lifecycle, models, stall, state, task_registry
from vibe_guide.paths import RunFiles
from vibe_guide.runners import provider_action


class StatusSetTests(unittest.TestCase):
    def test_sets_equal_their_sources(self):
        self.assertEqual(lifecycle.TERMINAL_NODE_STATUSES, stall.TERMINAL_STATUSES)
        self.assertEqual(lifecycle.SESSION_OCCUPYING_STATUSES, state._WORKER_SESSION_STATUSES)
        self.assertEqual(lifecycle.TERMINAL_TASK_STATUSES, task_registry._TERMINAL_STATUSES)
        self.assertEqual(lifecycle.RUNNER_TERMINAL_STATUSES, set(provider_action.WAIT_TERMINAL_STATUSES))
        self.assertTrue(lifecycle.ACTIVE_NODE_STATUSES <= state._USER_ACTIVE_STATUSES)
        self.assertTrue(lifecycle.SESSION_OCCUPYING_STATUSES <= lifecycle.ACTIVE_NODE_STATUSES)
        self.assertFalse(lifecycle.ACTIVE_NODE_STATUSES & lifecycle.TERMINAL_NODE_STATUSES)

    def test_known_differences_between_existing_sets(self):
        m, s, t = set(models._NODE_STATUSES), set(state._NODE_STATUSES), set(task_registry._STATUSES)
        self.assertEqual(m - s, {"ready", "blocked_deploy"})
        self.assertEqual(
            s - m,
            {"retry_pending", "start_pending", "failed", "stopped", "skipped_by_user", "blocked_by_required_node",
             "awaiting_supervisor"},
        )
        self.assertEqual(t - s, {"created", "archived"})
        self.assertEqual(s - t, {"planned", "retry_pending", "skipped_by_user", "blocked_by_required_node", "brief_pending",
                                 "awaiting_supervisor"})
        # terminal notions differ per layer: node accepted only; task adds archived/blocked_design/failed/stopped
        self.assertEqual(set(lifecycle.TERMINAL_TASK_STATUSES) - set(lifecycle.TERMINAL_NODE_STATUSES), set(lifecycle.TERMINAL_TASK_STATUSES))
        self.assertEqual(lifecycle.RUNNER_TERMINAL_STATUSES - lifecycle.TERMINAL_TASK_STATUSES, {"complete", "completed"})

    def test_run_files_match_names_in_use(self):
        self.assertEqual(RunFiles.STATE, "state.json")
        self.assertEqual(RunFiles.EVENTS, "events.jsonl")
        self.assertEqual(RunFiles.TASKS, "tasks.json")


if __name__ == "__main__":
    unittest.main()
