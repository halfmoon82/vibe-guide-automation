import unittest

from vibe_guide.dag import transition_node_status
from vibe_guide.evidence import validate_task_pair


class V4SddLifecycleTests(unittest.TestCase):
    def test_full_lifecycle_and_same_task_rework(self):
        node = {"status": "planned", "developer_task_id": "dev-1", "reviewer_task_id": "rev-1"}
        for status in ("ready", "running", "review", "rework", "review", "accepted", "archived"):
            transition_node_status(node, status)
            self.assertEqual(node["status"], status)
        self.assertTrue(validate_task_pair(node["developer_task_id"], node["reviewer_task_id"]))

    def test_invalid_transition_and_same_task_pair_are_rejected(self):
        node = {"status": "planned"}
        with self.assertRaises(ValueError):
            transition_node_status(node, "review")
        with self.assertRaises(ValueError):
            validate_task_pair("same", "same")

    def test_running_status_is_snapshot_compatible(self):
        node = {"status": "ready"}
        transition_node_status(node, "developing")
        self.assertEqual(node["status"], "running")


if __name__ == "__main__":
    unittest.main()
