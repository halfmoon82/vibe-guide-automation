import unittest
import json
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from vibe_guide.monitor import Monitor
from vibe_guide.change_requests import ChangeRequest
from vibe_guide.cli import run_cli


class MonitorTests(unittest.TestCase):
    def test_unknown_remote_without_local_authorization_is_blocked_unknown(self):
        result = Monitor.classify_change_request({"title": "CANMERGE", "status": "PASS"})
        self.assertEqual(result["status"], "blocked_unknown")

    def test_cli_json_reports_verified_remote_from_corrobated_facts(self):
        payload = {
            "change_request": {
                "provider": "example",
                "kind": "PR",
                "source": "feature",
                "target": "main",
                "head_sha": "a" * 40,
                "tree_sha": "b" * 40,
            },
            "observed_facts": {
                "source": "feature",
                "target": "main",
                "head_sha": "a" * 40,
                "tree_sha": "b" * 40,
                "remote_merge_supported": True,
                "remote_merge_verified": True,
            },
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            request_path = Path(temp_dir) / "request.json"
            request_path.write_text(json.dumps(payload), encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                exit_code = run_cli(("change-request", "--request", str(request_path), "--json"))
        self.assertEqual(exit_code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result["merge_capability"], "verified_remote")
        self.assertTrue(result["remote_merge"])

    def test_monitor_keeps_local_and_remote_merge_actions_separate(self):
        request = ChangeRequest(
            "example", "PR", "feature", "main", "a" * 40, "b" * 40,
            "verified_remote", "", "V2-4", "PR-7",
        )
        with self.assertRaises(PermissionError):
            Monitor.merge_local(
                request,
                {
                    "allowed_actions": ("merge",),
                    "merge_scope": {
                        "issue_id": "V2-4", "source_sha": "a" * 40,
                        "target_branch": "main", "change_request": "PR-7",
                    },
                },
                {
                    "target_ref": "main", "source_sha": "a" * 40,
                    "merge_base": "c" * 40, "merge_commit": "d" * 40,
                    "merge_tree": "e" * 40, "tests": ["python -m unittest"],
                },
            )


if __name__ == "__main__":
    unittest.main()
