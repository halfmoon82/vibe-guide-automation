import unittest
from vibe_guide.cli import render_v41_closeout_status
from vibe_guide.evidence import build_integration_review_evidence
from vibe_guide.state import RunSnapshot


class V41CliTests(unittest.TestCase):
    def snap(self, status="running", nodes=None, evidence=None):
        return RunSnapshot(
            "r", "p", 1, status, nodes or {}, {},
            authorization_digest="a" * 64, node_contract_digest="b" * 64,
            prd_digest="c" * 64, spec_digest="d" * 64,
            integration_review_evidence=evidence or {},
        )

    def accepted_snapshot(self):
        """A snapshot carrying the package the validator actually accepts.

        Its key set is exact, so the `status`/`p0_p2` shape this test used to
        pass in could never reach the renderer from a real run.
        """
        snapshot = self.snap(status="complete", nodes={"a": {"status": "accepted"}, "integration-review": {"status": "accepted"}})
        snapshot.integration_review_evidence = build_integration_review_evidence(
            snapshot,
            {"findings": [], "out_of_scope": [],
             "iteration_compatibility": {"status": "verified", "evidence": "checked"},
             "test_runtime_delivery": {"status": "verified", "evidence": "checked"}},
            agentsmd_acceptance_refs=["AGENTS.md"], unverified_or_excluded=["deploy"],
        )
        return snapshot

    def test_status_distinguishes_integration_phases(self):
        self.assertIn("局部节点完成", render_v41_closeout_status(self.snap(nodes={"a": {"status": "accepted"}, "integration-review": {"status": "planned"}})))
        self.assertIn("整合 Review 进行中", render_v41_closeout_status(self.snap(nodes={"a": {"status": "accepted"}, "integration-review": {"status": "review"}})))
        self.assertIn("整合 Review 返工", render_v41_closeout_status(self.snap(nodes={"integration-review": {"status": "rework"}})))
        self.assertIn("整合通过但外部动作未授权", render_v41_closeout_status(self.accepted_snapshot()))

    def test_complex_incomplete_is_not_acceptance(self):
        text = render_v41_closeout_status(self.snap(status="complete", nodes={"integration-review": {"status": "planned"}}))
        self.assertIn("未闭合", text)
        self.assertIn("不可验收", text)


if __name__ == "__main__":
    unittest.main()
