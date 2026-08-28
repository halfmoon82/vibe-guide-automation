import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli


class DeliveryCliTests(unittest.TestCase):
    def test_help_and_module_metadata_are_available(self):
        result = run_cli(["--help"], Path.cwd())
        self.assertEqual(result.exit_code, 0)
        self.assertIn("usage: vibe", result.text)

    def test_scan_doctor_are_read_only_json_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            for command in ("scan", "doctor"):
                result = run_cli([command, "--json"], root)
                self.assertEqual(result.exit_code, 0)
                self.assertEqual(result.payload["command"], command)
                json.dumps(result.payload)
            after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
            self.assertEqual(before, after)

    def test_init_requires_confirmation_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            blocked = run_cli(["init", "--json"], root)
            self.assertEqual(blocked.exit_code, 3)
            self.assertFalse((root / ".vibe").exists())
            first = run_cli(["init", "--confirm", "--json"], root)
            second = run_cli(["init", "--confirm", "--json"], root)
            self.assertEqual(first.exit_code, second.exit_code, 0)
            self.assertTrue(first.payload["changed"])
            self.assertFalse(second.payload["changed"])
            self.assertTrue((root / ".vibe" / "state.json").exists())

    def test_fake_local_runner_flow_is_resumable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(run_cli(["init", "--confirm"], root).exit_code, 0)
            plan = run_cli(["plan", "--request", "local fake flow", "--plan-id", "demo", "--json"], root)
            self.assertEqual(plan.exit_code, 0)
            started = run_cli(["monitor", "--plan", "demo", "--authorize", "AUTHORIZE", "--json"], root)
            self.assertEqual(started.exit_code, 0)
            self.assertEqual(started.payload["status"], "delivered")
            resumed = run_cli(["resume", "--plan", "demo", "--json"], root)
            self.assertEqual(resumed.exit_code, 0)
            self.assertEqual(resumed.payload["status"], "accepted")

