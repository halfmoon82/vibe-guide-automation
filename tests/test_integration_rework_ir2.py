"""IR-2: engineering blocks need supervisor investigation, not auto-repair copy."""

import unittest

from vibe_guide.state import (
    STALL_INVESTIGATE_STATUS,
    WORKTREE_ACTION_STATUS,
    WORKTREE_PREFLIGHT_KEY,
    map_user_status,
)


class EngineeringBlockedUserStatusTests(unittest.TestCase):
    def test_exact_ir2_record_requires_supervisor_investigation(self):
        record = {
            "status": "blocked_unknown",
            "reason_class": "engineering_blocked",
            "retryable_action": None,
            "active_task": None,
        }
        self.assertEqual(map_user_status(record), STALL_INVESTIGATE_STATUS)
        self.assertNotEqual(map_user_status(record), "自动修复中")

    def test_recovery_markers_do_not_swallow_engineering_blocked(self):
        record = {
            "status": "blocked_unknown",
            "reason_class": "engineering_blocked",
            "binding_phase": "binding_repair_pending",
            "retryable_action": {
                "role": "developer",
                "reason_class": "engineering_blocked",
            },
            "active_task": None,
        }
        self.assertEqual(map_user_status(record), STALL_INVESTIGATE_STATUS)

    def test_ordinary_retry_and_binding_recovery_stay_auto_repairing(self):
        records = (
            {
                "status": "blocked_unknown",
                "reason_class": "binding_unknown",
                "retryable_action": None,
                "active_task": None,
            },
            {
                "status": "blocked_unknown",
                "binding_phase": "binding_repair_pending",
                "active_task": None,
            },
            {
                "status": "running",
                "retryable_action": {"role": "developer"},
                "active_task": None,
            },
        )
        for record in records:
            with self.subTest(record=record):
                self.assertEqual(map_user_status(record), "自动修复中")

    def test_worktree_decision_and_active_labels_keep_their_priority(self):
        worktree = {
            "status": "blocked_unknown",
            "reason_class": "engineering_blocked",
            "active_task": None,
            WORKTREE_PREFLIGHT_KEY: {"reason_code": "worktree_missing"},
        }
        self.assertEqual(map_user_status(worktree), WORKTREE_ACTION_STATUS)

        for decision in ("blocked_design", "blocked_deploy"):
            with self.subTest(decision=decision):
                self.assertEqual(
                    map_user_status({
                        "status": decision,
                        "reason_class": "engineering_blocked",
                        "retryable_action": None,
                        "active_task": None,
                    }),
                    "需要你决定",
                )

        active = {
            "status": "running",
            "reason_class": "engineering_blocked",
            "retryable_action": None,
            "active_task": {"task_id": "task-ir2"},
        }
        self.assertEqual(map_user_status(active), "已启动")


if __name__ == "__main__":
    unittest.main()
