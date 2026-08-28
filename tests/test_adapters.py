import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.paths import ProjectPaths


class ProviderActionStoreTests(unittest.TestCase):
    def test_has_request_is_read_only_and_matches_identity_and_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            store = ProviderActionStore(paths)

            self.assertFalse(store.has_request("run-1", "issue-1", "developer"))
            self.assertFalse(store.root.exists())

            store.request(
                operation="create",
                provider="codex-app-visible",
                run_id="run-1",
                issue_id="issue-1",
                role="developer",
                generation=1,
                native_tool="codex_app__create_thread",
                request={"prompt": "work"},
            )

            self.assertTrue(store.has_request("run-1", "issue-1", "developer"))
            self.assertTrue(
                store.has_request("run-1", "issue-1", "developer", "create")
            )
            self.assertFalse(
                store.has_request("run-1", "issue-1", "developer", "resume")
            )
            self.assertFalse(store.has_request("run-2", "issue-1", "developer"))


if __name__ == "__main__":
    unittest.main()
