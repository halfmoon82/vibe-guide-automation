"""#337: a locate mailbox request includes copyable native tool arguments."""

import json
from pathlib import Path
import tempfile
import unittest

from vibe_guide.cli import run_cli


class MailboxLocateNativeArgsTests(unittest.TestCase):
    def test_codex_locate_suggests_only_thread_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requests = root / ".vibe/provider-actions/requests"
            requests.mkdir(parents=True)
            action = {
                "schema_version": 1,
                "action_id": "action-locate",
                "operation": "locate",
                "provider": "codex-app-visible",
                "run_id": "run-337",
                "issue_id": "node-a",
                "role": "developer",
                "generation": 1,
                "sequence": 0,
                "native_tool": "codex_app__navigate_to_codex_page",
                "request_digest": "d" * 64,
                "request": {"threadId": "thread-337"},
            }
            (requests / "action-locate.json").write_text(
                json.dumps(action), encoding="utf-8"
            )
            result = run_cli(
                ["supervisor-mailbox", "--run-id", "run-337", "--json"], root
            )
            self.assertEqual(result.exit_code, 0, result.payload)
            item = result.payload["pending"][0]
            self.assertEqual(item["native_args"], {"threadId": "thread-337"})
            self.assertNotIn("hostId", item["native_args"])


if __name__ == "__main__":
    unittest.main()
