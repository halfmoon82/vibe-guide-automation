import unittest

from vibe_guide.models import Plan, IntegrationAcceptanceContract
from vibe_guide.planner import build_integration_acceptance_contract, project_v41_integration_contract
from vibe_guide.contracts import IntegrationAcceptanceContract as ExportedContract


def complex_plan(contract=None):
    return Plan("v41", 1, "prd.md", ["N1"], "authorized", complexity_band="complex", integration_contract=contract or {})


def simple_plan():
    return Plan("simple", 1, "prd.md", ["N1"], "authorized", complexity_band="simple")


def light_plan():
    return Plan("light", 1, "prd.md", ["N1"], "authorized", complexity_band="light_plan")


def complete_integration_contract():
    return {
        "iteration_context": {"kind": "iteration", "based_on": ["V4"]},
        "compatibility_scope": ["V4 API", "V3.10 run artifacts"],
        "agentsmd_acceptance_refs": ["AGENTS.md#8", "AGENTS.md#9"],
        "integration_acceptance_contract": {"checks": ["cross-node contract", "P0-P2 clearance"]},
        "unverified_or_excluded": ["real provider lifecycle"],
    }


class IntegrationContractTests(unittest.TestCase):
    def test_complex_contract_requires_iteration_compatibility_agentsmd_and_unverified_fields(self):
        plan = complex_plan(contract={"iteration_context": "V4", "compatibility_scope": ["V4 API"]})
        with self.assertRaises(ValueError):
            build_integration_acceptance_contract(plan)

    def test_complex_contract_round_trips_all_five_fields_and_digest_inputs(self):
        contract = complete_integration_contract()
        decoded = IntegrationAcceptanceContract.from_dict(contract)
        self.assertEqual(decoded.to_dict(), contract)

    def test_simple_and_light_plan_do_not_require_integration_contract(self):
        for plan in (simple_plan(), light_plan()):
            self.assertEqual(project_v41_integration_contract(plan), None)

    def test_invalid_contract_types_empty_and_conflicts_are_blocked(self):
        cases = [
            {**complete_integration_contract(), "compatibility_scope": "V4 API"},
            {**complete_integration_contract(), "agentsmd_acceptance_refs": []},
            {**complete_integration_contract(), "compatibility_scope": ["V4 API", "V4 API"]},
            {**complete_integration_contract(), "unverified_or_excluded": ["V4 API"]},
        ]
        for payload in cases:
            with self.assertRaises((TypeError, ValueError)):
                build_integration_acceptance_contract(complex_plan(payload))

    def test_contract_is_deeply_immutable_and_digest_is_stable(self):
        decoded = IntegrationAcceptanceContract.from_dict(complete_integration_contract())
        digest = decoded.digest(prd_ref="prd.md", spec_ref="spec.md", plan_revision=1)
        with self.assertRaises((TypeError, AttributeError)):
            decoded.compatibility_scope.append("new")
        with self.assertRaises((TypeError, AttributeError)):
            decoded.iteration_context["based_on"].append("V3")
        self.assertEqual(decoded.digest(prd_ref="prd.md", spec_ref="spec.md", plan_revision=1), digest)

    def test_absolute_prd_spec_refs_are_rejected_without_collision(self):
        for path in ("/a/prd.md", "/b/prd.md"):
            with self.assertRaises(ValueError):
                build_integration_acceptance_contract(Plan("v41", 1, path, ["N1"], "authorized", spec_path="spec.md", complexity_band="complex", integration_contract=complete_integration_contract()))

    def test_digest_metadata_is_fail_closed_and_contract_is_reexported(self):
        self.assertIs(ExportedContract, IntegrationAcceptanceContract)
        payload = build_integration_acceptance_contract(Plan("v41", 1, "prd.md", ["N1"], "authorized", spec_path="spec.md", complexity_band="complex", integration_contract=complete_integration_contract()))
        for bad in (
            {**payload, "digest": None},
            {key: value for key, value in payload.items() if key != "digest"},
            {**payload, "digest_inputs": {"prd_ref": "/abs", "spec_ref": "spec.md", "plan_revision": 1}},
            {**payload, "digest_inputs": {"prd_ref": "prd.md", "spec_ref": "spec.md", "plan_revision": 0}},
        ):
            with self.assertRaises((TypeError, ValueError)):
                IntegrationAcceptanceContract.from_dict(bad)

    def test_builder_rejects_empty_or_non_relative_prd_spec_lineage(self):
        for prd_ref, spec_ref in (("prd.md", ""), ("", "spec.md"), ("../prd.md", "spec.md")):
            plan = Plan("v41", 1, prd_ref, ["N1"], "authorized", spec_path=spec_ref, complexity_band="complex", integration_contract=complete_integration_contract())
            with self.assertRaises(ValueError):
                build_integration_acceptance_contract(plan)


if __name__ == "__main__":
    unittest.main()
