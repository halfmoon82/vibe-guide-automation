import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.models import PRD, StageHandoff
from vibe_guide.planner import build_stage_handoff
from vibe_guide.cli import run_cli


def _node_spec(**overrides):
    source = {
        "title": "示例计划",
        "objective": "验证 PRD 到计划的衔接",
        "capabilities": {
            "agent_id": "fixture-agent",
            "shell": True,
            "subprocess": True,
            "worktree": True,
            "background": True,
            "session_resume": True,
            "level": "background",
        },
        "decisions": [
            {
                "id": "decision-mode",
                "question": "如何执行？",
                "options": ["并行", "串行"],
                "impact": "影响执行顺序",
                "recommendation": "并行",
                "status": "approved",
                "selected": "并行",
                "field": "mode",
                "revision": 1,
            }
        ],
        "nodes": [
            {
                "id": "node",
                "title": "示例节点",
                "depends_on": [],
                "integration_after": [],
                "parallel_group": "default",
                "status": "planned",
                "contract": {
                    "input": "输入",
                    "output": "输出",
                    "error_behavior": "未知则阻塞",
                    "acceptance_example": "通过测试",
                    "adapter_id": "fixture-agent",
                    "command": ["python3", "-c", "pass"],
                    "provider": "fixture-agent",
                    "mode": "background",
                    "worker": "fixture-developer",
                    "reviewer_worker": "fixture-reviewer",
                    "worktree": ".",
                    "branch": "fixture/main",
                    "files": ["result.txt"],
                    "actions": ["develop", "test", "review"],
                },
            }
        ],
    }
    source.update(overrides)
    return source


def _run_plan(source):
    temporary = tempfile.TemporaryDirectory()
    root = Path(temporary.name)
    (root / ".project-root").write_text("fixture\n", encoding="utf-8")
    source_path = root / "node-spec.json"
    source_path.write_text(json.dumps(source), encoding="utf-8")
    result = run_cli(
        [
            "plan",
            "--request",
            "设计并实现一个复杂契约节点",
            "--plan-id",
            "prd-regression",
            "--s1",
            "4,4,4,4,4",
            "--node-spec",
            source_path.name,
            "--json",
        ],
        root,
    )
    return temporary, root, result


