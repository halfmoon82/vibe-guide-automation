import unittest
from vibe_guide.cli import render_v41_closeout_status
from vibe_guide.state import RunSnapshot


class V41CliTests(unittest.TestCase):
    def snap(self, status="running", nodes=None, evidence=None):
        return RunSnapshot("r", "p", 1, status, nodes or {}, {}, integration_review_evidence=evidence or {})

    def test_status_distinguishes_integration_phases(self):
        self.assertIn("局部节点完成", render_v41_closeout_status(self.snap(nodes={"a": {"status": "accepted"}, "integration-review": {"status": "planned"}})))
        self.assertIn("整合 Review 进行中", render_v41_closeout_status(self.snap(nodes={"a": {"status": "accepted"}, "integration-review": {"status": "review"}})))
        self.assertIn("整合 Review 返工", render_v41_closeout_status(self.snap(nodes={"integration-review": {"status": "rework"}})))
        self.assertIn("整合通过但外部动作未授权", render_v41_closeout_status(self.snap(status="complete", nodes={"integration-review": {"status": "accepted"}}, evidence={"status": "accepted", "p0_p2": {"p0": 0, "p1": 0, "p2": 0}})))

    def test_complex_incomplete_is_not_acceptance(self):
        text = render_v41_closeout_status(self.snap(status="complete", nodes={"integration-review": {"status": "planned"}}))
        self.assertIn("未闭合", text)
        self.assertIn("不可验收", text)


if __name__ == "__main__":
    unittest.main()
