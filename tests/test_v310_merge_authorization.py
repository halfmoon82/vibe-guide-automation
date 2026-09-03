import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.authorization import can_create_change_request, can_auto_merge
from vibe_guide.change_requests import execute_change_request, ChangeRequestEvidence
from vibe_guide.models import TargetContract
from vibe_guide.models import DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.state import RunSnapshot


SHA = "a" * 40


def auth(*actions):
    return SimpleNamespace(allowed_actions=tuple(actions))


def evidence(action="create_pr", **overrides):
    contract = {
            "provider": "github", "repository": "org/repo",
            "target_branch": "codex/v310", "source_branch": "feature/v310",
            "issue_type": "mr" if action == "create_mr" else "pr", "file_scope": ["vibe_guide/change_requests.py"],
            "merge_method": "squash", "frozen": True, "status": "frozen",
        }
    contract["digest"] = TargetContract.from_dict(contract).digest
    value = {
        "action": action,
        "final_review": {"status": "accepted", "reviewer_id": "reviewer-1"},
        "p0_p2": {"p0": 0, "p1": 0, "p2": 0},
        "developer": {"status": "delivered", "writer_id": "writer-1"},
        "target_contract": contract,
        "target_digest": contract["digest"],
        "provider": "github",
        "writer_reviewer_binding": True,
        "target_match": True,
        "provider_verified": True,
    }
    value.update(overrides)
    return value


class V310MergeAuthorizationTests(unittest.TestCase):
    def test_create_requires_explicit_action_and_final_review(self):
        self.assertTrue(can_create_change_request(evidence(), auth("create_pr")))
        self.assertFalse(can_create_change_request(evidence(), auth()))
        self.assertFalse(can_create_change_request(evidence(final_review={"status": "pending"}), auth("create_pr")))

    def test_create_pr_mr_and_merge_are_explicit_and_deploy_excluded(self):
        self.assertTrue(can_create_change_request(evidence("create_mr"), auth("create_mr")))
        self.assertFalse(can_create_change_request(evidence("create_mr"), auth("create_pr")))
        self.assertFalse(can_create_change_request(evidence("deploy"), auth("deploy")))

    def test_merge_requires_zero_p0_p2_and_frozen_target(self):
        self.assertTrue(can_auto_merge(evidence("merge_local"), auth("merge_local")))
        self.assertFalse(can_auto_merge(evidence("merge_local", p0_p2={"p0": 1, "p1": 0, "p2": 0}), auth("merge_local")))
        self.assertFalse(can_auto_merge(evidence("merge_local", target_match=False), auth("merge_local")))

    def test_target_drift_isolated_until_reconciled(self):
        observed = dict(evidence("create_pr")["target_contract"])
        observed["target_branch"] = "codex/other"
        self.assertFalse(can_create_change_request(
            evidence(observed_target_contract=observed), auth("create_pr")
        ))
        self.assertFalse(can_create_change_request(evidence(target_digest="0" * 64), auth("create_pr")))

    def test_writer_and_reviewer_must_be_distinct(self):
        self.assertFalse(can_create_change_request(evidence(
            final_review={"status": "accepted", "reviewer_id": "writer-1"}
        ), auth("create_pr")))

    def test_mapping_contract_digest_and_required_fields_are_checked(self):
        contract = evidence()["target_contract"]
        self.assertEqual(execute_change_request("create_pr", contract).status, "prepared")
        bad = dict(contract, digest="f" * 64)
        self.assertEqual(execute_change_request("create_pr", bad).status, "blocked_unknown")
        self.assertEqual(execute_change_request("merge_remote", contract).status, "prepared")
        self.assertEqual(execute_change_request("create_mr", contract).status, "blocked_unknown")
        self.assertEqual(execute_change_request("merge_local", dict(contract, issue_type="deploy")).status, "blocked_unknown")

    def test_merge_rejects_non_change_request_issue_types(self):
        self.assertFalse(can_auto_merge(
            evidence("merge_local", target_contract=dict(evidence("merge_local")["target_contract"], issue_type="deploy")),
            auth("merge_local"),
        ))

    def test_change_request_evidence_rejects_forged_status_digest_or_remote(self):
        contract = evidence()["target_contract"]
        digest = contract["digest"]
        for kwargs in (
            {"status": "merged_remote"},
            {"target_digest": "bad"},
            {"remote_mutated": True},
        ):
            with self.assertRaises((ValueError, TypeError)):
                ChangeRequestEvidence("merge_local", kwargs.pop("status", "prepared"), contract, kwargs.pop("target_digest", digest), "github", **kwargs)

    def test_replay_prepared_blocked_and_duplicate_are_semantic_and_idempotent(self):
        contract = evidence()["target_contract"]
        base = evidence()
        blocked = dict(base, status="blocked_unknown", details={"reason": "provider unknown"})
        records = []
        for seq, status, item in ((1, "prepared", base), (2, "blocked_unknown", blocked)):
            records.append({"sequence": seq, "event": "change_request_" + status, "data": {"run_id": "run-r", "node_id": "n1", "evidence": dict(item, status=status, remote_mutated=False, details={})}, "provenance": {"role": "system", "authorization_digest": "a", "node_contract_digest": "b"}})
        snapshot = RunSnapshot("run-r", "p", 1, "running", {"n1": {}}, {}, tasks={}, authorization_digest="a", node_contract_digest="b")
        monitor = Monitor.__new__(Monitor)
        monitor.paths = None
        monitor._binding_cache = {}
        with patch("vibe_guide.monitor.load_events", return_value=records):
            monitor._reconcile_unapplied_events(snapshot)
            self.assertEqual(len(snapshot.nodes["n1"]["change_request_evidence"]), 2)
            snapshot.event_sequence = 0
            monitor._reconcile_unapplied_events(snapshot)
            self.assertEqual(len(snapshot.nodes["n1"]["change_request_evidence"]), 2)
        forged = dict(base, status="prepared", target_digest="f" * 64, remote_mutated=True, details={})
        records[0]["data"]["evidence"] = forged
        snapshot.event_sequence = 0
        with patch("vibe_guide.monitor.load_events", return_value=records[:1]):
            with self.assertRaises(ValueError):
                monitor._reconcile_unapplied_events(snapshot)

    def test_remote_merge_requires_verified_provider_and_local_never_claims_remote(self):
        self.assertFalse(can_auto_merge(evidence("merge_remote", provider_verified=False), auth("merge_remote")))
        result = execute_change_request("merge_local", TargetContract(
            provider="github", repository="org/repo", target_branch="codex/v310",
            source_branch="feature/v310", issue_type="pr",
            file_scope=["vibe_guide/change_requests.py"], merge_method="squash",
            frozen=True, status="frozen"))
        self.assertIsInstance(result, ChangeRequestEvidence)
        self.assertNotEqual(result.status, "merged_remote")


if __name__ == "__main__":
    unittest.main()
