import unittest

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.dag import append_integration_review_node
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


def _plan():
    nodes = [
        DAGNode("a", "A", [], [], "g", {"worker": "wa", "reviewer": "ra", "files": ["a.py"]}, "accepted"),
        DAGNode("b", "B", ["a"], [], "g", {"worker": "wb", "reviewer": "rb", "files": ["b.py"]}, "planned"),
    ]
    return Plan(
        "engine-plan", 7, "prd.md", ["a", "b"], "authorized", spec_path="spec.md",
        complexity_band="complex", nodes=nodes,
        integration_contract={
            "iteration_context": {"kind": "iteration", "based_on": "V4"},
            "compatibility_scope": ["V4 API"],
            "agentsmd_acceptance_refs": ["AGENTS.md#8"],
            "integration_acceptance_contract": {"checks": ["all"]},
            "unverified_or_excluded": ["provider"],
        },
    )


class ExecutionEngineBindingTests(unittest.TestCase):
    def test_complex_authorization_binds_monitor_engine_and_dag_revision(self):
        plan = append_integration_review_node(_plan())
        card = build_authorization_card(
            plan,
            plan.nodes,
            AgentCapabilities("codex", False, False, False, False, False, "guide"),
            execution_engine="vibeguide_monitor",
            engine_mode="dag",
            engine_evidence_ref="engine-probe:fixture",
        )
        self.assertEqual(card.execution_engine, "vibeguide_monitor")
        self.assertEqual(card.engine_mode, "dag")
        self.assertEqual(card.engine_evidence_ref, "engine-probe:fixture")
        self.assertEqual(card.dag_revision, plan.version)
        self.assertEqual(card.engine_authorization_digest, card.digest)
        record = authorize(card, "AUTHORIZE")
        self.assertEqual(record.engine_authorization_digest, card.digest)
        self.assertEqual(record.dag_revision, plan.version)

    def test_complex_rejects_sdd_only_or_unverified_engine(self):
        plan = append_integration_review_node(_plan())
        capabilities = AgentCapabilities("codex", False, False, False, False, False, "guide")
        for engine, mode in (("sdd", "serial"), ("unknown", "dag")):
            with self.assertRaises(ValueError):
                build_authorization_card(plan, plan.nodes, capabilities, execution_engine=engine, engine_mode=mode, engine_evidence_ref="probe")

    def test_explicit_override_is_audited_but_not_monitor_guaranteed(self):
        plan = append_integration_review_node(_plan())
        card = build_authorization_card(
            plan, plan.nodes, AgentCapabilities("codex", False, False, False, False, False, "guide"),
            execution_engine="sdd", engine_mode="serial", engine_evidence_ref="override:1",
            explicit_execution_mode_override={
                "original_instruction": "run with SDD",
                "alternative_mode": "serial",
                "affected_nodes": ["a", "b"],
            },
        )
        self.assertEqual(card.explicit_execution_mode_override["alternative_mode"], "serial")


if __name__ == "__main__":
    unittest.main()
