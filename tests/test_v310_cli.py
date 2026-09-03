import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli, run_install_or_upgrade


class V310CLITests(unittest.TestCase):
    def test_legacy_background_node_spec_without_project_id_remains_compatible(self):
        import shutil
        fixture = Path(__file__).parent / "fixtures" / "e2e-project"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            shutil.copyfile(fixture / "plan-source.json", root / "plan-source.json")
            self.assertEqual(run_cli(["init", "--confirm", "--json"], root).exit_code, 0)
            result = run_cli(["plan", "--request", "兼容旧后台节点", "--plan-id", "legacy", "--s1", "4,4,4,4,4", "--node-spec", "plan-source.json", "--json"], root)
            self.assertEqual(result.exit_code, 0, result.text)

    def test_install_entry_returns_state_machine_result_and_json_parity(self):
        with tempfile.TemporaryDirectory() as directory:
            request = {"operation": "install", "mode": "layered", "project_root": directory}
            interactive = run_install_or_upgrade(request, False)
            with tempfile.TemporaryDirectory() as second:
                json_result = run_install_or_upgrade({**request, "project_root": second}, True)
            for key in ("status", "phase", "version_after", "mode"):
                self.assertEqual(interactive[key], json_result[key])
            self.assertIn("evidence_refs", interactive)

    def test_upgrade_cli_is_user_safe_and_json_structured(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_cli(["upgrade", "--mode", "layered", "--json"], Path(directory))
            self.assertEqual(result.payload["command"], "upgrade")
            self.assertIn(result.payload["status"], {"complete", "blocked_unknown", "retry_pending", "blocked_invalid", "failed"})
            self.assertNotIn("worktree", result.text)


if __name__ == "__main__":
    unittest.main()
