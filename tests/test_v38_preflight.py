import tempfile
import unittest
from pathlib import Path

from vibe_guide.preflight import (
    Check, PreflightContext, assert_authorizable, preflight_status, run_preflight,
    validate_v38_artifact_set,
)
from vibe_guide.authorization import build_v38_authorization_card


class V38PreflightTests(unittest.TestCase):
    def test_mismatch_and_unknown_block_authorization(self):
        context = PreflightContext(checks=[
            Check("base", "passed", {}, {}, "git:base", "none"),
            Check("provider", "unknown", {}, {}, "runtime:probe", "refresh"),
        ])
        report = run_preflight(context)
        self.assertEqual(preflight_status(report), "preflight_blocked")
        with self.assertRaises(ValueError):
            assert_authorizable(report)

    def test_all_passed_is_ready_to_authorize(self):
        report = run_preflight(PreflightContext(checks=[
            Check("base", "passed", {}, {}, "git:base", "none"),
        ]))
        self.assertEqual(preflight_status(report), "ready_to_authorize")
        assert_authorizable(report)

    def test_structured_mismatch_checks_are_fail_closed(self):
        context = PreflightContext.from_mapping({
            "base_sha": "a" * 40,
            "expected_base_sha": "b" * 40,
            "remote_target": "origin/main",
            "expected_remote_target": "origin/codex/v38-integration",
            "binding_occupied": True,
            "old_active_writer": True,
            "owned_path_overlap": {"path": "vibe_guide/preflight.py", "nodes": ["V38-2", "V38-5"]},
            "production_entrypoint": "vibe_guide/preflight.py:run_preflight",
            "allowlist": [],
            "capability_status": "unknown",
            "baseline_manifest": None,
            "merge_target_branch": "",
        })
        report = run_preflight(context)
        self.assertEqual(preflight_status(report), "preflight_blocked")
        check_ids = {item.check_id for item in report.checks}
        self.assertTrue({
            "base_sha", "remote_target", "occupied_binding", "old_active_writer",
            "owned_path_overlap", "production_entrypoint", "structured_capability",
            "baseline_manifest", "merge_target",
        }.issubset(check_ids))
        self.assertTrue(all(item.status in {"mismatch", "unknown", "passed"} for item in report.checks))

    def test_unknown_capability_is_not_authorizable_even_with_other_passes(self):
        report = run_preflight(PreflightContext.from_mapping({
            "base_sha": "a" * 40,
            "expected_base_sha": "a" * 40,
            "remote_target": "origin/codex/v38-integration",
            "expected_remote_target": "origin/codex/v38-integration",
            "capability_status": "unknown",
            "baseline_manifest": {"digest": "d"},
            "merge_target_branch": "codex/v38-integration",
        }))
        self.assertEqual(preflight_status(report), "preflight_blocked")
        self.assertEqual(next(item for item in report.checks if item.check_id == "structured_capability").status, "unknown")

    def test_empty_context_is_fail_closed(self):
        report = run_preflight(PreflightContext())
        self.assertEqual(preflight_status(report), "preflight_blocked")

    def test_artifacts_require_current_revisions_and_complete_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            refs = []
            for name, revision in (("prd", 2), ("spec", 2), ("issues", 2), ("dag", 3)):
                path = root / (name + ".md")
                path.write_text("FR-801..FR-811 AC-801..AC-808 V38-1 V38-2 V38-3 V38-4 V38-5 V38-6 V38-7 V38-8 merge_to_main deploy: excluded_by_prd\n", encoding="utf-8")
                refs.append(str(path) + "@" + str(revision))
            result = validate_v38_artifact_set(*refs)
        self.assertTrue(result.valid, result.missing)

    def test_invalid_check_status_and_partial_observations_block_unknown(self):
        malformed = run_preflight(PreflightContext(checks=[Check("bad", "bogus", {}, {}, "", "")]))
        self.assertEqual(preflight_status(malformed), "preflight_blocked")
        self.assertIn("bad", {item.check_id for item in malformed.checks})
        report = run_preflight(PreflightContext.from_mapping({"merge_target_branch": "codex/v38-integration"}))
        self.assertEqual(preflight_status(report), "preflight_blocked")
        self.assertTrue(any(item.status == "unknown" for item in report.checks))

    def test_main_target_variants_are_rejected_case_insensitively(self):
        for target in (" main ", "MAIN", " origin/main "):
            report = run_preflight(PreflightContext.from_mapping({
                "merge_target_branch": target,
                "baseline_manifest": {"schema_version": 1, "base_sha": "a" * 40, "commands": [], "collection_count": 0, "import_errors": 0, "scope": "passed", "generated_at": "now"},
                "capability_status": "verified_available",
            }))
            self.assertEqual(preflight_status(report), "preflight_blocked", target)

    def test_preflight_does_not_create_tasks_or_worktrees(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            before = sorted(root.rglob("*"))
            report = run_preflight(PreflightContext.from_mapping({
                "base_sha": "a" * 40,
                "expected_base_sha": "b" * 40,
                "baseline_manifest": None,
            }))
            self.assertEqual(preflight_status(report), "preflight_blocked")
            self.assertEqual(before, sorted(root.rglob("*")))

    def test_authorization_card_rejects_blocked_preflight(self):
        with self.assertRaises(ValueError):
            build_v38_authorization_card(
                "p", 3, "run", 0, ["V38-2"], ["V38-2"], ["vibe_guide/preflight.py"],
                "codex/v38-integration", "preflight-report.json", "ref",
                preflight_report=run_preflight(PreflightContext.from_mapping({"capability_status": "unknown"})),
            )


if __name__ == "__main__":
    unittest.main()
