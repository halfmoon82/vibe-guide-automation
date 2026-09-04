import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.installation import InstallStateMachine, PHASES, run_install, run_upgrade
from vibe_guide.models import InstallRequest, InstallResult
from vibe_guide.paths import ProjectPaths


class InstallationContractTests(unittest.TestCase):
    def test_state_machine_runs_both_modes_without_provider(self):
        for mode in ("layered", "bundled"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                result = InstallStateMachine().run(mode, directory)
                self.assertEqual(result.status, "complete")
                self.assertEqual(result.phase, "complete")
                self.assertEqual(result.mode, mode)
                self.assertEqual(result.evidence_refs, ["install:preflight", "install:probe:not_required", "install:authorize:not_required"])
                state = json.loads((Path(directory) / ".vibe/installation/state.json").read_text())
                self.assertEqual(state["phase_history"], list(PHASES))
                self.assertEqual(state["mode"], mode)

    def test_state_machine_failure_is_recoverable_and_json_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "target"
            target.write_text("file")
            result = InstallStateMachine().run("layered", target)
            self.assertEqual(result.status, "blocked_invalid")
            self.assertEqual(result.phase, "blocked")
            self.assertTrue(result.errors)
            self.assertEqual(json.loads(json.dumps(result.to_dict()))["status"], "blocked_invalid")

    def test_state_machine_maps_uncreatable_target_to_structured_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory) / "parent"
            parent.write_text("not a directory")
            result = InstallStateMachine().run("bundled", parent / "child")
            self.assertIn(result.status, {"blocked_invalid", "failed"})
            self.assertEqual(result.phase, "blocked")
            self.assertTrue(result.errors)
            self.assertEqual(json.loads(json.dumps(result.to_dict()))["phase"], "blocked")
    def test_request_modes_and_result_are_json_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            request = InstallRequest("layered", True, Path(directory))
            self.assertEqual(Path(request.to_dict()["project_root"]), Path(directory).resolve())
        result = InstallResult("blocked_unknown", "blocked", "unknown", "4.0.0", errors=["x"])
        self.assertEqual(json.loads(json.dumps(result.to_dict()))["status"], "blocked_unknown")

    def test_invalid_mode_and_status_rejected(self):
        with self.assertRaises(ValueError):
            InstallRequest("unsafe", False, Path.cwd())
        with self.assertRaises(ValueError):
            InstallResult("unavailable", "blocked", "none", "4.0.0")

    def test_install_persists_phase_and_does_not_write_before_callbacks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root)
            seen = []
            def probe(request):
                self.assertFalse((root / ".vibe" / "business.json").exists())
                seen.append("probe")
                return {"status": "verified", "runtime.exec": "available"}
            def authorize(request, capabilities):
                self.assertFalse((root / ".vibe" / "business.json").exists())
                seen.append("authorize")
                return {"status": "approved"}
            result = run_install(InstallRequest("layered", True, root), paths, authorize, probe)
            self.assertEqual(result.status, "complete")
            self.assertEqual(seen, ["probe", "authorize"])
            persisted = json.loads((root / ".vibe" / "installation" / "state.json").read_text())
            self.assertEqual(persisted["phase"], "complete")

    def test_stable_unknown_and_timeout_statuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); paths = ProjectPaths(root)
            request = InstallRequest("bundled", True, root)
            unknown = run_install(request, paths, lambda *_: {"status": "approved"}, lambda *_: {"status": "unknown"})
            self.assertEqual(unknown.status, "blocked_unknown")
            timeout = run_install(request, paths, lambda *_: {"status": "approved"}, lambda *_: {"status": "unknown_timeout"})
            self.assertEqual(timeout.status, "retry_pending")

    def test_upgrade_reads_version_and_records_migration_phase(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / ".vibe").mkdir()
            (root / ".vibe" / "config.json").write_text(json.dumps({"version": "2.0.0"}))
            result = run_upgrade(InstallRequest("layered", True, root), ProjectPaths(root), lambda *_: {"status": "approved"}, lambda *_: {"status": "verified"})
            self.assertEqual(result.version_before, "2.0.0")
            self.assertEqual(result.migration["target_version"], "4.0.0")


if __name__ == "__main__":
    unittest.main()
