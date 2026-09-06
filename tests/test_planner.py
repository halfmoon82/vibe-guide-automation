import unittest
import json

from vibe_guide.planner import (
    DecisionCard,
    PRD,
    ProductQuestion,
    S1Score,
    TaskContext,
    approve_prd,
    classify_s0,
    create_decision_card,
    resolve_consistency,
    route_task,
    score_s1,
)
from vibe_guide.models import DAGNode, Plan


class PlannerTests(unittest.TestCase):
    def test_plan_roundtrip_preserves_v2_dag_nodes(self):
        contract = {
            "input": "request",
            "output": "result",
            "error_behavior": "return blocked_dag",
            "acceptance_examples": ["ready set is observable"],
            "risk_tags": ["scheduling"],
            "writer": "developer-v2-2",
            "worktree": ".vibe/worktrees/v2-2",
            "allowlist": ["vibe_guide/dag.py"],
        }
        original = Plan(
            "v2-plan", 2, "prd.md", ["V2-0", "V2-2"], "authorized",
            nodes=[
                DAGNode("V2-0", "baseline", [], [], "baseline", dict(contract), "accepted"),
                DAGNode("V2-2", "dag audit", ["V2-0"], ["V2-3"], "dag", dict(contract), "planned"),
            ],
        )
        restored = Plan.from_dict(json.loads(json.dumps(original.to_dict())))
        self.assertEqual([node.id for node in restored.nodes], ["V2-0", "V2-2"])
        self.assertEqual(restored.nodes[1].depends_on, ["V2-0"])
        self.assertEqual(restored.nodes[1].contract["risk_tags"], ["scheduling"])
    def test_obvious_one_step_request_is_simple(self):
        result = classify_s0("把 README 里的错别字改掉")
        self.assertTrue(result.simple)
        self.assertEqual(result.route, "simple")

    def test_multi_step_request_enters_s1(self):
        result = classify_s0("设计并实现一个支付系统，编写测试并部署")
        self.assertFalse(result.simple)
        self.assertTrue(result.needs_s1)

    def test_common_english_multi_step_requests_enter_s1(self):
        messages = (
            "implement a payment system and write tests",
            "fix the bug and add a regression test",
            "refactor module, migrate data, deploy",
        )

        for message in messages:
            with self.subTest(message=message):
                result = classify_s0(message)
                self.assertFalse(result.simple)
                self.assertTrue(result.needs_s1)

    def test_ordinary_one_step_english_requests_remain_simple(self):
        messages = (
            "fix the typo in README",
            "rename the account and profile labels",
        )

        for message in messages:
            with self.subTest(message=message):
                result = classify_s0(message)
                self.assertTrue(result.simple)
                self.assertFalse(result.needs_s1)

    def test_score_boundaries(self):
        for total, route in ((8, "simple"), (9, "light_plan"), (15, "light_plan"), (16, "complex")):
            score = S1Score(total=total, steps=1, domains=1, uncertainty=1, failure_cost=1, toolchain=1)
            self.assertEqual(route_task(score), route)

    def test_s1_score_keeps_five_dimensions_and_rationale(self):
        context = TaskContext(
            steps=3,
            domains=2,
            uncertainty=4,
            failure_cost=5,
            toolchain=1,
            rationale={"uncertainty": "外部接口尚未验证"},
        )
        score = score_s1(context)
        self.assertEqual(score.total, 15)
        self.assertEqual(
            (score.steps, score.domains, score.uncertainty, score.failure_cost, score.toolchain),
            (3, 2, 4, 5, 1),
        )
        self.assertEqual(score.rationale["uncertainty"], "外部接口尚未验证")

    def test_decision_card_is_plain_language_and_unresolved_by_default(self):
        card = create_decision_card(
            ProductQuestion(
                "使用实时数据还是脱敏样例？",
                ["实时数据", "脱敏样例"],
                "实时数据增加权限和隐私风险",
                recommendation="脱敏样例",
            )
        )
        rendered = card.render()
        self.assertEqual(card.status, "unresolved")
        for expected in ("使用实时数据还是脱敏样例？", "实时数据", "脱敏样例", "权限和隐私风险", "建议：脱敏样例", "状态：待决定"):
            self.assertIn(expected, rendered)

    def test_unresolved_product_decision_does_not_approve_prd(self):
        prd = PRD(title="示例", objective="目标")
        card = create_decision_card(ProductQuestion("实时数据还是脱敏样例？", ["实时数据", "脱敏样例"], "影响数据风险"))
        result = approve_prd(prd, [card])
        self.assertFalse(result.approved)
        self.assertNotEqual(result.prd.status, "approved")

    def test_resolved_product_decision_approves_prd(self):
        prd = PRD(title="示例", objective="目标")
        card = DecisionCard(
            "选哪种数据？",
            ["A", "B"],
            "影响范围",
            "A",
            status="approved",
            selected="A",
            field="data.source",
        )
        result = approve_prd(prd, [card])
        self.assertTrue(result.approved)
        self.assertEqual(result.prd.status, "approved")

    def test_bound_current_user_candidate_must_exist_in_persisted_approved_decisions(self):
        binding = {
            "schema_version": 1,
            "project_digest": "1" * 64,
            "plan_id": "plan-1",
            "plan_version": 1,
            "decision_digest": "2" * 64,
            "authorization_digest": "3" * 64,
            "issue_contract_digest": "4" * 64,
        }
        result = resolve_consistency(
            {
                "field": "naming",
                "action": "rework",
                "files": ["n1.py"],
                "candidates": [
                    {
                        "source": "current_user",
                        "value": "unapproved-name",
                        "binding": binding,
                    },
                    {"source": "implementation", "value": "stale-name"},
                ],
            },
            decisions=[{"status": "approved", "selected": "approved-name"}],
            issue_contract={"naming": "approved-name"},
            authorized_actions=["rework"],
            authorized_files=["n1.py"],
            expected_binding=binding,
        )

        self.assertIsNone(result)

        exact = resolve_consistency(
            {
                "field": "naming",
                "action": "rework",
                "files": ["n1.py"],
                "candidates": [
                    {
                        "source": "current_user",
                        "value": "approved-name",
                        "binding": binding,
                        "decision": {
                            "id": "decision-name",
                            "field": "naming",
                            "revision": 1,
                            "status": "approved",
                            "selected": "approved-name",
                        },
                    },
                    {"source": "implementation", "value": "stale-name"},
                ],
            },
            decisions=[
                {
                    "id": "decision-name",
                    "field": "naming",
                    "revision": 1,
                    "status": "approved",
                    "selected": "approved-name",
                },
                {
                    "id": "decision-database",
                    "field": "database.engine",
                    "revision": 2,
                    "status": "approved",
                    "selected": "postgres",
                },
            ],
            issue_contract={"naming": "approved-name"},
            authorized_actions=["rework"],
            authorized_files=["n1.py"],
            expected_binding=binding,
        )

        self.assertIsNotNone(exact)

    def test_approved_decision_reference_cannot_inject_value_into_another_field(self):
        binding = {
            "schema_version": 1,
            "project_digest": "1" * 64,
            "plan_id": "plan-1",
            "plan_version": 1,
            "decision_digest": "2" * 64,
            "authorization_digest": "3" * 64,
            "issue_contract_digest": "4" * 64,
        }
        result = resolve_consistency(
            {
                "field": "naming",
                "action": "rework",
                "files": ["n1.py"],
                "candidates": [
                    {
                        "source": "current_user",
                        "value": "postgres",
                        "binding": binding,
                        "decision": {
                            "id": "decision-database",
                            "field": "database.engine",
                            "revision": 2,
                            "status": "approved",
                            "selected": "postgres",
                        },
                    },
                    {"source": "implementation", "value": "stale-name"},
                ],
            },
            decisions=[
                {
                    "id": "decision-name",
                    "field": "naming",
                    "revision": 1,
                    "status": "approved",
                    "selected": "approved-name",
                },
                {
                    "id": "decision-database",
                    "field": "database.engine",
                    "revision": 2,
                    "status": "approved",
                    "selected": "postgres",
                },
            ],
            issue_contract={"naming": "approved-name"},
            authorized_actions=["rework"],
            authorized_files=["n1.py"],
            expected_binding=binding,
        )

        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
