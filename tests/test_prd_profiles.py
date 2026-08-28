import unittest

from vibe_guide.models import PRDCheckpoint, SkillProfile
from vibe_guide.planner import (
    ProductQuestion,
    TaskContext,
    evaluate_prd_checkpoints,
    select_prd_profiles,
)


class PRDProfilesContractTests(unittest.TestCase):
    def test_unresolved_product_choice_is_one_blocked_design_question(self):
        context = TaskContext(
            steps=3,
            domains=2,
            uncertainty=4,
            failure_cost=4,
            toolchain=2,
            rationale={
                "product_question": ProductQuestion(
                    "选择实时数据还是脱敏样例？",
                    ["实时数据", "脱敏样例"],
                    "影响隐私和权限",
                )
            },
        )

        checkpoints = evaluate_prd_checkpoints(context)

        unresolved = [item for item in checkpoints if item.status == "blocked_design"]
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0].kind, "decision_pending")
        self.assertEqual(unresolved[0].fields["question"], "选择实时数据还是脱敏样例？")
        self.assertEqual(unresolved[0].fields["required_user_action"], "answer_question")
        self.assertNotIn("spec", unresolved[0].fields)
        self.assertNotIn("issue", unresolved[0].fields)
        self.assertNotIn("dag", unresolved[0].fields)

    def test_mapping_product_choice_is_one_blocked_design_question(self):
        context = TaskContext(
            steps=2,
            domains=1,
            uncertainty=2,
            failure_cost=2,
            toolchain=1,
            rationale={
                "decision_pending": {
                    "question": "选择实时数据还是脱敏样例？",
                    "options": ["实时数据", "脱敏样例"],
                    "impact": "影响隐私和权限",
                }
            },
        )

        checkpoints = evaluate_prd_checkpoints(context)

        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0].status, "blocked_design")
        self.assertEqual(checkpoints[0].fields["question"], "选择实时数据还是脱敏样例？")

    def test_checkpoint_categories_retain_evidence_labels(self):
        context = TaskContext(
            steps=1,
            domains=1,
            uncertainty=1,
            failure_cost=1,
            toolchain=1,
            rationale={
                "framing": "verified_fact:目标已确认",
                "tradeoffs": "assumption:使用本地 fixture",
                "flow": "verified_fact:先规划再授权",
                "acceptance": "verified_fact:通过回归测试",
            },
        )

        checkpoints = evaluate_prd_checkpoints(context)

        self.assertEqual(len(checkpoints), 4)
        self.assertEqual(
            [item.evidence for item in checkpoints],
            [
                ["verified_fact"],
                ["assumption"],
                ["verified_fact"],
                ["verified_fact"],
            ],
        )

    def test_empty_checkpoint_evidence_requires_review(self):
        context = TaskContext(1, 1, 1, 1, 1, rationale={})

        checkpoints = evaluate_prd_checkpoints(context)

        self.assertTrue(checkpoints)
        self.assertTrue(all(item.status == "review_required" for item in checkpoints))

    def test_unverified_fact_does_not_satisfy_verified_fact_checkpoint(self):
        context = TaskContext(
            steps=1,
            domains=1,
            uncertainty=1,
            failure_cost=1,
            toolchain=1,
            rationale={
                "framing": "unverified_fact: source not checked",
                "solution_tradeoffs": "not_verified_fact: source not checked",
                "flow_rules": "unverified_fact, pending verification",
                "acceptance_handoff": "not_verified_fact",
            },
        )

        checkpoints = evaluate_prd_checkpoints(context)

        self.assertEqual(len(checkpoints), 4)
        self.assertTrue(all(item.status == "review_required" for item in checkpoints))
        self.assertTrue(all(item.evidence == [] for item in checkpoints))

    def test_mixed_verified_and_unverified_evidence_fails_closed(self):
        context = TaskContext(
            steps=1,
            domains=1,
            uncertainty=1,
            failure_cost=1,
            toolchain=1,
            rationale={
                "framing": "verified_fact: goal; unverified_fact: stale",
                "solution_tradeoffs": "verified_fact: tradeoff",
                "flow_rules": "verified_fact: flow",
                "acceptance_handoff": "verified_fact: acceptance",
            },
        )

        checkpoints = evaluate_prd_checkpoints(context)

        self.assertEqual(checkpoints[0].status, "review_required")
        self.assertEqual(checkpoints[0].evidence, [])

    def test_bare_open_question_becomes_one_fallback_question(self):
        context = TaskContext(
            steps=1,
            domains=1,
            uncertainty=1,
            failure_cost=1,
            toolchain=1,
            rationale={"framing": "open_question", "flow": "verified_fact: flow"},
        )

        checkpoints = evaluate_prd_checkpoints(context)

        self.assertEqual(len(checkpoints), 1)
        self.assertEqual(checkpoints[0].status, "blocked_design")
        self.assertEqual(checkpoints[0].fields["required_user_action"], "answer_question")
        self.assertTrue(checkpoints[0].fields["question"])
        self.assertNotEqual(checkpoints[0].fields["question"], "open_question")

    def test_approved_checkpoints_expose_continue_planning(self):
        context = TaskContext(
            steps=1,
            domains=1,
            uncertainty=1,
            failure_cost=1,
            toolchain=1,
            rationale={
                "verified_fact": "目标和验收标准已确认",
                "assumption": "使用本地 fixture",
                "decision": "采用脱敏样例",
                "process": "先规划再授权",
                "acceptance": "通过回归测试",
            },
        )

        checkpoints = evaluate_prd_checkpoints(context)

        self.assertTrue(checkpoints)
        self.assertTrue(all(item.status == "approved" for item in checkpoints))
        self.assertTrue(all(item.fields["required_user_action"] == "continue_planning" for item in checkpoints))

    def test_skill_selection_preserves_recheck_reference_without_installing(self):
        sha = "a" * 40
        candidate = SkillProfile(
            name="prd-discovery",
            source_url="https://github.com/example/prd-discovery",
            commit_sha=sha,
            license="MIT",
            selected_paths=["skills/prd-discovery"],
            status="candidate",
        )

        selected = select_prd_profiles({"prd-discovery": "select"}, [candidate])

        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].source_url, candidate.source_url)
        self.assertEqual(selected[0].commit_sha, sha)
        self.assertEqual(selected[0].license, "MIT")
        self.assertEqual(selected[0].status, "selected")
        self.assertNotIn("installed", selected[0].to_dict())

    def test_install_selection_does_not_claim_installation(self):
        candidate = SkillProfile(
            name="prd-critic",
            source_url="https://github.com/example/prd-critic",
            commit_sha="b" * 40,
            license="Apache-2.0",
            selected_paths=["skills/prd-critic"],
        )

        selected = select_prd_profiles({"prd-critic": "install"}, [candidate])

        self.assertEqual(selected[0].status, "selected")
        self.assertEqual(selected[0].installed_at, "")
        self.assertEqual(selected[0].install_time, None)
        self.assertEqual(selected[0].verification_status, "recheck_before_install")


if __name__ == "__main__":
    unittest.main()
