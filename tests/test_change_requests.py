import unittest
import json
import tempfile
from pathlib import Path

from vibe_guide.change_requests import ChangeRequest, classify_merge_capability, merge_remote


class ChangeRequestTests(unittest.TestCase):
    def test_unknown_remote_never_becomes_verified_from_markers(self):
        request = ChangeRequest(
            "example", "MR", "feature", "main", "a" * 40, "b" * 40,
            classify_merge_capability({"title": "CANMERGE", "status": "PASS"}),
        )
        self.assertEqual(request.merge_capability, "unknown_remote")

    def test_verified_remote_requires_corroborated_provider_facts(self):
        facts = {
            "source": "feature",
            "target": "main",
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "remote_merge_supported": True,
            "remote_merge_verified": True,
        }
        self.assertEqual(classify_merge_capability(facts), "verified_remote")
        conflicting = dict(facts, provider_response={"head_sha": "c" * 40})
        self.assertEqual(classify_merge_capability(conflicting), "unknown_remote")

    def test_remote_merge_requires_result_commit_tree_merge_base_and_tests(self):
        request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, "verified_remote", "", "V3-2", "MR-1")
        authorization = {
            "allowed_actions": ("merge",),
            "merge_scope": {
                "issue_id": "V3-2", "source_sha": "a" * 40,
                "target_branch": "main", "change_request_id": "MR-1",
            },
        }
        incomplete = {
            "issue_id": "V3-2", "source_sha": "a" * 40,
            "target_branch": "main", "change_request_id": "MR-1",
            "remote_merge_verified": True, "remote_mutated": True,
        }
        evidence = merge_remote(request, authorization, incomplete)
        self.assertEqual(evidence.status, "blocked_unknown")

    def test_remote_merge_accepts_only_complete_result_evidence(self):
        request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, "verified_remote", "", "V3-2", "MR-1")
        authorization = {"allowed_actions": ("merge",), "merge_scope": {"issue_id": "V3-2", "source_sha": "a" * 40, "target_branch": "main", "change_request_id": "MR-1"}}
        evidence = merge_remote(request, authorization, {
            "issue_id": "V3-2", "source_sha": "a" * 40, "target_branch": "main", "change_request_id": "MR-1",
            "remote_merge_verified": True, "remote_mutated": True,
            "merge_base": "c" * 40, "merge_commit": "d" * 40, "merge_tree": "e" * 40,
            "tests": ["python -m unittest"],
        })
        self.assertEqual(evidence.status, "merged_remote")
        self.assertEqual(evidence.merge_base, "c" * 40)
        self.assertEqual(evidence.merge_commit, "d" * 40)
        self.assertEqual(evidence.merge_tree, "e" * 40)

    def test_v2_legacy_remote_merge_keeps_existing_result_evidence_entrypoint(self):
        request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, "verified_remote", "", "V2-4", "MR-42")
        authorization = {"allowed_actions": ("merge",), "merge_scope": {"issue_id": "V2-4", "source_sha": "a" * 40, "target_branch": "main", "change_request_id": "MR-42"}}
        evidence = merge_remote(request, authorization, {
            "issue_id": "V2-4", "source_sha": "a" * 40, "target_branch": "main", "change_request_id": "MR-42",
            "remote_merge_verified": True, "remote_mutated": True, "tests": ["python -m unittest"],
        })
        self.assertEqual(evidence.status, "merged_remote")

    def test_cli_classifies_change_request_even_when_v2_runtime_is_unavailable(self):
        from vibe_guide.cli import run_cli

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "request.json"
            path.write_text(json.dumps({
                "provider": "example", "kind": "MR", "source": "feature", "target": "main",
                "head_sha": "a" * 40, "tree_sha": "b" * 40,
                "title": "CANMERGE", "status": "PASS",
            }), encoding="utf-8")
            result = run_cli(["change-request", "--request", str(path), "--json"], Path(directory))
        self.assertEqual(result.payload["status"], "blocked_unknown")


if __name__ == "__main__":
    unittest.main()
