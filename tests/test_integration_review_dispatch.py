"""The final integration review node must be dispatchable.

`append_integration_review_node` builds the node read-only: `allowlist` is
empty because it owns no files, it only aggregates everybody else's.  But the
monitor derives a worker profile from `contract["files"]` and
`validate_child_session_binding` rejects an empty allowlist, so the node can
never be handed to a reviewer session -- every complex run stalls with the two
business nodes `accepted` and this one still `planned`.  That is the last node
of every complex plan, so no complex run can reach `complete`.
"""
import re
import unittest
from pathlib import Path

from vibe_guide import monitor
from vibe_guide.authorization import _normalize_files, validate_runtime_contract
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
            "project_id": "probe-project",
            "files": list(files),
            "worktree": ".worktrees/" + node_id,
            "branch": "node/" + node_id,
        },
        "planned",
        owned_paths=list(files),
        allowlist=list(files),
    )


def _dispatch(contract):
    """Hand *contract* to the binding validator the way `_start_task` does.

    The profile literal lives inline in `Monitor._start_task`, so there is
    nothing to call.  Rather than copy it -- a copy stays green while the real
    derivation drifts, which is how a previous test in this repo went blind --
    read it out of the source and evaluate it against this contract.  A rename
    or a moved fallback breaks this helper loudly instead of silently.
    """
    source = (Path(monitor.__file__).read_text(encoding="utf-8"))
    match = re.search(
        r"profile_data = (\{\"worker\": str\(contract\.get.*?\})\n", source, re.S
    )
    if match is None:
        raise AssertionError(
            "Monitor._start_task no longer builds its fallback worker profile as a "
            "dict literal; update this helper to match the real derivation"
        )
    contract = dict(contract)
    # `_start_task` normalises the contract before deriving the profile.  This
    # step is why an absent `files` key is not a harmless default: it is filled
    # from `worker_profile.allowlist`, which is how a `"."` ends up somewhere
    # `validate_runtime_contract` refuses it.
    contract.setdefault(
        "files", list((contract.get("worker_profile") or {}).get("allowlist", []))
    )
    # The literal is only the fallback; a contract carrying its own profile uses
    # that one (`monitor.py`: `if not profile_data`).
    profile_data = contract.get("worker_profile") or eval(  # noqa: S307 - the package's own source
        match.group(1),
        {"str": str, "list": list},
        {"contract": contract, "node_id": INTEGRATION_REVIEW_NODE_ID},
    )
    validate_child_session_binding(
        "run-1", "1", "digest", INTEGRATION_REVIEW_NODE_ID, "reviewer",
        WorkerProfile(**profile_data),
    )


