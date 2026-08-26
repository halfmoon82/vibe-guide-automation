import json
import io
import shutil
import tempfile
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from vibe_guide.cli import _observed_adapter, main
from vibe_guide.adapters.task_provider import ProviderPending
from vibe_guide.paths import ProjectPaths


class CliContractTests(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "vibe_guide", *args], text=True, capture_output=True)

    def test_scan_json_succeeds(self):
        result = self.run_cli("scan", "--json")
        self.assertIn(result.returncode, (0, 3), result.stderr)
        self.assertIsInstance(json.loads(result.stdout), dict)

    def test_monitor_without_authorization_is_blocked_with_json(self):
        result = self.run_cli("monitor", "--json")
        self.assertEqual(result.returncode, 3)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "blocked")

    def test_missing_capability_observation_is_retry_pending(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            with self.assertRaises(ProviderPending):
                _observed_adapter(paths, "codex")

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

    def test_monitor_missing_capability_contract_is_unknown_not_unavailable(self):
        try:
            from vibe_guide.cli import run_cli
        except ImportError as error:
            self.fail("run_cli public entry point is missing: %s" % error)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            initialized = run_cli(["init", "--confirm", "--json"], root)
            self.assertEqual(initialized.exit_code, 0)
            (root / ".vibe" / "session-contract.json").unlink()
            result = run_cli(
                [
                    "monitor",
                    "--plan",
                    "missing-contract-plan",
                    "--authorize",
                    "AUTHORIZE",
                    "--json",
                ],
                root,
            )
            self.assertEqual(result.exit_code, 4)
            self.assertEqual(result.payload["status"], "blocked_unknown")
            self.assertIn("capability_contract_unknown", result.payload["reason"])

    def test_monitor_missing_contract_flag_is_still_unknown(self):
        from vibe_guide.cli import run_cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            (root / ".vibe").mkdir()
            (root / ".vibe" / "state.json").write_text(
                '{"workflow_version":2,"session_gate":"s0_required"}\n',
                encoding="utf-8",
            )
            result = run_cli(
                [
                    "monitor",
                    "--plan",
                    "missing-contract-plan",
                    "--authorize",
                    "AUTHORIZE",
                    "--json",
                ],
                root,
            )
            self.assertEqual(result.exit_code, 4)
            self.assertEqual(result.payload["status"], "blocked_unknown")
            self.assertIn("capability_contract_unknown", result.payload["reason"])

    def test_legacy_setup_metadata_exposes_real_name_version_and_console_entry(self):
        root = Path(__file__).resolve().parents[1]
        name = subprocess.run(
            [sys.executable, "setup.py", "--name"],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        version = subprocess.run(
            [sys.executable, "setup.py", "--version"],
            cwd=str(root),
            text=True,
            capture_output=True,
            check=True,
        ).stdout.strip()
        setup_text = (root / "setup.py").read_text(encoding="utf-8")

        self.assertEqual((name, version), ("vibe-guide", "0.1.0"))
        self.assertIn("vibe=vibe_guide.cli:main", setup_text)
        self.assertIn('python_requires=">=3.9"', setup_text)

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            environment = Path(temporary) / "venv"
            shutil.copytree(
                root,
                source,
                ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"),
            )
            subprocess.run(
                [sys.executable, "-m", "venv", str(environment)],
                check=True,
                text=True,
                capture_output=True,
            )
            installed = subprocess.run(
                [str(environment / "bin/python"), "setup.py", "develop"],
                cwd=str(source),
                text=True,
                capture_output=True,
            )
            self.assertEqual(installed.returncode, 0, installed.stderr)
            console = subprocess.run(
                [str(environment / "bin/vibe"), "--help"],
                cwd=str(source),
                text=True,
                capture_output=True,
            )
            self.assertEqual(console.returncode, 0, console.stderr)
            self.assertIn("monitor", console.stdout)

    def test_agents_contract_contains_visible_successor_recovery_rule(self):
        text = (Path(__file__).resolve().parents[1] / "AGENTS.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("visible successor", text)
        self.assertIn("原任务 aborted/archived", text)
        self.assertIn("冻结 HEAD", text)
