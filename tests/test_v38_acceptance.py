import unittest

from vibe_guide.preflight import validate_v38_artifact_set


class V38AcceptanceTests(unittest.TestCase):
    def test_confirmed_artifact_set_is_versioned_and_complete(self):
        result = validate_v38_artifact_set(
            "docs/superpowers/specs/2026-08-30-vibe-coding-development-guide-v3.8-prd.md@2",
            "docs/superpowers/specs/2026-08-30-vibe-coding-development-guide-v3.8-spec.md@2",
            "docs/superpowers/specs/2026-08-30-vibe-coding-development-guide-v3.8-issues.yaml@2",
            "docs/superpowers/plans/2026-08-30-vibe-coding-guide-v3.8-spec-issue-dag.yaml@3",
        )
        self.assertTrue(result.valid, result.missing)


if __name__ == "__main__":
    unittest.main()
