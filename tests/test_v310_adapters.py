import unittest

from vibe_guide.adapters.task_provider import TaskProviderAdapter


class V310AdapterTests(unittest.TestCase):
    def test_upgrade_entry_is_provider_neutral_and_delegates(self):
        calls = []
        delegate = lambda request: calls.append(request) or {"status": "complete", "evidence": []}
        adapter = TaskProviderAdapter("codex-app-visible", delegate)
        metadata = adapter.describe_upgrade_entry()
        self.assertEqual(metadata["entry"], "upgrade")
        self.assertTrue(metadata["provider_neutral"])
        result = adapter.invoke_upgrade({"mode": "layered"})
        self.assertEqual(result["status"], "complete")
        self.assertEqual(calls, [{"mode": "layered"}])

    def test_provider_unknown_and_timeout_are_structured(self):
        for status in ("unknown", "unknown_timeout"):
            adapter = TaskProviderAdapter("missing", lambda request, status=status: {"status": status, "evidence": [status]})
            result = adapter.invoke_upgrade({})
            self.assertEqual(result["status"], status)
            self.assertEqual(result["evidence"], [status])


if __name__ == "__main__":
    unittest.main()
