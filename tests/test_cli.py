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
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events


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

    def test_reconcile_requires_explicit_plan_run_and_evidence(self):
        result = self.run_cli("reconcile", "--json")
        self.assertEqual(result.returncode, 4)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["command"], "reconcile")
        self.assertEqual(payload["status"], "blocked_unknown")

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

    def test_apply_agentsmd_requires_confirmation_and_is_idempotent(self):
        try:
            from vibe_guide.cli import run_cli
        except ImportError as error:
            self.fail("run_cli public entry point is missing: %s" % error)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            original = "# Existing project rules\n\n- preserve this text\n"
            agents = root / "AGENTS.md"
            agents.write_text(original, encoding="utf-8")

            initialized = run_cli(["init", "--confirm", "--json"], root)
            self.assertEqual(initialized.exit_code, 0)
            self.assertEqual(agents.read_text(encoding="utf-8"), original)

            blocked = run_cli(["apply-agentsmd", "--json"], root)
            self.assertEqual(blocked.exit_code, 3)
            self.assertEqual(agents.read_text(encoding="utf-8"), original)

            applied = run_cli(
                ["apply-agentsmd", "--confirm", "--json"], root
            )
            self.assertEqual(applied.exit_code, 0)
            self.assertTrue(applied.payload["changed"])
            content = agents.read_text(encoding="utf-8")
            self.assertTrue(content.startswith(original))
            self.assertIn("Capability and Tool Truth", content)
            self.assertIn("不得根据记忆、README", content)

            repeated = run_cli(
                ["apply-agentsmd", "--confirm", "--json"], root
            )
            self.assertEqual(repeated.exit_code, 0)
            self.assertFalse(repeated.payload["changed"])
            self.assertEqual(agents.read_text(encoding="utf-8"), content)

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

    def test_resume_capability_digest_mismatch_is_unknown_without_design_invalidation(self):
        from vibe_guide.cli import run_cli

        fixture = Path(__file__).parent / "fixtures" / "e2e-project"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            shutil.copyfile(fixture / "plan-source.json", root / "plan-source.json")
            self.assertEqual(run_cli(["init", "--confirm", "--json"], root).exit_code, 0)
            planned = run_cli(
                [
                    "plan", "--request", "设计并实现两个契约兼容的并行节点并完成独立审查",
                    "--plan-id", "cap-mismatch", "--s1", "4,4,4,4,4",
                    "--node-spec", "plan-source.json", "--json",
                ],
                root,
            )
            self.assertEqual(planned.exit_code, 0, planned.text)
            started = run_cli(
                ["monitor", "--plan", "cap-mismatch", "--authorize", "AUTHORIZE", "--json"],
                root,
                runner=FakeRunner(),
            )
            self.assertEqual(started.exit_code, 0, started.text)
            save_contract(
                ProjectPaths(root),
                build_contract(
                    root,
                    provider="fake",
                    host_id="local",
                    facts={
                        "runtime.exec": {
                            "status": "verified_available",
                            "scope": "task",
                            "route": "functions.exec",
                            "evidence_ref": "live:capability-refresh",
                        }
                    },
                ),
            )

            resumed = run_cli(["resume", "--plan", "cap-mismatch", "--json"], root, runner=FakeRunner())

            self.assertEqual(resumed.exit_code, 4)
            self.assertEqual(resumed.payload["status"], "blocked_unknown")
            self.assertIn("capability_contract_unknown", resumed.payload["reason"])
            self.assertFalse(
                (root / ".vibe/plans/cap-mismatch/authorization-invalidated.json").exists()
            )

    def test_explicit_authorize_after_capability_unknown_reauthorizes_current_run(self):
        from vibe_guide.cli import run_cli

        fixture = Path(__file__).parent / "fixtures" / "e2e-project"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            shutil.copyfile(fixture / "plan-source.json", root / "plan-source.json")
            self.assertEqual(run_cli(["init", "--confirm", "--json"], root).exit_code, 0)
            planned = run_cli(
                [
                    "plan", "--request", "设计并实现两个契约兼容的并行节点并完成独立审查",
                    "--plan-id", "cap-route", "--s1", "4,4,4,4,4",
                    "--node-spec", "plan-source.json", "--json",
                ],
                root,
            )
            self.assertEqual(planned.exit_code, 0, planned.text)
            runner = FakeRunner()
            started = run_cli(
                ["monitor", "--plan", "cap-route", "--authorize", "AUTHORIZE", "--json"],
                root,
                runner=runner,
            )
            self.assertEqual(started.exit_code, 0, started.text)
            original_run_id = started.payload["run_id"]
            save_contract(
                ProjectPaths(root),
                build_contract(
                    root,
                    provider="fake",
                    host_id="local",
                    facts={
                        "runtime.exec": {
                            "status": "verified_available",
                            "scope": "task",
                            "route": "functions.exec",
                            "evidence_ref": "live:capability-reroute",
                        }
                    },
                ),
            )

            resumed = run_cli(["resume", "--plan", "cap-route", "--json"], root, runner=runner)
            self.assertEqual(resumed.exit_code, 4)
            self.assertEqual(resumed.payload["status"], "blocked_unknown")
            self.assertFalse(
                (root / ".vibe/plans/cap-route/authorization-invalidated.json").exists()
            )

            reauthorized = run_cli(
                ["monitor", "--plan", "cap-route", "--authorize", "AUTHORIZE", "--json"],
                root,
                runner=runner,
            )
            self.assertEqual(reauthorized.payload["run_id"], original_run_id)
            self.assertNotEqual(reauthorized.payload["status"], "blocked_unknown")
            events = load_events(ProjectPaths(root), original_run_id)
            self.assertEqual(
                sum(event["event"] == "authorization_reauthorized" for event in events),
                1,
            )
            self.assertFalse(
                (root / ".vibe/plans/cap-route/authorization-invalidated.json").exists()
            )

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

        self.assertEqual((name, version), ("vibe-guide", "4.1.0"))
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
