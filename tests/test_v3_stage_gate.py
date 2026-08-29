import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.models import Action, Phase, PRD
from vibe_guide.workflow_gate import (
    _published_reviewed,
    evaluate_planning_gate,
    legal_actions,
    next_phase,
)


class V3StageGateTests(unittest.TestCase):
    def test_status_evidence_requires_exact_terminal_status_and_review_marker(self):
        self.assertTrue(_published_reviewed("status: published\nreviewed: yes\n"))
        self.assertTrue(_published_reviewed("status: approved\n"))
        self.assertTrue(_published_reviewed({"status": "reviewed"}))
        self.assertTrue(_published_reviewed({"status": "published", "reviewed": True}))
        self.assertFalse(_published_reviewed({"status": "published"}))
        self.assertFalse(_published_reviewed(True))
        self.assertFalse(_published_reviewed([True]))
        for value in (
            "status: unapproved\n",
            "status: disapproved\n",
            "status: published\nbody says approved\n",
            "status: draft\nbody says approved\n",
            "approved text without a status field",
        ):
            with self.subTest(value=value):
                self.assertFalse(_published_reviewed(value))

    def test_phase_action_mapper_covers_every_transition_and_rejects_fast_forward(self):
        expected = (
            (Phase.PRD_APPROVED, Action.CONTINUE_PLANNING, Phase.SPEC_ISSUE_DAG),
            (Phase.SPEC_ISSUE_DAG, Action.CONTINUE_PLANNING, Phase.DEVELOPMENT_PLAN_CONFIRMATION),
            (Phase.DEVELOPMENT_PLAN_CONFIRMATION, Action.CONFIRM_PLAN, Phase.AUTHORIZATION),
            (Phase.AUTHORIZATION, Action.AUTHORIZE_EXECUTION, Phase.MONITOR),
        )
        for phase, action, target in expected:
            with self.subTest(phase=phase):
                self.assertEqual(next_phase(phase, action), target)
        wrong_actions = {
            Phase.PRD_APPROVED: Action.AUTHORIZE_EXECUTION,
            Phase.SPEC_ISSUE_DAG: Action.AUTHORIZE_EXECUTION,
            Phase.DEVELOPMENT_PLAN_CONFIRMATION: Action.CONTINUE_PLANNING,
            Phase.AUTHORIZATION: Action.CONFIRM_PLAN,
        }
        for phase, wrong_action in wrong_actions.items():
            with self.subTest(phase=phase, wrong_action=wrong_action):
                with self.assertRaises(ValueError):
                    next_phase(phase, wrong_action)
        with self.assertRaises(ValueError):
            next_phase(Phase.MONITOR, Action.CONTINUE_PLANNING)

    def test_plan_revision_must_match_approved_prd_and_all_planning_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "spec.md").write_text("状态：published\n审核：reviewed\n", encoding="utf-8")
            (root / "issue.md").write_text("状态：published\n审核：reviewed\n", encoding="utf-8")
            (root / "dag-audit.json").write_text(json.dumps({"status": "reviewed", "plan_revision": 2}), encoding="utf-8")
            (root / "plan-confirmation.json").write_text(json.dumps({"status": "confirmed", "plan_revision": 2}), encoding="utf-8")
            result = evaluate_planning_gate(
                PRD("Approved", "Objective", revision=3, status="approved"),
                {
                    "prd": {"status": "approved", "revision": 2},
                    "spec": "状态：published\n审核：reviewed\n",
                    "issue": "状态：published\n审核：reviewed\n",
                    "dag_audit": {"status": "reviewed", "plan_revision": 2},
                    "plan_confirmation": {"status": "confirmed", "plan_revision": 2},
                },
                plan_id="plan-1",
                plan_revision=3,
            )
        self.assertEqual(result.status, "planning_required")
        self.assertIn("prd.revision", result.missing)
        self.assertIn("dag-audit.plan_revision", result.missing)
        self.assertIn("plan-confirmation.plan_revision", result.missing)

    def test_explicit_unpublished_prd_artifact_cannot_be_overridden_by_approved_object(self):
        result = evaluate_planning_gate(
            PRD("Approved", "Objective", revision=3, status="approved"),
            {
                "prd": {"status": "draft", "revision": 3},
                "spec": "状态：published\n审核：reviewed\n",
                "issue": "状态：published\n审核：reviewed\n",
                "dag_audit": {"status": "reviewed", "plan_revision": 3},
                "plan_confirmation": {"status": "confirmed", "plan_revision": 3},
            },
            plan_id="plan-1",
            plan_revision=3,
        )
        self.assertEqual(result.status, "planning_required")
        self.assertIn("prd", result.missing)

    def test_approved_markdown_prd_is_valid_evidence_when_revision_matches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "prd.md").write_text("状态：approved\nRevision：3\n", encoding="utf-8")
            (root / "spec.md").write_text("状态：published\n审核：reviewed\n", encoding="utf-8")
            (root / "issue.md").write_text("状态：published\n审核：reviewed\n", encoding="utf-8")
            (root / "dag-audit.json").write_text(json.dumps({"status": "reviewed", "plan_revision": 3}), encoding="utf-8")
            (root / "plan-confirmation.json").write_text(json.dumps({"status": "confirmed", "plan_revision": 3}), encoding="utf-8")
            result = evaluate_planning_gate(
                PRD("Approved", "Objective", revision=3, status="approved"),
                root,
                plan_id="plan-1",
                plan_revision=3,
            )
        self.assertEqual(result.status, "ready_for_authorization")

    def test_prd_artifact_requires_explicit_revision_even_when_object_matches(self):
        result = evaluate_planning_gate(
            PRD("Approved", "Objective", revision=3, status="approved"),
            {
                "prd": {"status": "approved"},
                "spec": "状态：published\n审核：reviewed\n",
                "issue": "状态：published\n审核：reviewed\n",
                "dag_audit": {"status": "reviewed", "plan_revision": 3},
                "plan_confirmation": {"status": "confirmed", "plan_revision": 3},
            },
            plan_id="plan-1",
            plan_revision=3,
        )
        self.assertEqual(result.status, "planning_required")
        self.assertIn("prd.revision", result.missing)

    def test_planning_gate_serialization_retains_stage_handoff(self):
        result = evaluate_planning_gate(
            PRD("Approved", "Objective", status="approved"),
            {},
            plan_id="plan-1",
            plan_revision=1,
        )
        payload = result.to_dict()
        self.assertIsInstance(payload["handoff"], dict)
        self.assertEqual(payload["handoff"]["plan_revision"], 1)
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
