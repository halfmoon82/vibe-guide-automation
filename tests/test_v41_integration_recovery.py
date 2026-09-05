import unittest
from types import SimpleNamespace

from vibe_guide.evidence import evaluate_v41_closeout, record_integration_review
from vibe_guide.monitor import Monitor


class IntegrationRecoveryTests(unittest.TestCase):
    def test_old_run_without_integration_node_is_read_only_compatible(self):
        s = SimpleNamespace(nodes={"a": {"status": "accepted"}}, integration_review_evidence={})
        self.assertTrue(evaluate_v41_closeout(s).allowed)

    def test_stale_revision_and_unknown_clearance_do_not_pass(self):
        s = SimpleNamespace(
            plan_id="p", plan_version=2, status="running",
            authorization_digest="d" * 64, node_contract_digest="n" * 64,
            nodes={"a": {"status": "accepted", "contract_digest": "a" * 64}, "integration-review": {"status": "accepted", "review_clearance": {"p0": 0, "p1": 0, "p2": 0}}},
            integration_review_evidence={},
        )
        self.assertFalse(evaluate_v41_closeout(s).allowed)

    def test_legacy_bypass_requires_strict_boolean_and_history_evidence(self):
        self.assertFalse(Monitor._legacy_run_allowed({"legacy_run": "false"}))
        self.assertFalse(Monitor._legacy_run_allowed({"legacy_run": 1}))
        self.assertFalse(Monitor._legacy_run_allowed({"legacy_run": True}))
        self.assertTrue(Monitor._legacy_run_allowed({"legacy_run": True, "legacy_evidence": {"source": "migration", "run_id": "old"}}))

    def test_legacy_evidence_tamper_is_detected(self):
        evidence = {"source": "migration", "run_id": "old"}
        digest = Monitor._legacy_evidence_digest(evidence)
        self.assertTrue(Monitor._legacy_binding_valid(True, evidence, digest))
        self.assertFalse(Monitor._legacy_binding_valid(True, {"source": "tampered", "run_id": "old"}, digest))
        self.assertFalse(Monitor._legacy_binding_valid(True, None, digest))

    def test_legacy_evidence_source_and_run_id_require_nonempty_strings(self):
        for evidence in (
            {"source": 1, "run_id": "old"},
            {"source": ["migration"], "run_id": "old"},
            {"source": "migration", "run_id": 2},
            {"source": "migration", "run_id": ["old"]},
            {"source": "", "run_id": "old"},
        ):
            self.assertFalse(Monitor._legacy_run_allowed({"legacy_run": True, "legacy_evidence": evidence}))

    def test_resume_lineage_drift_is_detected(self):
        s = SimpleNamespace(run_id="run-1", plan_id="p", plan_version=2,
            authorization_digest="d" * 64, node_contract_digest="e" * 64,
            prd_digest="1" * 64, spec_digest="2" * 64,
            nodes={"a": {"status": "accepted"}, "integration-review": {"status": "accepted"}},
            integration_review_evidence={})
        self.assertFalse(Monitor._lineage_matches_snapshot(s, "3" * 64, "2" * 64))


if __name__ == "__main__":
    unittest.main()