class StageHandoffContractTests(unittest.TestCase):
    def test_runtime_handoff_supports_auto_correction_and_retry_pending(self):
        for status in ("auto_corrected", "retry_pending"):
            handoff = StageHandoff(
                from_stage="monitor",
                from_status=status,
                to_stage="monitor",
                readiness="ready",
                evidence_refs=["run:r:event:1"],
                open_questions=[],
                required_user_action="none",
                prompt="继续当前任务",
                forbidden_automatic_actions=["expand_scope", "create_worker", "authorize", "deploy"],
            )
            restored = StageHandoff.from_dict(handoff.to_dict())
            self.assertEqual(restored.from_status, status)
            self.assertIn(status, handoff.render())

    def test_blocked_prd_handoff_asks_one_question_without_authorizing_or_spawning(self):
        handoff = build_stage_handoff(
            PRD(title="示例", objective="目标", revision=2, status="blocked_design"),
            open_questions=["选择数据来源", "确认回滚策略"],
            evidence_refs=["prd:example@2"],
        )

        self.assertEqual(handoff.from_stage, "prd")
        self.assertEqual(handoff.from_status, "blocked_design")
        self.assertEqual(handoff.to_stage, "spec_issue_dag")
        self.assertEqual(handoff.readiness, "blocked_design")
        self.assertEqual(handoff.required_user_action, "answer_question")
        self.assertEqual(len(handoff.open_questions), 1)
        self.assertIn("create_worker", handoff.forbidden_automatic_actions)
        self.assertIn("authorize", handoff.forbidden_automatic_actions)
        self.assertNotIn("authorized", handoff.render())

    def test_approved_prd_handoff_exposes_continue_planning_and_is_not_authorization(self):
        handoff = build_stage_handoff(
            PRD(title="示例", objective="目标", revision=3, status="approved"),
            open_questions=[],
            evidence_refs=["prd:example@3"],
        )

        self.assertEqual(handoff.readiness, "ready")
        self.assertEqual(handoff.required_user_action, "continue_planning")
        self.assertIn("Spec/Issue/DAG", handoff.prompt)
        self.assertIn("create_spec", handoff.forbidden_automatic_actions)
        self.assertIn("create_worker", handoff.forbidden_automatic_actions)
        self.assertFalse(handoff.authorizes)
        self.assertFalse(handoff.creates_worker)

    def test_blocked_factory_and_render_keep_prd_revision_visible(self):
        blocked = StageHandoff.for_blocked_prd(["prd:example@4"], "选择数据来源")
        self.assertEqual(blocked.to_stage, "spec_issue_dag")
        self.assertIn("prd:example@4", blocked.render())

        handoff = build_stage_handoff(
            PRD(title="示例", objective="目标", revision=7, status="approved"),
            open_questions=[],
            evidence_refs=["prd:example@7"],
        )
        self.assertIn("revision=7", handoff.render())

    def test_cli_top_level_product_question_returns_one_blocked_design_question(self):
        temporary, root, result = _run_plan(
            _node_spec(
                product_question={
                    "question": "选择实时数据还是脱敏样例？",
                    "options": ["实时数据", "脱敏样例"],
                    "impact": "影响隐私和权限",
                }
            )
        )
        try:
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["status"], "blocked_design")
            self.assertEqual(result.payload["question"], "选择实时数据还是脱敏样例？")
            self.assertEqual(len(result.payload["checkpoints"]), 1)
            self.assertIsNone(result.payload["downstream_artifact"])
            self.assertFalse((root / ".vibe" / "plans" / "prd-regression").exists())
        finally:
            temporary.cleanup()

    def test_cli_embedded_open_question_collapses_to_one_question(self):
        temporary, root, result = _run_plan(
            _node_spec(rationale={"acceptance": "open_question: confirm rollback"})
        )
        try:
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["status"], "blocked_design")
            self.assertEqual(result.payload["question"], "confirm rollback")
            self.assertEqual(len(result.payload["checkpoints"]), 1)
            self.assertEqual(result.payload["handoff"]["open_questions"], ["confirm rollback"])
            self.assertIsNone(result.payload["downstream_artifact"])
        finally:
            temporary.cleanup()

    def test_cli_review_required_prd_does_not_publish_plan(self):
        temporary, root, result = _run_plan(_node_spec(rationale={}))
        try:
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["status"], "review_required")
            self.assertEqual(result.payload["required_user_action"], "confirm_plan")
            self.assertIsNone(result.payload["downstream_artifact"])
            self.assertFalse((root / ".vibe" / "plans" / "prd-regression").exists())
        finally:
            temporary.cleanup()

    def test_cli_unverified_fact_does_not_publish_plan(self):
        temporary, root, result = _run_plan(
            _node_spec(
                rationale={
                    "framing": "unverified_fact: source not checked",
                    "solution_tradeoffs": "not_verified_fact: source not checked",
                    "flow_rules": "unverified_fact: pending verification",
                    "acceptance_handoff": "not_verified_fact",
                }
            )
        )
        try:
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["status"], "review_required")
            self.assertEqual(result.payload["required_user_action"], "confirm_plan")
            self.assertIsNone(result.payload["downstream_artifact"])
            self.assertFalse((root / ".vibe" / "plans" / "prd-regression").exists())
        finally:
            temporary.cleanup()

    def test_cli_mixed_verified_and_unverified_evidence_does_not_publish_plan(self):
        temporary, root, result = _run_plan(
            _node_spec(
                rationale={
                    "framing": "verified_fact: goal; unverified_fact: stale",
                    "solution_tradeoffs": "verified_fact: tradeoff",
                    "flow_rules": "verified_fact: flow",
                    "acceptance_handoff": "verified_fact: acceptance",
                }
            )
        )
        try:
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["status"], "review_required")
            self.assertIsNone(result.payload["downstream_artifact"])
            self.assertFalse((root / ".vibe" / "plans" / "prd-regression").exists())
        finally:
            temporary.cleanup()

    def test_cli_bare_open_question_returns_one_fallback_question(self):
        temporary, root, result = _run_plan(
            _node_spec(
                rationale={
                    "framing": "open_question",
                    "solution_tradeoffs": "verified_fact: tradeoff",
                    "flow_rules": "verified_fact: flow",
                    "acceptance_handoff": "verified_fact: acceptance",
                }
            )
        )
        try:
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["status"], "blocked_design")
            self.assertEqual(len(result.payload["checkpoints"]), 1)
            self.assertTrue(result.payload["question"])
            self.assertNotEqual(result.payload["question"], "open_question")
            self.assertIsNone(result.payload["downstream_artifact"])
        finally:
            temporary.cleanup()

    def test_cli_approved_plan_contains_readable_stage_handoff(self):
        temporary, root, result = _run_plan(
            _node_spec(
                rationale={
                    "framing": "verified_fact:目标已确认",
                    "solution_tradeoffs": "assumption:使用 fixture",
                    "flow_rules": "verified_fact:先规划再授权",
                    "acceptance_handoff": "verified_fact:通过回归测试",
                }
            )
        )
        try:
            self.assertEqual(result.exit_code, 0)
            handoff = result.payload["handoff"]
            self.assertEqual(handoff["from_stage"], "prd")
            self.assertEqual(handoff["from_status"], "approved")
            self.assertEqual(handoff["to_stage"], "spec_issue_dag")
            self.assertEqual(handoff["required_user_action"], "continue_planning")
            self.assertFalse(handoff["authorizes"])
            self.assertFalse(handoff["creates_worker"])
            self.assertIn("revision=1", result.payload["handoff_text"])
            self.assertTrue((root / ".vibe" / "plans" / "prd-regression").exists())
        finally:
            temporary.cleanup()

    def test_cli_rejects_invalid_skill_profile_reference(self):
        temporary, root, result = _run_plan(
            _node_spec(
                rationale={
                    "framing": "verified_fact:目标已确认",
                    "solution_tradeoffs": "assumption:使用 fixture",
                    "flow_rules": "verified_fact:先规划再授权",
                    "acceptance_handoff": "verified_fact:通过回归测试",
                },
                skill_profiles=[
                    {
                        "name": "prd-discovery",
                        "source_url": "http://evil/x",
                        "commit_sha": "short",
                        "license": "MIT",
                        "selected_paths": [],
                    }
                ],
            )
        )
        try:
            self.assertEqual(result.exit_code, 3)
            self.assertEqual(result.payload["status"], "blocked")
            self.assertIn("GitHub HTTPS URL", result.payload["reason"])
            self.assertFalse((root / ".vibe" / "plans" / "prd-regression").exists())
        finally:
            temporary.cleanup()


if __name__ == "__main__":
    unittest.main()
