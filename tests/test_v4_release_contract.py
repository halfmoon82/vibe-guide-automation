import json
import re
import tempfile
import unittest
from pathlib import Path

import vibe_guide
from vibe_guide import installation
from vibe_guide.migration import TARGET_VERSION, migrate_v2_to_v310
from vibe_guide.models import InstallRequest
from vibe_guide.paths import ProjectPaths


ROOT = Path(__file__).resolve().parents[1]


class V4ReleaseContractTests(unittest.TestCase):
    @property
    def dependencies(self):
        return getattr(installation, "SDD_SKILL_DEPENDENCIES", [])

    def test_v4_version_is_consistent_across_package_and_migration(self):
        self.assertEqual(vibe_guide.__version__, "4.0.0")
        self.assertEqual(TARGET_VERSION, "4.0.0")
        setup = (ROOT / "setup.py").read_text(encoding="utf-8")
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertRegex(setup, r'version\s*=\s*["\']4\.0\.0["\']')
        self.assertRegex(pyproject, r'(?ms)^\[project\].*?^version\s*=\s*["\']4\.0\.0["\']')

    def test_sdd_dependencies_are_fixed_provider_managed_records(self):
        names = [item["name"] for item in self.dependencies]
        for expected in (
            "subagent-driven-development",
            "test-driven-development",
            "writing-plans",
            "verification-before-completion",
        ):
            self.assertIn(expected, names)
        for item in self.dependencies:
            self.assertTrue(item["required"])
            self.assertEqual(item["provider"], "provider-managed")
            self.assertEqual(item["status"], "unknown")

    def test_install_writes_v4_config_and_sdd_dependency_declarations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = installation.InstallStateMachine().run("layered", root)
            self.assertEqual(result.status, "complete")
            config_path = root / ".vibe" / "config.json"
            state_path = root / ".vibe" / "installation" / "state.json"
            self.assertTrue(config_path.is_file())
            self.assertTrue(state_path.is_file())
            config = json.loads(config_path.read_text(encoding="utf-8"))
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(config.get("version"), "4.0.0")
            self.assertEqual(config.get("workflow_version"), 4)
            self.assertEqual(config.get("execution_mode"), "sdd_first")
            self.assertEqual(config.get("required_skills"), list(self.dependencies))
            self.assertEqual(state.get("required_skills"), list(self.dependencies))

    def test_upgrade_accepts_v310_source_and_targets_v4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "config.json").write_text(
                json.dumps({"version": "3.10.0", "workflow_version": 3.10}),
                encoding="utf-8",
            )
            result = installation.run_upgrade(
                InstallRequest("bundled", True, root),
                ProjectPaths(root),
                lambda *_: {"status": "approved"},
                lambda *_: {"status": "verified"},
            )
            self.assertEqual(result.status, "complete")
            self.assertEqual(result.version_before, "3.10.0")
            self.assertEqual(result.version_after, "4.0.0")
            config = json.loads((root / ".vibe" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config.get("version"), "4.0.0")
            self.assertEqual(config.get("workflow_version"), 4)
            self.assertEqual(config.get("required_skills"), list(self.dependencies))

    def test_upgrade_accepts_v200_source_and_targets_v4(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vibe").mkdir()
            (root / ".vibe" / "config.json").write_text(
                json.dumps({"version": "2.0.0", "workflow_version": 2}),
                encoding="utf-8",
            )
            result = installation.run_upgrade(
                InstallRequest("layered", True, root),
                ProjectPaths(root),
                lambda *_: {"status": "approved"},
                lambda *_: {"status": "verified"},
            )
            self.assertEqual(result.status, "complete")
            self.assertEqual(result.version_before, "2.0.0")
            self.assertEqual(result.version_after, "4.0.0")
            config = json.loads((root / ".vibe" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config.get("version"), "4.0.0")
            self.assertEqual(config.get("required_skills"), list(self.dependencies))

    def test_backup_first_migration_accepts_v310_source_directly(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "v310"
            (source / ".vibe").mkdir(parents=True)
            (source / ".vibe" / "config.json").write_text(
                json.dumps({"version": "3.10.0", "workflow_version": 3.10, "custom": "keep"}),
                encoding="utf-8",
            )
            (source / ".vibe" / "state.json").write_text(
                json.dumps({"workflow_version": 3.10, "session_gate": "s0_required"}),
                encoding="utf-8",
            )
            destination = root / "v4"
            result = migrate_v2_to_v310(source, destination)
            self.assertEqual(result.status, "migrated")
            self.assertEqual(result.source_version, "3.10.0")
            self.assertEqual(result.target_version, "4.0.0")
            config = json.loads((destination / ".vibe" / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(config["version"], "4.0.0")
            self.assertEqual(config["workflow_version"], 4)
            self.assertEqual(config["execution_mode"], "sdd_first")
            self.assertEqual(config["required_skills"], list(self.dependencies))
            self.assertEqual(config["custom"], "keep")


if __name__ == "__main__":
    unittest.main()
