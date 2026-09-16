"""Contract tests for the V4.5 `authorize` entry.

The entry closes the gap between a published complex plan and Monitor's
mandatory ten-node workflow evidence.  These tests pin the two properties that
make it safe: it refuses anything short of the exact token, and every node
record it writes is derived from a published artifact rather than assumed.
"""
import hashlib
import json
import unittest
from pathlib import Path

from vibe_guide.authorize_entry import (
    build_workflow_evidence,
    materialize_workflow_evidence,
    select_plan_workflow,
    verify_workflow_artifacts,
)
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.workflow_gate import REQUIRED_COMPLEX_WORKFLOW, require_v42_sdd_first, verify_workflow

from tests.support_v45_authorize import publish_complex_probe, publish_second_plan


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
        self.assertEqual(len(workflow["node_records"]), 10)
        for node_id, record in workflow["node_records"].items():
            with self.subTest(node=node_id):
                evidence = record["evidence"]
                self.assertTrue(evidence, "evidence must be non-empty")
                self.assertIn("verified_fact", evidence)
                # Unconditional: an assertion that only runs when the key
                # happens to be present passes vacuously and proves nothing.
                artifact = evidence.get("artifact")
                self.assertIsNotNone(artifact, "every record must name a source artifact")
                referenced = plan_root / artifact["ref"]
                self.assertTrue(referenced.is_file())
                self.assertEqual(
                    artifact["sha256"],
                    hashlib.sha256(referenced.read_bytes()).hexdigest(),
                )

    def test_s0_records_escalation_backed_by_the_published_band(self):
        workflow = build_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        record = workflow["node_records"]["s0"]
        # A complex plan must never be recorded as simple, and the claim has to
        # rest on the published band rather than on a re-run keyword screen.
        self.assertFalse(record["output"]["simple"])
        self.assertTrue(record["output"]["needs_s1"])
        self.assertEqual(record["evidence"]["complexity_band"], "complex")
        self.assertEqual(record["evidence"]["artifact"]["ref"], "plan.json")

    def test_editing_a_decision_after_publication_is_detected(self):
        plan_path = self.root / ".vibe" / "plans" / "probe-plan" / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        options = plan["decisions"][0]["options"]
        # Still an approved decision whose `selected` is one of the options, so
        # only a recomputed digest can catch it.
        plan["decisions"][0]["selected"] = next(x for x in options if x != plan["decisions"][0]["selected"])
        plan_path.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            build_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")

    def test_evidence_for_one_plan_does_not_authorize_another(self):
        publish_second_plan(self.root, "plan-b")
        materialize_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        state = json.loads((self.paths.vibe / "state.json").read_text(encoding="utf-8"))
        self.assertIsNotNone(select_plan_workflow(state, "probe-plan"))
        self.assertIsNone(select_plan_workflow(state, "plan-b"))

    def test_monitor_refuses_a_plan_authorized_by_another_plans_token(self):
        publish_second_plan(self.root, "plan-b")
        authorized = run_cli(["authorize", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"], self.root)
        self.assertEqual(authorized.payload["status"], "ok")
        stolen = run_cli(["monitor", "--json", "--plan", "plan-b", "--authorize", "AUTHORIZE"], self.root)
        self.assertEqual(stolen.payload["status"], "blocked_design")
        self.assertIn("required_workflow_blocked", stolen.payload["reason"])
        self.assertIsNone(stolen.payload.get("run_id"))

    def test_each_plan_keeps_its_own_evidence(self):
        publish_second_plan(self.root, "plan-b")
        materialize_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        materialize_workflow_evidence(self.paths, "plan-b", "AUTHORIZE")
        state = json.loads((self.paths.vibe / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(select_plan_workflow(state, "probe-plan")["task_id"], "probe-plan")
        self.assertEqual(select_plan_workflow(state, "plan-b")["task_id"], "plan-b")

    def test_editing_an_artifact_after_authorize_invalidates_the_evidence(self):
        workflow = build_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        verify_workflow_artifacts(self.paths, workflow)  # clean before the edit
        prd = self.root / ".vibe" / "plans" / "probe-plan" / "prd.md"
        prd.write_text(prd.read_text(encoding="utf-8") + "\n附加改动\n", encoding="utf-8")
        with self.assertRaises(PermissionError):
            verify_workflow_artifacts(self.paths, workflow)

    def test_monitor_refuses_stale_evidence_after_an_artifact_edit(self):
        run_cli(["authorize", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"], self.root)
        prd = self.root / ".vibe" / "plans" / "probe-plan" / "prd.md"
        prd.write_text(prd.read_text(encoding="utf-8") + "\n附加改动\n", encoding="utf-8")
        result = run_cli(["monitor", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"], self.root)
        self.assertNotEqual(result.payload.get("status"), "ok")
        self.assertIsNone(result.payload.get("run_id"))

    def test_reauthorizing_is_idempotent_for_an_unchanged_plan(self):
        first = materialize_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        second = materialize_workflow_evidence(self.paths, "probe-plan", "AUTHORIZE")
        self.assertEqual(first["node_records"], second["node_records"])

    def test_unreadable_state_is_reported_as_unknown_not_as_a_policy_block(self):
        state_path = self.paths.vibe / "state.json"
        state_path.write_text("{not json", encoding="utf-8")
        result = run_cli(["authorize", "--json", "--plan", "probe-plan", "--authorize", "AUTHORIZE"], self.root)
        # A corrupt state file is an environment fault, not a design change.
        self.assertEqual(result.payload["status"], "blocked_unknown")
        self.assertIn("state_unreadable", result.payload["reason"])

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
