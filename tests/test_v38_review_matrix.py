import unittest

from vibe_guide.review import ReviewFinding, accept_review, bundle_findings


class V38ReviewMatrixTests(unittest.TestCase):
    def test_same_root_cause_is_one_bundle_and_missing_evidence_not_pass(self):
        bundles = bundle_findings([
            ReviewFinding("I1", "P1", "same", "first symptom", "pytest"),
            ReviewFinding("I2", "P1", "same", "second symptom", "pytest"),
        ])
        self.assertEqual(len(bundles), 1)
        self.assertEqual(set(bundles[0].invariant_ids), {"I1", "I2"})
        self.assertEqual(accept_review([]).status, "blocked_unknown")


if __name__ == "__main__":
    unittest.main()
