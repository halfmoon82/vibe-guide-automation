"""The final integration review node must be dispatchable.

`append_integration_review_node` builds the node read-only: `allowlist` is
empty because it owns no files, it only aggregates everybody else's.  But the
monitor derives a worker profile from `contract["files"]` and
`validate_child_session_binding` rejects an empty allowlist, so the node can
never be handed to a reviewer session -- every complex run stalls with the two
business nodes `accepted` and this one still `planned`.  That is the last node
of every complex plan, so no complex run can reach `complete`.
"""
import unittest

from vibe_guide.dag import append_integration_review_node
from vibe_guide.diagnostics import validate_child_session_binding
from vibe_guide.models import INTEGRATION_REVIEW_NODE_ID, DAGNode, Plan, WorkerProfile


def _business_node(node_id, files):
    return DAGNode(
        node_id,
        node_id.replace("-", " "),
        [],
        [],
        "group-a",
        {
            "input": "a request",
            "output": "a change",
            "error_behavior": "a message",
            "acceptance_example": "click it",
            "adapter_id": "claude-code",
            "files": list(files),
            "worktree": ".worktrees/" + node_id,
            "branch": "node/" + node_id,
        },
        "planned",
        owned_paths=list(files),
        allowlist=list(files),
    )


def _integration_contract(node_ids):
    """The five-part contract, shaped as `node_spec.derive_integration_contract`
    builds it on the product path -- `append_integration_review_node` validates
    it, so a plan without one cannot reach the code under test."""
    from vibe_guide.node_spec import ALWAYS_EXCLUDED_SCOPE
    return {
        "iteration_context": {"kind": "iteration", "plan_id": "plan-1",
                              "request_digest": "0" * 64},
        "compatibility_scope": list(node_ids),
        "agentsmd_acceptance_refs": ["AGENTS.md"],
        "integration_acceptance_contract": {
            "p0_p2_cleared": "verified_fact: P0/P1/P2 cleared",
            "full_diff_reviewed": "verified_fact: full diff reviewed",
            "prd_spec_matched": "verified_fact: delivery matches the spec",
        },
        "unverified_or_excluded": list(ALWAYS_EXCLUDED_SCOPE),
    }


class IntegrationReviewDispatchTests(unittest.TestCase):
    def plan(self):
        plan = Plan(
            "plan-1",
            1,
            "prd.md",
            ["date-range-filter", "export-button"],
            "draft",
            nodes=[
                _business_node("date-range-filter", ["src/components/DateRange.tsx"]),
                _business_node("export-button", ["src/pages/view.tsx", "src/utils/pdf.ts"]),
            ],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(
                ["date-range-filter", "export-button"]
            ),
        )
        return append_integration_review_node(plan)

    def integration_contract(self):
        plan = self.plan()
        node = next(n for n in plan.nodes if n.id == INTEGRATION_REVIEW_NODE_ID)
        return node.contract

    def test_the_integration_node_carries_a_review_scope(self):
        """It reviews every business node, so that union is its scope.

        The node owns no files of its own, which is why `allowlist` is empty on
        the DAGNode.  The contract still has to name what may be read, because
        that is what the dispatched reviewer session is scoped to.
        """
        contract = self.integration_contract()
        self.assertEqual(
            sorted(contract.get("files") or []),
            ["src/components/DateRange.tsx", "src/pages/view.tsx", "src/utils/pdf.ts"],
            contract.get("files"),
        )

    def test_the_integration_node_can_be_handed_to_a_reviewer_session(self):
        """The profile the monitor derives must pass the binding validator.

        This is the assertion that fails today: with no `files` on the
        contract, the monitor's fallback allowlist is empty and dispatch is
        refused with "worker profile is required", leaving the node `planned`
        forever.
        """
        contract = self.integration_contract()
        profile = WorkerProfile(
            worker="worker",
            model="default",
            reasoning="normal",
            fallbacks=[],
            selection_basis={
                "issue_complexity_ref": INTEGRATION_REVIEW_NODE_ID,
                "complexity_band": "standard",
                "risk_tags": [],
                "availability_evidence": "runtime",
            },
            writer=str(contract.get("worker", "writer")),
            worktree=str(contract.get("worktree", ".")),
            branch=str(contract.get("branch", "branch-" + INTEGRATION_REVIEW_NODE_ID)),
            allowlist=list(contract.get("files", [])),
        )
        validate_child_session_binding(
            "run-1", "1", "digest", INTEGRATION_REVIEW_NODE_ID, "reviewer", profile
        )

    def test_the_review_scope_stays_inside_the_project(self):
        """An aggregated scope must not smuggle in an absolute or parent path.

        The validator refuses those, so a business node naming one would make
        the integration node undispatchable again -- for a different reason and
        only on that project.
        """
        plan = Plan(
            "plan-2",
            1,
            "prd.md",
            ["a"],
            "draft",
            nodes=[_business_node("a", ["src/ok.ts", "/etc/passwd", "../outside.ts"])],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(["a"]),
        )
        contract = next(
            n for n in append_integration_review_node(plan).nodes
            if n.id == INTEGRATION_REVIEW_NODE_ID
        ).contract
        self.assertEqual(contract.get("files"), ["src/ok.ts"], contract.get("files"))

    def test_a_plan_whose_nodes_name_no_files_still_dispatches(self):
        """Falling back to the project root keeps the node reviewable.

        `node_spec` already does this for business nodes (`or ["."]`); the
        integration node is appended afterwards and never passes through there.
        """
        plan = Plan(
            "plan-3",
            1,
            "prd.md",
            ["a"],
            "draft",
            nodes=[_business_node("a", [])],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(["a"]),
        )
        contract = next(
            n for n in append_integration_review_node(plan).nodes
            if n.id == INTEGRATION_REVIEW_NODE_ID
        ).contract
        self.assertEqual(contract.get("files"), ["."])


if __name__ == "__main__":
    unittest.main()