def _validate_runtime(contract):
    """Run the gate `_start_task` applies before it derives the profile.

    Separate from `_normalize_files` on `contract["files"]`: this one walks the
    whole contract, so it catches a `"."` that arrived by way of
    `worker_profile.allowlist`.
    """
    normalized = dict(contract)
    normalized.setdefault(
        "files", list((normalized.get("worker_profile") or {}).get("allowlist", []))
    )
    validate_runtime_contract(normalized, authorized_actions=[], authorized_files=[])


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
        _dispatch(self.integration_contract())

    def test_the_integration_node_carries_the_project_id(self):
        """Visible dispatch refuses a contract without one.

        `provider_action.task_binding` raises `visible provider contract
        requires project_id` before the session is created, so the node is
        rejected even once its `files` are right.  The business nodes get theirs
        from `complete_node_contracts`; this node is appended afterwards and
        never passes through there.
        """
        contract = self.integration_contract()
        self.assertEqual(contract.get("project_id"), "probe-project", contract.get("project_id"))

    def test_the_project_id_is_never_invented(self):
        """No business node carries one, so neither may this node.

        `node_spec` only fills `project_id` in when the attested capabilities
        actually reported one, and raises `project_id_unavailable` for visible
        routes otherwise.  Synthesising a value here would hand a session a
        project identity nobody verified -- the failure mode PR #41 fixed.
        """
        plan = Plan(
            "plan-4",
            1,
            "prd.md",
            ["a"],
            "draft",
            nodes=[_business_node("a", ["src/ok.ts"])],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(["a"]),
        )
        for node in plan.nodes:
            node.contract.pop("project_id", None)
        contract = next(
            n for n in append_integration_review_node(plan).nodes
            if n.id == INTEGRATION_REVIEW_NODE_ID
        ).contract
        self.assertNotIn("project_id", contract)

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
        """A spec naming no files is legal, and must not strand the node.

        `complete_node_contracts` tolerates a node without `files`, and business
        nodes survive it because their `worker_profile.allowlist` falls back to
        `["."]`.  The integration node has no `worker_profile`, so an empty
        union leaves it with the very `worker profile is required` refusal this
        module exists to prevent.

        `files` must be present and empty.  It cannot carry `"."`
        (`_normalize_files` rejects it, so the plan would not publish), and it
        cannot be absent either: `_start_task` setdefaults it from
        `worker_profile.allowlist`, so a missing key becomes `["."]` at dispatch
        and `validate_runtime_contract` refuses the node.
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
        self.assertEqual(contract.get("files"), [], "'.' in files would block publishing")
        _normalize_files(contract["files"], "contract.files")
        _validate_runtime(contract)
        _dispatch(contract)

    def test_the_review_scope_stays_publishable(self):
        """The union must survive the validator that gates publishing.

        Each business node's own `files` passes `_normalize_files`, but the
        union is a different list: it can exceed the 256-item cap, and it can
        hold two spellings of one path that the validator rejects as duplicates.
        Either one turns "the last node stalls" into "the plan never publishes",
        which is strictly worse and happens where the product path cannot act
        on it.
        """
        many = Plan(
            "plan-5",
            1,
            "prd.md",
            ["a", "b"],
            "draft",
            nodes=[
                _business_node("a", ["src/a%d.ts" % i for i in range(200)]),
                _business_node("b", ["src/b%d.ts" % i for i in range(200)]),
            ],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(["a", "b"]),
        )
        shared = Plan(
            "plan-6",
            1,
            "prd.md",
            ["a", "b"],
            "draft",
            nodes=[
                _business_node("a", ["src/shared.ts"]),
                _business_node("b", ["./src/shared.ts", "a/./b.ts"]),
            ],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(["a", "b"]),
        )
        for plan in (many, shared):
            contract = next(
                n for n in append_integration_review_node(plan).nodes
                if n.id == INTEGRATION_REVIEW_NODE_ID
            ).contract
            # The validator that publishing runs, called the same way.
            _normalize_files(contract.get("files", []), "contract.files")

    def test_the_review_scope_rejects_what_the_publish_validator_rejects(self):
        """`allowlist` is an unvalidated channel; the scope must clean it.

        `DAGNode.__post_init__` only checks that `allowlist` holds non-empty
        strings -- it never looks at the paths.  A backslash, a NUL or padding
        whitespace therefore reaches the union, and `_normalize_files` refuses
        the first two, so the plan does not publish.
        """
        node = DAGNode(
            "a", "a", [], [], "group-a",
            {
                "input": "a request",
                "output": "a change",
                "error_behavior": "a message",
                "acceptance_example": "click it",
                "adapter_id": "claude-code",
                "project_id": "probe-project",
            },
            "planned",
            owned_paths=[],
            allowlist=["src\\evil.ts", "src/a\x00b.ts", "  src/pad.ts  ", "src/ok.ts"],
        )
        plan = Plan(
            "plan-7", 1, "prd.md", ["a"], "draft", nodes=[node],
            spec_path="spec.md", complexity_band="complex",
            integration_contract=_integration_contract(["a"]),
        )
        contract = next(
            n for n in append_integration_review_node(plan).nodes
            if n.id == INTEGRATION_REVIEW_NODE_ID
        ).contract
        self.assertEqual(
            sorted(contract.get("files") or []),
            ["src/ok.ts", "src/pad.ts"],
            contract.get("files"),
        )
        _normalize_files(contract["files"], "contract.files")

    def test_an_oversized_scope_coarsens_instead_of_truncating(self):
        """Past the validator's bound the scope must stay complete.

        Cutting the list at 256 would leave a reviewer whose scope silently
        excludes real deliverables while still looking well-formed -- it could
        report P0-P2 cleared on files it was never shown.  Collapsing to the
        top-level directories is coarser but still covers everything.
        """
        plan = Plan(
            "plan-9",
            1,
            "prd.md",
            ["a", "b"],
            "draft",
            nodes=[
                _business_node("a", ["src/a%d.ts" % i for i in range(200)]),
                _business_node("b", ["lib/b%d.ts" % i for i in range(200)]),
            ],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(["a", "b"]),
        )
        scope = next(
            n for n in append_integration_review_node(plan).nodes
            if n.id == INTEGRATION_REVIEW_NODE_ID
        ).contract["files"]
        self.assertEqual(sorted(scope), ["lib", "src"], scope)

    def test_home_relative_paths_are_dropped(self):
        """`~/x` is not project-relative, and nothing downstream catches it.

        `normalize_project_path` keeps a leading `~` (it only rejects absolute
        and parent-escaping paths), and so does `_normalize_files`, so a scope
        carrying one would hand the reviewer a path outside the project.
        """
        node = DAGNode(
            "a", "a", [], [], "group-a",
            {
                "input": "a request",
                "output": "a change",
                "error_behavior": "a message",
                "acceptance_example": "click it",
                "adapter_id": "claude-code",
                "project_id": "probe-project",
                "files": ["~/.ssh/id_rsa", "src/ok.ts"],
            },
            "planned",
            owned_paths=[],
            allowlist=["~/secrets"],
        )
        plan = Plan(
            "plan-10", 1, "prd.md", ["a"], "draft", nodes=[node],
            spec_path="spec.md", complexity_band="complex",
            integration_contract=_integration_contract(["a"]),
        )
        scope = next(
            n for n in append_integration_review_node(plan).nodes
            if n.id == INTEGRATION_REVIEW_NODE_ID
        ).contract["files"]
        self.assertEqual(scope, ["src/ok.ts"], scope)

    def test_conflicting_project_ids_fail_closed(self):
        """Taking the first of two is worse than refusing.

        `complete_node_contracts` uses `setdefault`, so a spec that declares its
        own `project_id` on one node keeps it while the other gets the attested
        default -- divergence is reachable.  Routing the reviewer at one project
        while half the delivery lives in the other gives a reviewer that reports
        P0-P2 cleared on a diff it could not read.
        """
        plan = Plan(
            "plan-8",
            1,
            "prd.md",
            ["a", "b"],
            "draft",
            nodes=[
                _business_node("a", ["src/a.ts"]),
                _business_node("b", ["src/b.ts"]),
            ],
            spec_path="spec.md",
            complexity_band="complex",
            integration_contract=_integration_contract(["a", "b"]),
        )
        plan.nodes[1].contract["project_id"] = "other-project"
        with self.assertRaises(ValueError) as caught:
            append_integration_review_node(plan)
        self.assertIn("project", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
