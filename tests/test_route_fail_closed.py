"""Route classification decides which gates apply, so it must fail closed.

``verify_workflow`` reads the evidence route twice with two different defaults:
the node-sequence check reads ``workflow.get("route", "complex")`` while the
authorization check compares ``workflow.get("route") == "complex"``.  Evidence
that simply omits the key therefore satisfies the ten-node complex sequence
while skipping the proof of human authorization entirely -- the one node the
whole gate exists to require.

``required_workflow_nodes`` compounds it by treating everything that is not the
literal ``"complex"`` as the two-node route, so a typo, an empty string, or a
non-string silently downgrades a complex task to the shortest workflow instead
of failing.

``Plan`` completes the set: a plan may carry the reserved ``integration-review``
node while declaring a non-complex ``complexity_band``, and every complex gate
keyed on that band -- engine attestation, topology validation, authorization
binding -- then skips itself on a plan that is structurally complex.
"""

import unittest

from vibe_guide.dag import INTEGRATION_REVIEW_NODE_ID
from vibe_guide.models import Plan
from vibe_guide.planner import required_workflow_nodes
from vibe_guide.workflow_gate import REQUIRED_COMPLEX_WORKFLOW, verify_workflow


def _complete_evidence(task_id="fail-closed"):
    """Ten completed nodes that were never authorized by a human."""
    return {
        "task_id": task_id,
        "nodes": list(REQUIRED_COMPLEX_WORKFLOW),
        "node_records": {
            node_id: {
                "task_id": task_id, "node_id": node_id, "status": "completed",
                "input": {"request": node_id}, "output": {"result": node_id},
                "evidence": {"source": "fixture"}, "sequence": index + 1,
            }
            for index, node_id in enumerate(REQUIRED_COMPLEX_WORKFLOW)
        },
        "authorization_granted": False,
    }


class EvidenceRouteMustBeExplicitTests(unittest.TestCase):
    def test_evidence_without_a_route_is_not_complete(self):
        """Omitting ``route`` must not skip the authorization check.

        With the key absent the sequence check defaults to ``"complex"`` and
        passes, while the authorization check compares against ``"complex"`` and
        does not fire.  The result is ``complete`` for evidence whose own
        ``authorization_granted`` is ``False``.
        """
        evidence = _complete_evidence()
        self.assertNotIn("route", evidence)
        self.assertFalse(evidence["authorization_granted"])

        result = verify_workflow(evidence)

        self.assertNotEqual(
            result.get("status"), "complete",
            "evidence with no route and no authorization must not verify",
        )

    def test_an_explicit_complex_route_is_still_checked(self):
        """The same evidence, with the route spelled out, already blocks.

        This is the asymmetry: the only difference between blocked and complete
        is whether one key is present.
        """
        evidence = _complete_evidence()
        evidence["route"] = "complex"

        self.assertEqual(
            verify_workflow(evidence).get("status"), "blocked_by_required_node"
        )

    def test_an_unrecognised_route_is_not_complete(self):
        """A misspelled route must fail, not downgrade to two nodes."""
        for route in ("complexx", "COMPLEX", "unknown-route"):
            with self.subTest(route=route):
                evidence = _complete_evidence()
                evidence["route"] = route
                self.assertNotEqual(
                    verify_workflow(evidence).get("status"), "complete",
                    "route {!r} must not verify a complex node list".format(route),
                )


class UnrecognisedRoutesFailClosedTests(unittest.TestCase):
    def test_recognised_routes_keep_their_node_lists(self):
        self.assertEqual(
            required_workflow_nodes("complex"), list(REQUIRED_COMPLEX_WORKFLOW)
        )
        self.assertEqual(required_workflow_nodes("simple"), ["s0", "s1"])

    def test_every_route_the_planner_emits_is_recognised(self):
        """Whatever ``route_task`` produces must be a known route.

        Pinning this keeps the fail-closed check from rejecting a route the
        planner itself still emits.
        """
        from vibe_guide.planner import TaskContext, route_task

        for scores in ((1, 1, 1, 1, 1), (3, 3, 3, 3, 3), (5, 5, 5, 5, 5)):
            with self.subTest(scores=scores):
                route = route_task(TaskContext(*scores))
                self.assertTrue(required_workflow_nodes(route))

    def test_an_unrecognised_route_raises(self):
        """Silently returning the shortest workflow is the wrong default.

        A dropped or misspelled band made the authorization binding vacuous:
        two nodes were required, two nodes were present, and the eight nodes
        that carry the human decision were never expected in the first place.
        """
        # An empty string is a recognised simple route, so it is not listed
        # here: the rule rejects routes nothing emits, not legitimate ones.
        for route in ("complexx", "COMPLEX", None, 123, ("complex",)):
            with self.subTest(route=route):
                with self.assertRaises((TypeError, ValueError)):
                    required_workflow_nodes(route)


class IntegrationPlanMustDeclareItsBandTests(unittest.TestCase):
    def test_a_plan_carrying_the_reserved_node_must_be_complex(self):
        """A structurally complex plan must not declare itself otherwise.

        ``complexity_band`` gates engine attestation, topology validation and
        the authorization binding.  A plan holding the reserved aggregate-review
        node while declaring a blank band turns all three off at once.
        """
        # ``None`` is rejected earlier by the existing string type check, so the
        # band rule itself is exercised with the string bands a real plan uses.
        for band in ("", "simple", "standard"):
            with self.subTest(band=band):
                with self.assertRaises(ValueError):
                    Plan(
                        "fail-closed", 1, "prd.md",
                        ["alpha", "beta", INTEGRATION_REVIEW_NODE_ID],
                        "authorized", spec_path="spec.md", complexity_band=band,
                    )

    def test_a_complex_plan_carrying_the_reserved_node_is_accepted(self):
        plan = Plan(
            "fail-closed", 1, "prd.md",
            ["alpha", "beta", INTEGRATION_REVIEW_NODE_ID],
            "authorized", spec_path="spec.md", complexity_band="complex",
        )
        self.assertIn(INTEGRATION_REVIEW_NODE_ID, plan.node_ids)

    def test_an_ordinary_plan_is_unaffected(self):
        """The rule keys on the reserved node, so normal plans keep working."""
        for band in ("", "simple", "complex"):
            with self.subTest(band=band):
                plan = Plan(
                    "fail-closed", 1, "prd.md", ["alpha", "beta"], "authorized",
                    spec_path="spec.md", complexity_band=band,
                )
                self.assertEqual(plan.node_ids, ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
