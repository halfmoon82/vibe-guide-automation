"""`vibe authorize` writes `task_workflow` into state.json; `init` and `scan`
must keep treating that project as a valid V4.2 project, the way the session
gate already does.  Three copies of the predicate drifted: the gate allowed the
evidence keys, the other two read them as an unknown legacy version."""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.initializer import init_project
from vibe_guide.paths import ProjectPaths
from vibe_guide.workflow_gate import V42_STATE, _V42_EVIDENCE_KEYS


def _authorized_state(evidence_key):
    state = dict(V42_STATE)
    state[evidence_key] = {"plan": {"task_id": "plan", "route": "complex", "nodes": [], "node_records": {}}}
    return state


class InitAndScanAcceptWorkflowEvidenceTests(unittest.TestCase):
    def test_init_reruns_on_a_project_that_recorded_workflow_evidence(self):
        for evidence_key in sorted(_V42_EVIDENCE_KEYS):
            with self.subTest(evidence_key=evidence_key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".vibe").mkdir()
                state = _authorized_state(evidence_key)
                (root / ".vibe" / "state.json").write_text(json.dumps(state), encoding="utf-8")
                (root / ".vibe" / "config.json").write_text("{}\n", encoding="utf-8")
                result = init_project(ProjectPaths.from_cwd(root), True)
                self.assertTrue(result.changed)
                # The evidence the run depends on must survive a re-init byte for byte.
                self.assertEqual(json.loads((root / ".vibe" / "state.json").read_text(encoding="utf-8")), state)

    def test_init_still_refuses_a_state_with_an_unknown_extra_key(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".vibe").mkdir()
            state = dict(V42_STATE, stray="value")
            (root / ".vibe" / "state.json").write_text(json.dumps(state), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "legacy version is unknown"):
                init_project(ProjectPaths.from_cwd(root), True)

    def test_scan_reads_a_project_that_recorded_workflow_evidence(self):
        from vibe_guide.cli import run_cli
        for evidence_key in sorted(_V42_EVIDENCE_KEYS):
            with self.subTest(evidence_key=evidence_key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".project-root").write_bytes(b"fixture\n")
                (root / ".vibe").mkdir()
                (root / ".vibe" / "state.json").write_text(json.dumps(_authorized_state(evidence_key)), encoding="utf-8")
                result = run_cli(["scan", "--json"], root)
                self.assertEqual(result.exit_code, 0, result.payload)
                self.assertEqual(result.payload["status"], "ok", result.payload)

    def test_scan_still_blocks_a_state_with_an_unknown_extra_key(self):
        from vibe_guide.cli import run_cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_bytes(b"fixture\n")
            (root / ".vibe").mkdir()
            (root / ".vibe" / "state.json").write_text(json.dumps(dict(V42_STATE, stray="value")), encoding="utf-8")
            result = run_cli(["scan", "--json"], root)
            self.assertEqual(result.payload["status"], "session_gate_blocked", result.payload)


if __name__ == "__main__":
    unittest.main()
