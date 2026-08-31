import unittest
import json
import tempfile
from pathlib import Path

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.change_requests import (
    ChangeRequest,
    LocalMergeEvidence,
    classify_merge_capability,
    merge_local,
)
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


class ChangeRequestTests(unittest.TestCase):
    def test_kind_is_normalized_without_granting_capability(self):
        self.assertEqual(
            ChangeRequest("coding", "pr", "feature", "main", "a" * 40, "b" * 40, "unknown_remote").kind,
            "PR",
        )
        self.assertEqual(
            ChangeRequest("coding", "mr", "feature", "main", "a" * 40, "b" * 40, "unknown_remote").kind,
            "MR",
        )
        self.assertEqual(
            ChangeRequest("coding", "anything", "feature", "main", "a" * 40, "b" * 40, "unknown_remote").kind,
            "other",
        )
        self.assertEqual(
            classify_merge_capability({"kind": "PR", "title": "CANMERGE", "status": "PASS"}),
            "unknown_remote",
        )

    def test_capability_requires_provider_evidence(self):
        self.assertEqual(
            classify_merge_capability({"remote_merge_supported": True, "remote_merge_verified": True,
                                       "source": "feature", "target": "main", "head_sha": "a" * 40,
                                       "tree_sha": "b" * 40}),
            "verified_remote",
        )
        self.assertEqual(classify_merge_capability({"permission_denied": True}), "denied_remote")
        self.assertEqual(
            classify_merge_capability({"provider_response": {"permission_denied": True}}),
            "denied_remote",
        )
        self.assertEqual(classify_merge_capability({"remote_merge_supported": False}), "unsupported_remote")
        self.assertEqual(classify_merge_capability({"network_error": True}), "unknown_remote")

    def test_verified_provider_response_must_match_change_request_facts(self):
        facts = {
            "source": "feature",
            "target": "main",
            "head_sha": "a" * 40,
            "tree_sha": "b" * 40,
            "provider_response": {
                "merge_allowed": True,
                "source": "feature",
                "target": "main",
                "head_sha": "c" * 40,
                "tree_sha": "b" * 40,
            },
        }

        self.assertEqual(classify_merge_capability(facts), "unknown_remote")

    def test_local_merge_requires_explicit_action_and_never_pushes(self):
        node = DAGNode("n1", "n1", [], [], "g", {"files": ["safe.py"]}, "ready")
        plan = Plan("p1", 1, "prd.md", ["n1"], "draft")
        caps = AgentCapabilities("codex", True, True, True, True, True, "full")
        denied = ChangeRequest("coding", "MR", "feature", "main", "a" * 40, "b" * 40, "denied_remote")

        ordinary = authorize(build_authorization_card(plan, [node], caps), "AUTHORIZE")
        with self.assertRaises(PermissionError):
            merge_local(denied, ordinary)

        local_card = build_authorization_card(plan, [node], caps, allowed_actions=("develop", "test", "review", "merge_local"))
        record = authorize(local_card, "AUTHORIZE")
        evidence = merge_local(
            denied,
            record,
            {"target_ref": "main", "merge_base": "c" * 40, "merge_commit": "d" * 40,
             "merge_tree": "e" * 40, "tests": ["python -m unittest"]},
        )
        self.assertIsInstance(evidence, LocalMergeEvidence)
        self.assertEqual(evidence.status, "merged_local")
        self.assertFalse(evidence.pushed)
        self.assertFalse(evidence.remote_mutated)

    def test_unknown_remote_without_local_authorization_is_blocked_unknown(self):
        node = DAGNode("n1", "n1", [], [], "g", {"files": ["safe.py"]}, "ready")
        plan = Plan("p1", 1, "prd.md", ["n1"], "draft")
        caps = AgentCapabilities("codex", True, True, True, True, True, "full")
        record = authorize(build_authorization_card(plan, [node], caps), "AUTHORIZE")
        unknown = ChangeRequest("coding", "other", "feature", "main", "a" * 40, "b" * 40, "unknown_remote")
        evidence = merge_local(unknown, record)
        self.assertEqual(evidence.status, "blocked_unknown")

    def test_local_merge_rejects_a_target_ref_that_does_not_match_request(self):
        node = DAGNode("n1", "n1", [], [], "g", {"files": ["safe.py"]}, "ready")
        plan = Plan("p1", 1, "prd.md", ["n1"], "draft")
        caps = AgentCapabilities("codex", True, True, True, True, True, "full")
        card = build_authorization_card(
            plan,
            [node],
            caps,
            allowed_actions=("develop", "test", "review", "merge_local"),
        )
        record = authorize(card, "AUTHORIZE")
        denied = ChangeRequest("coding", "MR", "feature", "main", "a" * 40, "b" * 40, "denied_remote")

        with self.assertRaises(ValueError):
            merge_local(
                denied,
                record,
                {
                    "target_ref": "release",
                    "merge_base": "c" * 40,
                    "merge_commit": "d" * 40,
                    "merge_tree": "e" * 40,
                    "tests": ["python -m unittest"],
                },
            )

    def test_cli_reports_unknown_remote_without_remote_merge_claim(self):
        from vibe_guide.cli import run_cli

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            (root / "cr.json").write_text(
                json.dumps({
                    "provider": "coding", "kind": "MR", "source": "feature",
                    "target": "main", "head_sha": "a" * 40, "tree_sha": "b" * 40,
                    "title": "CANMERGE", "status": "PASS",
                }),
                encoding="utf-8",
            )
            result = run_cli(["change-request", "--request", "cr.json", "--json"], root)
        self.assertEqual(result.exit_code, 4)
        self.assertEqual(result.payload["merge_capability"], "unknown_remote")
        self.assertEqual(result.payload["status"], "blocked_unknown")
        self.assertFalse(result.payload["remote_merge"])


if __name__ == "__main__":
    unittest.main()
