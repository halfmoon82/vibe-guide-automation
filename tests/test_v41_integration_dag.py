import unittest

from vibe_guide.dag import (
    append_integration_review_node,
    audit_dag,
    is_integration_review_node,
    validate_integration_review_node,
)
from vibe_guide.models import DAGNode, Plan


def business_node(node_id, reviewer="reviewer-" + "{id}"):
    reviewer = reviewer.format(id=node_id)
    contract = {
        "input": "request",
        "output": "result",
        "error_behavior": "return an error",
        "acceptance_example": "accepted",
        "risk_tags": ["business"],
        "writer": "writer-" + node_id,
        "worktree": ".vibe/worktrees/" + node_id,
        "allowlist": ["vibe_guide/" + node_id + ".py"],
        "reviewer": reviewer,
    }
    return DAGNode(
        node_id, node_id, [], [], "business", contract, "accepted",
        writer="writer-" + node_id, worktree=".vibe/worktrees/" + node_id,
        allowlist=["vibe_guide/" + node_id + ".py"], reviewer=reviewer,
    )


def complex_plan(nodes=None):
    nodes = list(nodes or [business_node("a"), business_node("b")])
    return Plan(
        "p", 1, "prd.md", [node.id for node in nodes], "authorized",
        spec_path="spec.md", complexity_band="complex", nodes=nodes,
        integration_contract={
            "iteration_context": {"kind": "iteration", "based_on": ["V4"]},
            "compatibility_scope": ["V4 API"],
            "agentsmd_acceptance_refs": ["AGENTS.md#8"],
            "integration_acceptance_contract": {"checks": ["all"]},
            "unverified_or_excluded": ["real provider lifecycle"],
        },
    )


class IntegrationDagTests(unittest.TestCase):
    def test_complex_plan_appends_exactly_one_full_dependency_read_only_node(self):
        plan = append_integration_review_node(complex_plan())
        nodes = [node for node in plan.nodes if is_integration_review_node(node)]
        self.assertEqual(len(nodes), 1)
        integration = nodes[0]
        self.assertEqual(integration.id, "integration-review")
        self.assertEqual(integration.depends_on, ["a", "b"])
        self.assertEqual(integration.owned_paths, [])
        self.assertEqual(integration.allowlist, [])
        self.assertEqual(integration.reviewer, "integration-reviewer")
        self.assertEqual(validate_integration_review_node(plan).valid, True)

    def test_simple_and_light_plans_are_unchanged(self):
        for band in ("simple", "light_plan"):
            node = business_node("a")
            plan = Plan("p", 1, "prd.md", ["a"], "authorized", complexity_band=band, nodes=[node])
            result = append_integration_review_node(plan)
            self.assertEqual([n.id for n in result.nodes], ["a"])

    def test_duplicate_missing_dependency_and_reused_reviewer_are_rejected(self):
        plan = append_integration_review_node(complex_plan())
        with self.assertRaises(ValueError):
            append_integration_review_node(plan)

        integration = next(n for n in plan.nodes if is_integration_review_node(n))
        integration.depends_on = ["a"]
        self.assertFalse(validate_integration_review_node(plan).valid)

        integration.depends_on = ["a", "b"]
        integration.reviewer = "reviewer-a"
        self.assertFalse(validate_integration_review_node(plan).valid)

    def test_audit_blocks_complex_plan_without_integration_node(self):
        plan = complex_plan()
        result = audit_dag(plan)
        self.assertEqual(result.status, "blocked_dag")
        self.assertTrue(any("exactly one integration" in reason for reason in result.reasons["__dag__"]))

    def test_audit_rejects_inconsistent_reviewer_provenance(self):
        plan = append_integration_review_node(complex_plan())
        integration = next(node for node in plan.nodes if is_integration_review_node(node))
        integration.contract["reviewer"] = "forged-reviewer"
        result = audit_dag(plan)
        self.assertEqual(result.status, "blocked_dag")
        self.assertTrue(any("reviewer" in reason and "mismatch" in reason for reason in result.reasons[integration.id]))


if __name__ == "__main__":
    unittest.main()
