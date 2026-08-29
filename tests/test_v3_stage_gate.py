import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.models import Action, Phase, PRD
from vibe_guide.workflow_gate import (
    evaluate_planning_gate,
    legal_actions,
    next_phase,
)


class V3StageGateTests(unittest.TestCase):
    def test_unapproved_prd_never_exposes_continue_planning(self):
        with tempfile.TemporaryDirectory() as directory:
            result = evaluate_planning_gate(
                PRD("Draft", "Objective", status="draft"),
                directory,
                plan_id="plan-1",
                plan_revision=1,
            )
        self.assertEqual(result.status, "planning_required")
        self.assertNotEqual(result.handoff.required_user_action, "continue_planning")
        self.assertEqual(result.handoff.required_user_action, "answer_question")

    def test_junk_empty_and_partial_artifact_directories_never_unlock_authorization(self):
        for marker in ("junk", "empty", "partial"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                (root / "specs").mkdir()
                (root / "issues").mkdir()
                if marker == "junk":
                    (root / "specs" / "junk.txt").write_text("unrelated", encoding="utf-8")
                    (root / "issues" / "junk.txt").write_text("unrelated", encoding="utf-8")
                elif marker == "partial":
                    (root / "specs" / "node.md").write_text("状态：published\n", encoding="utf-8")
                (root / "dag-audit.json").write_text(json.dumps({"status": "reviewed", "plan_revision": 1}), encoding="utf-8")
                (root / "plan-confirmation.json").write_text(json.dumps({"status": "confirmed", "plan_revision": 1}), encoding="utf-8")
                result = evaluate_planning_gate(PRD("Approved", "Objective", status="approved"), root, "plan-1", 1)
                self.assertEqual(result.status, "planning_required")
                self.assertFalse(result.run_created)
                self.assertFalse(result.worker_created)

    def test_approved_prd_has_only_continue_planning(self):
        self.assertEqual(legal_actions(Phase.PRD_APPROVED), (Action.CONTINUE_PLANNING,))
        self.assertEqual(next_phase(Phase.PRD_APPROVED, Action.CONTINUE_PLANNING), Phase.SPEC_ISSUE_DAG)
        with self.assertRaises(ValueError):
            next_phase(Phase.PRD_APPROVED, Action.AUTHORIZE_EXECUTION)

    def test_incomplete_planning_artifacts_are_fail_closed_without_side_effects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = evaluate_planning_gate(
                PRD("Approved", "Objective", status="approved"),
                root,
                plan_id="plan-1",
                plan_revision=1,
                execution_context={"s1_complex": True, "git_actions": {"commit": "allowed", "push": "allowed", "create_mr": "allowed", "merge": "allowed", "deploy": "excluded"}},
            )
            self.assertEqual(result.status, "planning_required")
            self.assertTrue(result.missing)
            self.assertFalse(result.run_created)
            self.assertFalse(result.worker_created)
            self.assertTrue(result.handoff.context["s1_complex"])
            self.assertFalse(any(root.iterdir()))

    def test_real_audit_and_confirmation_unlock_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "spec.md").write_text("状态：published\n审核：reviewed\n", encoding="utf-8")
            (root / "issue.md").write_text("状态：published\n审核：reviewed\n", encoding="utf-8")
            (root / "dag-audit.json").write_text(json.dumps({"status": "reviewed", "plan_revision": 1}), encoding="utf-8")
            before = evaluate_planning_gate(PRD("Approved", "Objective", status="approved"), root, "plan-1", 1)
            self.assertEqual(before.status, "planning_required")
            self.assertIn("plan-confirmation", before.missing)
            (root / "plan-confirmation.json").write_text(json.dumps({"status": "confirmed", "plan_revision": 1}), encoding="utf-8")
            after = evaluate_planning_gate(PRD("Approved", "Objective", status="approved"), root, "plan-1", 1)
            self.assertEqual(after.status, "ready_for_authorization")


