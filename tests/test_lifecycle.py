import unittest

from vibe_guide.lifecycle import (
    CanonicalTaskStatus,
    migrate_task_record,
    normalize_task_status,
)


class LifecycleTests(unittest.TestCase):
    def test_status_aliases_are_canonicalized(self):
        self.assertEqual(normalize_task_status("delivered"), "DELIVERED")
        self.assertEqual(normalize_task_status("DELIVERY_COMPLETE"), "DELIVERED")

    def test_pending_client_id_is_not_terminal_or_usable(self):
        record = migrate_task_record({"clientThreadId": "client-1"})
        self.assertEqual(record["status"], "SETUP_PENDING")
        self.assertIsNone(record["threadId"])
        self.assertEqual(record["clientThreadId"], "client-1")

    def test_unknown_status_is_fail_closed_and_legacy_is_preserved(self):
        record = migrate_task_record({"status": "made-up", "foo": "bar"})
        self.assertEqual(record["status"], CanonicalTaskStatus.BLOCKED_UNKNOWN)
        self.assertEqual(record["legacy"]["status"], "made-up")
        self.assertEqual(record["legacy"]["foo"], "bar")

    def test_topology_is_kept_at_top_level_and_not_pushed_into_legacy(self):
        record = migrate_task_record(
            {"threadId": "t-2", "status": "running", "topology": "visible-sdd", "branch": "fix/y"}
        )
        self.assertEqual(record["topology"], "visible-sdd")
        self.assertNotIn("topology", record["legacy"])

    def test_record_without_topology_migrates_exactly_as_before(self):
        # Baseline captured on main (60a8fa8) before topology joined _CANONICAL_FIELDS.
        legacy_record = {
            "thread_id": "t-1",
            "status": "delivery_complete",
            "host_id": "h-1",
            "branch": "fix/x",
            "worktree": "/tmp/wt",
            "extra": "keep",
        }
        expected = {
            "branch": "fix/x",
            "clientThreadId": None,
            "extra": "keep",
            "generation": 0,
            "host_id": "h-1",
            "legacy": {"extra": "keep"},
            "platform_task_id": "t-1",
            "status": "DELIVERED",
            "task_id": "t-1",
            "threadId": "t-1",
            "thread_id": "t-1",
            "worktree": "/tmp/wt",
        }
        self.assertEqual(migrate_task_record(legacy_record), expected)

    def test_topology_migration_is_idempotent(self):
        record = {"threadId": "t-3", "status": "running", "topology": "visible-sdd"}
        once = migrate_task_record(record)
        twice = migrate_task_record(once)
        self.assertEqual(once, twice)
        self.assertEqual(twice["topology"], "visible-sdd")
        self.assertNotIn("topology", twice["legacy"])


if __name__ == "__main__":
    unittest.main()
