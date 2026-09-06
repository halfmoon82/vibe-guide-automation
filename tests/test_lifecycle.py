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


if __name__ == "__main__":
    unittest.main()
