import unittest
from types import SimpleNamespace

from vibe_guide.evidence import (
    evaluate_v41_closeout,
    validate_integration_review_evidence,
    record_integration_review,
)


def snapshot(integration_status="planned", evidence=None):
    nodes = {
        "a": {"status": "accepted", "contract_digest": "a" * 64},
        "b": {"status": "accepted", "contract_digest": "b" * 64},
        "integration-review": {
            "status": integration_status,
            "contract_digest": "c" * 64,
            "review_clearance": {"p0": 0, "p1": 0, "p2": 0},
            "evidence": [],
        },
    }
    return SimpleNamespace(
        run_id="run-1", plan_id="p", plan_version=3, status="running", nodes=nodes,
        authorization_digest="d" * 64, node_contract_digest="e" * 64,
        prd_digest="1" * 64, spec_digest="2" * 64,
        integration_review_evidence=evidence or {},
    )


def valid_evidence(s):
    return {
        "schema_version": 1,
        "run_id": "run-1",
        "plan_id": s.plan_id,
        "plan_revision": s.plan_version,
        "prd_digest": "1" * 64,
        "spec_digest": "2" * 64,
        "authorization_digest": s.authorization_digest,
        "node_contract_digest": "e" * 64,
        "aggregated_scope": {"nodes": ["a", "b"]},
        "iteration_compatibility": {"status": "verified", "evidence": ["release-v4"]},
        "agentsmd_acceptance_refs": [{"ref": "AGENTS.md#8", "evidence": "ok"}],
        "test_runtime_delivery": {"status": "verified", "evidence": ["tests"]},
        "unverified_or_excluded": ["provider lifecycle"],
        "findings": [],
        "clearance": {"p0": 0, "p1": 0, "p2": 0},
    }


class IntegrationCloseoutTests(unittest.TestCase):
    def test_local_acceptance_without_integration_stays_open(self):
        decision = evaluate_v41_closeout(snapshot())
        self.assertFalse(decision.allowed)

    def test_integration_findings_block_closeout(self):
        s = snapshot("accepted")
        e = valid_evidence(s)
        e["clearance"] = {"p0": 0, "p1": 1, "p2": 1}
        e["findings"] = [{"severity": "P1", "status": "open"}, {"severity": "P2", "status": "open"}]
        validate_integration_review_evidence(s, e)
        record_integration_review(s, e)
        self.assertFalse(evaluate_v41_closeout(s).allowed)

    def test_missing_or_unknown_evidence_fails_closed(self):
        s = snapshot("accepted")
        with self.assertRaises(ValueError):
            validate_integration_review_evidence(s, {"schema_version": 1})
        e = valid_evidence(s)
        e["iteration_compatibility"]["status"] = "unknown"
        with self.assertRaises(ValueError):
            validate_integration_review_evidence(s, e)

    def test_zero_clearance_and_matching_lineage_allows_complete(self):
        s = snapshot("accepted")
        e = valid_evidence(s)
        record_integration_review(s, e)
        decision = evaluate_v41_closeout(s)
        self.assertTrue(decision.allowed)
        self.assertEqual(s.integration_review_evidence["clearance"], {"p0": 0, "p1": 0, "p2": 0})

    def test_missing_current_prd_spec_lineage_blocks(self):
        s = snapshot("accepted")
        e = valid_evidence(s)
        record_integration_review(s, e)
        s.prd_digest = ""
        s.spec_digest = ""
        self.assertFalse(evaluate_v41_closeout(s).allowed)

    def test_open_finding_cannot_claim_zero_clearance(self):
        s = snapshot("accepted")
        e = valid_evidence(s)
        e["findings"] = [{"severity": "P1", "status": "open"}]
        with self.assertRaises(ValueError):
            validate_integration_review_evidence(s, e)

    def test_unknown_or_missing_finding_status_is_rejected(self):
        s = snapshot("accepted")
        for finding in ({"severity": "P1"}, {"severity": "P1", "status": "weird"}):
            e = valid_evidence(s)
            e["findings"] = [finding]
            with self.assertRaises(ValueError):
                validate_integration_review_evidence(s, e)

    def test_clearance_matches_open_finding_count_exactly(self):
        s = snapshot("accepted")
        e = valid_evidence(s)
        e["findings"] = [{"severity": "P1", "status": "open"}, {"severity": "P1", "status": "open"}]
        e["clearance"] = {"p0": 0, "p1": 1, "p2": 0}
        with self.assertRaises(ValueError):
            validate_integration_review_evidence(s, e)


if __name__ == "__main__":
    unittest.main()
