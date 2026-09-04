import unittest

from vibe_guide.adapters.task_provider import classify_provider_for_v4


class V4ProviderDegradationTests(unittest.TestCase):
    def test_provider_statuses_are_conservative(self):
        self.assertEqual(classify_provider_for_v4({"status": "verified", "node_id": "n1"}), "verified")
        self.assertEqual(classify_provider_for_v4({"status": "timeout", "node_id": "n1"}), "degraded")
        self.assertEqual(classify_provider_for_v4({"node_id": "n1"}), "unknown")

    def test_missing_visible_locate_evidence_is_unknown_and_node_local(self):
        result = classify_provider_for_v4({"node_id": "n1", "action": "locate", "visible": False})
        self.assertEqual(result, "unknown")
        self.assertNotIn("global", result)


if __name__ == "__main__":
    unittest.main()
