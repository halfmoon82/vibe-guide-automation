import json
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout

from vibe_guide.cli import main


class CliContractTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "vibe_guide", *args], text=True, capture_output=True)

    def test_scan_json_succeeds(self):
        result = self.run_cli("scan", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)

    def test_monitor_without_authorization_is_blocked_with_json(self):
        result = self.run_cli("monitor", "--json")
        self.assertEqual(result.returncode, 3)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")

    def test_unauthorized_monitor_does_not_call_runner(self):
        class SpyRunner:
            def __init__(self):
                self.started = False

            def start(self, contract, worktree):
                self.started = True

        runner = SpyRunner()
        with redirect_stdout(io.StringIO()):
            code = main(["monitor"], runner=runner)
        self.assertEqual(code, 3)
        self.assertFalse(runner.started)

    def test_unknown_state_has_exit_code_four_and_json(self):
        result = self.run_cli("status", "--json")
        self.assertEqual(result.returncode, 4)
        self.assertEqual(json.loads(result.stdout)["status"], "unknown")

    def test_usage_error_has_exit_code_two(self):
        result = self.run_cli("not-a-command")
        self.assertEqual(result.returncode, 2)