class V2ModelExportCompatibilityTests(unittest.TestCase):
    def test_issue_complexity_export_constructs_and_round_trips(self):
        from vibe_guide.models import IssueComplexity

        value = IssueComplexity("I-1", "spec:I-1", 2, 1, 1, 1, 1, "small", [], "simple", "evidence:I-1")
        self.assertEqual(IssueComplexity.from_dict(value.to_dict()), value)

    def test_local_model_export_constructs_and_round_trips(self):
        from vibe_guide.models import LocalModel

        value = LocalModel("codex", ["shell"], 16_000, ["normal", "deep"], True)
        self.assertEqual(LocalModel.from_dict(value.to_dict()), value)

    def test_deploy_manifest_export_constructs_and_round_trips(self):
        from vibe_guide.models import DeployManifest

        value = DeployManifest("staging", "a" * 40, ["./deploy"], [{"kind": "http"}], {"command": "./rollback"})
        self.assertEqual(DeployManifest.from_dict(value.to_dict()), value)
        self.assertEqual(len(value.digest), 64)

    def test_deploy_state_export_constructs_and_round_trips(self):
        from vibe_guide.models import DeployState

        value = DeployState("deploy_planned", "a" * 64, "staging", {"source": "test"})
        self.assertEqual(DeployState.from_dict(value.to_dict()), value)


class V2ModelValidationBoundaryTests(unittest.TestCase):
    def test_legacy_evidence_priority_keeps_authorization(self):
        from vibe_guide.models import EVIDENCE_PRIORITY, Plan

        self.assertEqual(EVIDENCE_PRIORITY[2], "authorization")
        self.assertEqual(Plan("plan-1", 1, "prd.md", ["node"], "draft").evidence_priority, list(EVIDENCE_PRIORITY))

    def test_worker_profile_rejects_empty_or_wrong_typed_fields_and_round_trips(self):
        from vibe_guide.models import WorkerProfile

        valid = WorkerProfile("worker", "model", "normal", [], {"source": "test"})
        self.assertEqual(WorkerProfile.from_dict(valid.to_dict()), valid)
        for kwargs in (
            {"worker": "", "model": "model", "reasoning": "normal", "fallbacks": [], "selection_basis": {}},
            {"worker": "worker", "model": "", "reasoning": "normal", "fallbacks": [], "selection_basis": {}},
            {"worker": "worker", "model": "model", "reasoning": "", "fallbacks": [], "selection_basis": {}},
            {"worker": "worker", "model": "model", "reasoning": "normal", "fallbacks": "bad", "selection_basis": {}},
            {"worker": "worker", "model": "model", "reasoning": "normal", "fallbacks": ["bad"], "selection_basis": {}},
            {"worker": "worker", "model": "model", "reasoning": "normal", "fallbacks": [], "selection_basis": "bad"},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises((TypeError, ValueError)):
                WorkerProfile(**kwargs)

    def test_dag_node_rejects_terminal_complete_and_duplicate_or_invalid_dependencies(self):
        from vibe_guide.models import DAGNode

        base = {"title": "node", "depends_on": [], "integration_after": [], "parallel_group": "g", "contract": {}, "status": "ready"}
        with self.assertRaises(ValueError):
            DAGNode(id="node", status="complete", **{key: value for key, value in base.items() if key != "status"})
        with self.assertRaises(ValueError):
            DAGNode(depends_on=["n", "n"], **{key: value for key, value in base.items() if key != "depends_on"}, id="node")
        with self.assertRaises(ValueError):
            DAGNode(depends_on=["bad id"], **{key: value for key, value in base.items() if key != "depends_on"}, id="node")

    def test_plan_rejects_invalid_status_and_evidence_priority(self):
        from vibe_guide.models import Plan

        with self.assertRaises(ValueError):
            Plan("plan-1", 1, "prd.md", ["node"], "unknown")
        with self.assertRaises(ValueError):
            Plan("plan-1", 1, "prd.md", ["node"], "draft", evidence_priority=["implementation"])

    def test_agent_capabilities_rejects_invalid_level(self):
        from vibe_guide.models import AgentCapabilities

        with self.assertRaises(ValueError):
            AgentCapabilities("codex", True, True, True, True, True, "invalid")


if __name__ == "__main__":
    unittest.main()
