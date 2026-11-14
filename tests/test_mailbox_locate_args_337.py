"""#337: a locate mailbox request includes copyable native tool arguments."""

import json
from pathlib import Path
import tempfile
import unittest

from vibe_guide.cli import run_cli
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import RunSnapshot, append_event, save_snapshot
from tests.test_monitor import node


def _save_run(root):
    paths = ProjectPaths(root)
    plan = Plan('plan-337', 1, 'docs/prd.md', ['node-a'], 'draft')
    record = authorize(build_authorization_card(plan, [node('node-a')],
        AgentCapabilities('fake', True, True, True, True, True, 'full')), 'AUTHORIZE')
    append_event(paths, RunEvent('run_started', {'run_id': 'run-337', 'node_ids': ['node-a'],
        'authorization_digest': record.digest, 'node_contract_digest': record.node_contract_digest}))
    save_snapshot(paths, RunSnapshot('run-337', 'plan-337', 1, 'running',
        {'node-a': {'status': 'running'}}, {}, authorization=record.to_dict(),
        authorization_digest=record.digest, node_contract_digest=record.node_contract_digest,
        event_sequence=1))


class MailboxLocateNativeArgsTests(unittest.TestCase):
    def test_codex_locate_suggests_only_thread_id(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _save_run(root)
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

    def test_non_codex_locate_has_no_codex_native_args(self):
        for provider in ("claude-code-visible", "workbuddy-visible"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                _save_run(root)
                requests = root / ".vibe/provider-actions/requests"
                requests.mkdir(parents=True)
                action = {
                    "schema_version": 1,
                    "action_id": "action-locate",
                    "operation": "locate",
                    "provider": provider,
                    "run_id": "run-337",
                    "issue_id": "node-a",
                    "role": "developer",
                    "generation": 1,
                    "sequence": 0,
                    "native_tool": "provider_locate",
                    "request_digest": "d" * 64,
                    "request": {"threadId": "not-a-codex-thread"},
                }
                (requests / "action-locate.json").write_text(
                    json.dumps(action), encoding="utf-8"
                )
                result = run_cli(
                    ["supervisor-mailbox", "--run-id", "run-337", "--json"], root
                )
                self.assertEqual(result.exit_code, 0, result.payload)
                self.assertNotIn("native_args", result.payload["pending"][0])


if __name__ == "__main__":
    unittest.main()
