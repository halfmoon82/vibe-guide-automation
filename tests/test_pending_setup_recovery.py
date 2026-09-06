import unittest

from vibe_guide.monitor import reconcile_pending_binding


class PendingSetupRecoveryTests(unittest.TestCase):
    def test_client_thread_pending_retries_same_generation_and_writer(self):
        snapshot = {"nodes": {"N4": {"status": "running", "generation": 1,
                                      "binding": {"clientThreadId": "client-1"},
                                      "successor": "old"}}}
        changed = reconcile_pending_binding(snapshot, "N4", object())
        self.assertFalse(changed)
        self.assertEqual(snapshot["nodes"]["N4"]["status"], "retry_pending")
        self.assertEqual(snapshot["nodes"]["N4"]["generation"], 1)
        self.assertIsNone(snapshot["nodes"]["N4"]["successor"])

    def test_locate_result_for_different_setup_identity_is_rejected(self):
        class Runner:
            def resolve_pending(self, binding):
                return {
                    "threadId": "thread-other",
                    "clientThreadId": "client-other",
                    "provider": "codex",
                    "status": "verified",
                }

        snapshot = {"nodes": {"N4": {
            "status": "running", "generation": 1,
            "binding": {"clientThreadId": "client-1", "issue_id": "N4",
                         "role": "developer", "generation": 1},
            "successor": "old",
        }}}
        changed = reconcile_pending_binding(snapshot, "N4", Runner())
        self.assertFalse(changed)
        self.assertIsNone(snapshot["nodes"]["N4"].get("threadId"))
        self.assertEqual(snapshot["nodes"]["N4"]["status"], "retry_pending")


if __name__ == "__main__":
    unittest.main()
