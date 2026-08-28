import json
import unittest

from vibe_guide.change_requests import (
    ChangeRequest,
    LocalMergeEvidence,
    classify_merge_capability,
    merge_local,
    merge_remote,
)


class ChangeRequestTests(unittest.TestCase):
    def test_pr_mr_other_names_do_not_grant_remote_merge_permission(self):
        for kind in ("PR", "MR", "other"):
            request = ChangeRequest(
                provider="example",
                kind=kind,
                source="feature",
                target="main",
                head_sha="a" * 40,
                tree_sha="b" * 40,
                merge_capability="unknown_remote",
            )
            self.assertEqual(request.merge_capability, "unknown_remote")
        self.assertEqual(
            classify_merge_capability({"title": "CANMERGE", "status": "PASS"}),
            "unknown_remote",
        )

    def test_denied_or_unsupported_remote_with_explicit_local_authorization_is_merged_local(self):
        for capability in ("denied_remote", "unsupported_remote"):
            with self.subTest(capability=capability):
                request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, capability, "", "V2-4", "MR-42")
                evidence = merge_local(
                    request,
                    {"allowed_actions": ("develop", "test", "review", "merge_local"), "merge_scope": {"issue_id": "V2-4", "source_sha": "a" * 40, "target_branch": "main", "change_request": "MR-42"}},
                    {
                        "issue_id": "V2-4",
                        "change_request": "MR-42",
                        "target_ref": "main",
                        "source_sha": "a" * 40,
                        "merge_base": "c" * 40,
                        "merge_commit": "d" * 40,
                        "merge_tree": "e" * 40,
                        "tests": ["python -m unittest"],
                    },
                )
                self.assertEqual(evidence["status"], "merged_local")
                self.assertFalse(evidence["pushed"])
                self.assertFalse(evidence["remote_mutated"])

    def test_unknown_remote_without_authorization_stays_blocked_unknown(self):
        request = ChangeRequest("example", "other", "feature", "main", "a" * 40, "b" * 40, "unknown_remote")
        evidence = merge_local(request, {"allowed_actions": ("develop", "test", "review")})
        self.assertEqual(evidence["status"], "blocked_unknown")

    def test_unknown_remote_with_local_authorization_stays_blocked_unknown(self):
        request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, "unknown_remote")
        evidence = merge_local(
            request,
            {"allowed_actions": ("merge_local",)},
            {
                "target_ref": "main",
                "source_sha": "a" * 40,
                "merge_base": "c" * 40,
                "merge_commit": "d" * 40,
                "merge_tree": "e" * 40,
                "tests": ["python -m unittest"],
            },
        )
        self.assertEqual(evidence["status"], "blocked_unknown")

    def test_local_merge_requires_non_empty_exact_target_ref(self):
        request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, "denied_remote", "", "V2-4", "MR-42")
        scope = {"issue_id": "V2-4", "source_sha": "a" * 40, "target_branch": "main", "change_request": "MR-42"}
        for target_ref in ("release", ""):
            with self.subTest(target_ref=target_ref):
                with self.assertRaises(ValueError):
                    merge_local(
                        request,
                        {"allowed_actions": ("merge_local",), "merge_scope": scope},
                        {
                            "issue_id": "V2-4",
                            "change_request": "MR-42",
                            "target_ref": target_ref,
                            "source_sha": "a" * 40,
                            "merge_base": "c" * 40,
                            "merge_commit": "d" * 40,
                            "merge_tree": "e" * 40,
                            "tests": ["python -m unittest"],
                        },
                    )

    def test_verified_remote_requires_strict_head_and_tree_sha(self):
        facts = {
            "source": "feature",
            "target": "main",
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "remote_merge_supported": True,
            "remote_merge_verified": True,
        }
        self.assertEqual(classify_merge_capability(facts), "verified_remote")
        for field in ("head_sha", "tree_sha"):
            malformed = dict(facts)
            malformed[field] = "bad"
            with self.subTest(field=field):
                self.assertEqual(classify_merge_capability(malformed), "unknown_remote")

    def test_verified_remote_rejects_conflicting_nested_provider_facts(self):
        facts = {
            "source": "feature",
            "target": "main",
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "remote_merge_supported": True,
            "remote_merge_verified": True,
            "provider_response": {
                "source": "feature",
                "target": "main",
                "head_sha": "c" * 40,
                "tree_sha": "b" * 40,
                "merge_allowed": True,
            },
        }
        self.assertEqual(classify_merge_capability(facts), "unknown_remote")

    def test_local_merge_evidence_is_json_safe(self):
        request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, "unsupported_remote", "", "V2-4", "MR-42")
        evidence = merge_local(
            request,
            {"allowed_actions": ("merge_local",), "merge_scope": {"issue_id": "V2-4", "source_sha": "a" * 40, "target_branch": "main", "change_request": "MR-42"}},
            {
                "issue_id": "V2-4",
                "change_request": "MR-42",
                "target_ref": "main",
                "source_sha": "a" * 40,
                "merge_base": "c" * 40,
                "merge_commit": "d" * 40,
                "merge_tree": "e" * 40,
                "tests": ["python -m unittest"],
            },
        )
        self.assertIsInstance(evidence, LocalMergeEvidence)
        self.assertEqual(evidence.status, "merged_local")
        encoded = json.dumps(evidence.to_dict(), sort_keys=True)
        self.assertIn('"remote_mutated": false', encoded)

    def test_local_merge_rejects_empty_or_whitespace_test_evidence(self):
        request = ChangeRequest("example", "MR", "feature", "main", "a" * 40, "b" * 40, "denied_remote", "", "V2-4", "MR-42")
        scope = {"issue_id": "V2-4", "source_sha": "a" * 40, "target_branch": "main", "change_request": "MR-42"}
        for tests in (" ", "", [], [""], [" "]):
            with self.subTest(tests=tests):
                with self.assertRaises(ValueError):
                    merge_local(
                        request,
                        {"allowed_actions": ("merge_local",), "merge_scope": scope},
                        {
                            "issue_id": "V2-4",
                            "change_request": "MR-42",
                            "target_ref": "main",
                            "source_sha": "a" * 40,
                            "merge_base": "c" * 40,
                            "merge_commit": "d" * 40,
                            "merge_tree": "e" * 40,
                            "tests": tests,
                        },
                    )

    def test_local_and_remote_merge_require_distinct_explicit_actions_and_scope(self):
        denied = ChangeRequest(
            "example", "MR", "feature", "main", "a" * 40, "b" * 40,
            "denied_remote", "", "V2-4", "MR-42",
        )
        local_authorization = {
            "allowed_actions": ("merge_local",),
            "merge_scope": {
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "MR-42",
            },
        }
        evidence = merge_local(
            denied,
            local_authorization,
            {
                "issue_id": "V2-4",
                "change_request": "MR-42",
                "target_ref": "main",
                "source_sha": "a" * 40,
                "merge_base": "c" * 40,
                "merge_commit": "d" * 40,
                "merge_tree": "e" * 40,
                "tests": ["python -m unittest"],
            },
        )
        self.assertEqual(evidence["status"], "merged_local")
        self.assertEqual(evidence["issue_id"], "V2-4")
        self.assertEqual(evidence["change_request"], "MR-42")
        with self.assertRaises(PermissionError):
            merge_remote(
                denied,
                {"allowed_actions": ("merge",), "merge_scope": local_authorization["merge_scope"]},
                {"remote_merge_verified": True},
            )

    def test_local_merge_without_issue_and_named_change_request_scope_is_rejected(self):
        request = ChangeRequest(
            "example", "MR", "feature", "main", "a" * 40, "b" * 40,
            "denied_remote",
        )
        with self.assertRaises(PermissionError):
            merge_local(
                request,
                {"allowed_actions": ("merge_local",)},
                {
                    "target_ref": "main",
                    "source_sha": "a" * 40,
                    "merge_base": "c" * 40,
                    "merge_commit": "d" * 40,
                    "merge_tree": "e" * 40,
                    "tests": ["python -m unittest"],
                },
            )

    def test_remote_merge_requires_verified_capability_and_exact_binding(self):
        request = ChangeRequest(
            "example", "PR", "feature", "main", "a" * 40, "b" * 40,
            "verified_remote", "", "V2-4", "PR-7",
        )
        authorization = {
            "allowed_actions": ("merge",),
            "merge_scope": {
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "PR-7",
            },
        }
        evidence = merge_remote(
            request,
            authorization,
            {
                "issue_id": "V2-4",
                "change_request": "PR-7",
                "target_branch": "main",
                "source_sha": "a" * 40,
                "remote_merge_verified": True,
                "remote_mutated": True,
                "tests": ["python -m unittest"],
            },
        )
        self.assertEqual(evidence["status"], "merged_remote")
        self.assertTrue(evidence["remote_mutated"])
        self.assertFalse(evidence["pushed"])
        with self.assertRaises(ValueError):
            merge_remote(
                request,
                authorization,
                {
                    "issue_id": "V2-4",
                    "change_request": "PR-7",
                    "target_branch": "release",
                    "source_sha": "a" * 40,
                    "remote_merge_verified": True,
                    "remote_mutated": True,
                    "tests": ["python -m unittest"],
                },
            )

    def test_remote_merge_rejects_conflicting_provider_head_sha(self):
        request = ChangeRequest(
            "example", "PR", "feature", "main", "a" * 40, "b" * 40,
            "verified_remote", "", "V2-4", "PR-7",
        )
        authorization = {
            "allowed_actions": ("merge",),
            "merge_scope": {
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "PR-7",
            },
        }
        with self.assertRaises(ValueError):
            merge_remote(
                request,
                authorization,
                {
                    "issue_id": "V2-4",
                    "change_request": "PR-7",
                    "target_branch": "main",
                    "head_sha": "c" * 40,
                    "remote_merge_verified": True,
                    "remote_mutated": True,
                },
            )

    def test_remote_merge_rejects_nested_provider_target_and_head_binding_conflicts(self):
        request = ChangeRequest(
            "example", "PR", "feature", "main", "a" * 40, "b" * 40,
            "verified_remote", "", "V2-4", "PR-7",
        )
        authorization = {
            "allowed_actions": ("merge",),
            "merge_scope": {
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "PR-7",
            },
        }
        for nested in (
            {"target": "release", "head_sha": "a" * 40},
            {"target": "main", "head_sha": "c" * 40},
        ):
            with self.subTest(nested=nested):
                with self.assertRaises(ValueError):
                    merge_remote(
                        request,
                        authorization,
                        {
                            "issue_id": "V2-4",
                            "change_request": "PR-7",
                            "target_branch": "main",
                            "source_sha": "a" * 40,
                            "remote_merge_verified": True,
                            "remote_mutated": True,
                            "provider_response": nested,
                        },
                    )

    def test_remote_merge_requires_change_request_target_and_head_binding(self):
        authorization = {
            "allowed_actions": ("merge",),
            "merge_scope": {
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "PR-7",
            },
        }
        for request in (
            ChangeRequest("example", "PR", "feature", "release", "a" * 40, "b" * 40, "verified_remote", "", "V2-4", "PR-7"),
            ChangeRequest("example", "PR", "feature", "main", "c" * 40, "b" * 40, "verified_remote", "", "V2-4", "PR-7"),
        ):
            with self.subTest(target=request.target, head=request.head_sha):
                with self.assertRaises(ValueError):
                    merge_remote(
                        request,
                        authorization,
                        {
                            "issue_id": "V2-4",
                            "change_request": "PR-7",
                            "target_branch": "main",
                            "source_sha": "a" * 40,
                            "remote_merge_verified": True,
                            "remote_mutated": True,
                        },
                    )

    def test_push_facts_never_become_pushed_false_success_evidence(self):
        request = ChangeRequest(
            "example", "PR", "feature", "main", "a" * 40, "b" * 40,
            "verified_remote", "", "V2-4", "PR-7",
        )
        authorization = {
            "allowed_actions": ("merge",),
            "merge_scope": {
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "PR-7",
            },
        }
        for facts in (
            {"pushed": True},
            {"push_attempted": True},
            {"provider_response": {"push_status": "succeeded"}},
        ):
            with self.subTest(facts=facts):
                result = merge_remote(
                    request,
                    authorization,
                    {
                        "issue_id": "V2-4",
                        "change_request": "PR-7",
                        "target_branch": "main",
                        "source_sha": "a" * 40,
                        "remote_merge_verified": True,
                        "remote_mutated": True,
                        **facts,
                    },
                )
                self.assertEqual(result["status"], "blocked_unknown")
                self.assertTrue(result["pushed"])

    def test_nested_push_facts_never_become_pushed_false_success_evidence(self):
        request = ChangeRequest(
            "example", "PR", "feature", "main", "a" * 40, "b" * 40,
            "verified_remote", "", "V2-4", "PR-7",
        )
        authorization = {
            "allowed_actions": ("merge",),
            "merge_scope": {
                "issue_id": "V2-4",
                "source_sha": "a" * 40,
                "target_branch": "main",
                "change_request": "PR-7",
            },
        }
        for provider_facts in (
            {"pushed": True},
            {"push_attempted": True},
            {"push_succeeded": True},
            {"push_status": "succeeded"},
            {"pushed": "true"},
        ):
            with self.subTest(provider_facts=provider_facts):
                result = merge_remote(
                    request,
                    authorization,
                    {
                        "issue_id": "V2-4",
                        "change_request": "PR-7",
                        "target_branch": "main",
                        "source_sha": "a" * 40,
                        "remote_merge_verified": True,
                        "remote_mutated": True,
                        "provider_response": provider_facts,
                    },
                )
                self.assertEqual(result["status"], "blocked_unknown")
                self.assertTrue(result["pushed"])


if __name__ == "__main__":
    unittest.main()
