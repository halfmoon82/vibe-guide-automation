"""Contract tests for the V4.5 `authorize` entry.

The entry closes the gap between a published complex plan and Monitor's
mandatory ten-node workflow evidence.  These tests pin the two properties that
make it safe: it refuses anything short of the exact token, and every node
record it writes is derived from a published artifact rather than assumed.
"""
import json
import unittest
from pathlib import Path

from vibe_guide.authorize_entry import build_workflow_evidence, materialize_workflow_evidence
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.workflow_gate import REQUIRED_COMPLEX_WORKFLOW, require_v42_sdd_first, verify_workflow

from tests.support_v45_authorize import publish_complex_probe


class AuthorizeEntryContract(unittest.TestCase):
    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)

    def test_exact_token_is_required(self):
        for token in ("", "authorize", "AUTHORIZE_LATER", "yes"):
            with self.subTest(token=token):
                with self.assertRaises(PermissionError):
                    build_workflow_evidence(self.paths, "probe-plan", token)

    def test_no_state_is_written_when_the_token_is_wrong(self):
        state_path = self.paths.vibe / "state.json"
        before = state_path.read_text(encoding="utf-8")
        with self.assertRaises(PermissionError):
            materialize_workflow_evidence(self.paths, "probe-plan", "nope")
        self.assertEqual(before, state_path.read_text(encoding="utf-8"))

    def test_records_all_ten_nodes_and_verifies_complete(self):
        workflow = build_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        self.assertEqual(workflow["nodes"], REQUIRED_COMPLEX_WORKFLOW)
        self.assertEqual(sorted(workflow["node_records"]), sorted(REQUIRED_COMPLEX_WORKFLOW))
        self.assertEqual(verify_workflow(workflow), {"status": "complete", "authorization_granted": True})

    def test_every_record_carries_evidence_traceable_to_an_artifact(self):
        workflow = build_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        plan_root = self.root / ".vibe" / "plans" / "probe-plan"
        for node_id, record in workflow["node_records"].items():
            with self.subTest(node=node_id):
                evidence = record["evidence"]
                self.assertTrue(evidence, "evidence must be non-empty")
                self.assertIn("verified_fact", evidence)
                artifact = evidence.get("artifact")
                if artifact is not None:
                    # A recorded digest must match the bytes actually on disk.
                    referenced = plan_root / artifact["ref"]
                    self.assertTrue(referenced.is_file())
                    import hashlib
                    self.assertEqual(
                        artifact["sha256"],
                        hashlib.sha256(referenced.read_bytes()).hexdigest(),
                    )

    def test_state_stays_v42_valid_after_materialization(self):
        materialize_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        state = json.loads((self.paths.vibe / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(state["workflow_version"], 4)
        self.assertEqual(state["execution_mode"], "sdd_first")
        self.assertIn("task_workflow", state)
        # The session gate must still accept the file it now shares with Monitor.
        require_v42_sdd_first(self.paths)

    def test_missing_artifact_blocks_instead_of_assuming(self):
        (self.root / ".vibe" / "plans" / "probe-plan" / "dag-audit.json").unlink()
        with self.assertRaises(PermissionError):
            build_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")

    def test_tampered_decision_blocks(self):
        plan_path = self.root / ".vibe" / "plans" / "probe-plan" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        plan["decisions"][0]["status"] = "unresolved"
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            build_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")

    def test_cli_exposes_authorize_and_reports_the_recorded_nodes(self):
        result = run_cli(["authorize", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"], self.root)
        self.assertEqual(result.payload["status"], "ok")
        self.assertEqual(result.payload["recorded_nodes"], REQUIRED_COMPLEX_WORKFLOW)
        self.assertTrue(result.payload["authorization_granted"])

    def test_cli_authorize_blocks_without_the_exact_token(self):
        result = run_cli(["authorize", "--json", "--plan", "probe-plan"], self.root)
        self.assertEqual(result.payload["status"], "blocked")
        state = json.loads((self.paths.vibe / "state.json").read_text(encoding="utf-8"))
        self.assertNotIn("task_workflow", state)


if __name__ == "__main__":
    unittest.main()
