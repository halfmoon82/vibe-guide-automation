import json
import io
import tempfile
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

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

    def test_help_is_clean_success_output(self):
        result = self.run_cli("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: vibe", result.stdout)
        self.assertNotIn("参数错误", result.stdout)

    def test_run_cli_uses_explicit_cwd_for_real_scan(self):
        try:
            from vibe_guide.cli import run_cli
        except ImportError as error:
            self.fail("run_cli public entry point is missing: %s" % error)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_bytes(b"fixture\n")
            result = run_cli(["scan", "--json"], root)

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["command"], "scan")
        self.assertEqual(result.payload["report"]["root"], str(root.resolve()))

    def test_init_confirmation_and_all_command_output_modes_are_wired(self):
        try:
            from vibe_guide.cli import run_cli
        except ImportError as error:
            self.fail("run_cli public entry point is missing: %s" % error)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            before = sorted(path.relative_to(root) for path in root.rglob("*"))
            blocked = run_cli(["init", "--json"], root)
            self.assertEqual(blocked.exit_code, 3)
            self.assertEqual(before, sorted(path.relative_to(root) for path in root.rglob("*")))

            initialized = run_cli(["init", "--confirm", "--json"], root)
            repeated = run_cli(["init", "--confirm", "--json"], root)
            self.assertEqual((initialized.exit_code, repeated.exit_code), (0, 0))
            self.assertTrue(initialized.payload["changed"])
            self.assertFalse(repeated.payload["changed"])

            calls = (
                ["scan"],
                ["doctor"],
                ["plan", "--request", "修正标题错别字"],
                ["monitor"],
                ["status"],
                ["resume"],
            )
            for argv in calls:
                with self.subTest(argv=argv):
                    text_result = run_cli(argv, root)
                    json_result = run_cli(argv + ["--json"], root)
                    self.assertTrue(text_result.text.strip())
                    self.assertIsInstance(json_result.payload, dict)
                    json.dumps(json_result.payload)
