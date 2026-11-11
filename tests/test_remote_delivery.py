"""Issue #371: the explicit remote-delivery ring behind remote_git_actions=allow.

Covers the acceptance examples from the issue: deny mode keeps the historical
behavior; allow mode appends a monitor-driven remote-delivery node after the
integration review and the run cannot complete until it is accepted on
remotely verified evidence; failures never complete or fabricate evidence;
retries resume from persisted PR/head/merge facts without re-creating a PR or
re-merging; and a change to the aggregated content or the authorization
digest invalidates the old evidence instead of reusing it.
"""
import copy
import unittest
from types import SimpleNamespace

from vibe_guide.evidence import (
    build_integration_review_evidence,
    evaluate_v41_closeout,
)
from vibe_guide.models import REMOTE_DELIVERY_NODE_ID
from vibe_guide.remote_delivery import (
    DEFAULT_TARGET_BRANCH,
    REMOTE_DELIVERY_STEPS,
    RemoteDeliveryExecutor,
    RemoteDeliveryFailed,
    SubprocessRemoteDeliveryExecutor,
    binding_matches,
    delivery_binding,
    drive_remote_delivery,
    remote_delivery_required,
    validate_remote_delivery_evidence,
)

H1 = "1" * 40
H2 = "2" * 40
H3 = "3" * 40
PR_URL = "https://github.com/halfmoon82/vibe-guide-automation/pull/371"


def _nodes():
    return {
        "alpha": {"status": "accepted", "branch": "node/alpha"},
        "beta": {"status": "accepted", "branch": "node/beta"},
        "integration-review": {"status": "accepted"},
    }


CLEARED_CLAIM = {
    "findings": [],
    "iteration_compatibility": {"status": "verified", "evidence": "迭代兼容性已逐条复核"},
    "test_runtime_delivery": {"status": "verified", "evidence": "测试与运行时交付已复核"},
    "out_of_scope": [],
}


def _snapshot(remote="allow"):
    snapshot = SimpleNamespace(
        run_id="run-1",
        plan_id="plan-1",
        plan_version=1,
        status="running",
        authorization_digest="a" * 64,
        node_contract_digest="b" * 64,
        topology_digest="c" * 64,
        prd_digest="d" * 64,
        spec_digest="e" * 64,
        nodes=_nodes(),
        integration_review_evidence={},
        remote_delivery_evidence={},
        authorization={"remote_git_actions": remote},
    )
    snapshot.integration_review_evidence = build_integration_review_evidence(
        snapshot, CLEARED_CLAIM,
        agentsmd_acceptance_refs=["AGENTS.md"],
        unverified_or_excluded=["deploy"],
    )
    return snapshot


class FakeExecutor(RemoteDeliveryExecutor):
    def __init__(self, fail_at=None, review=None, verify=None):
        self.calls = []
        self.fail_at = fail_at
        self.review = review or {"approved": True, "changes_requested": False, "pr_state": "OPEN"}
        self.verify = verify or {"verified": True, "main_head_sha": H3, "merge_commit": H2, "pr_state": "MERGED"}
        self.facts = {
            "prepare": {"source_branch": "vibe/integration-candidate-plan-1", "target_branch": "main", "head_sha": H1},
            "push": {"head_sha": H1, "already_pushed": False},
            "create_pr": {"pr_url": PR_URL, "pr_number": 371},
            "review": self.review,
            "merge": {"merge_commit": H2},
            "verify": self.verify,
        }

    def execute(self, step, context):
        self.calls.append(step)
        if step == self.fail_at:
            raise RemoteDeliveryFailed(step, "injected failure", retryable=True)
        return dict(self.facts[step])


def _events():
    recorded = []
    return recorded, (lambda name, data: recorded.append((name, data)))


class RemoteDeliveryRequirementTests(unittest.TestCase):
    def test_deny_mode_never_requires_delivery(self):
        self.assertFalse(remote_delivery_required({"remote_git_actions": "deny"}))
        self.assertFalse(remote_delivery_required({}))
        self.assertFalse(remote_delivery_required(None))

    def test_allow_mode_requires_delivery(self):
        self.assertTrue(remote_delivery_required({"remote_git_actions": "allow"}))

    def test_drive_is_a_no_op_under_deny(self):
        snapshot = _snapshot(remote="deny")
        executor = FakeExecutor()
        recorded, record_event = _events()
        self.assertFalse(drive_remote_delivery(snapshot, executor, record_event))
        self.assertNotIn(REMOTE_DELIVERY_NODE_ID, snapshot.nodes)
        self.assertEqual(executor.calls, [])
        self.assertEqual(recorded, [])
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)

    def test_drive_waits_until_the_integration_review_is_accepted(self):
        snapshot = _snapshot()
        snapshot.nodes["integration-review"]["status"] = "review"
        executor = FakeExecutor()
        self.assertFalse(drive_remote_delivery(snapshot, executor))
        self.assertNotIn(REMOTE_DELIVERY_NODE_ID, snapshot.nodes)
        self.assertEqual(executor.calls, [])


class RemoteDeliveryDriveTests(unittest.TestCase):
    def test_full_delivery_accepts_the_node_and_allows_closeout(self):
        snapshot = _snapshot()
        executor = FakeExecutor()
        recorded, record_event = _events()
        self.assertTrue(drive_remote_delivery(snapshot, executor, record_event))
        self.assertEqual(executor.calls, list(REMOTE_DELIVERY_STEPS))
        node = snapshot.nodes[REMOTE_DELIVERY_NODE_ID]
        self.assertEqual(node["status"], "accepted")
        evidence = snapshot.remote_delivery_evidence
        self.assertEqual(evidence["status"], "accepted")
        self.assertEqual(evidence["pr_url"], PR_URL)
        self.assertEqual(evidence["source_branch"], "vibe/integration-candidate-plan-1")
        self.assertEqual(evidence["target_branch"], DEFAULT_TARGET_BRANCH)
        self.assertEqual(evidence["head_sha"], H1)
        self.assertEqual(evidence["merge_commit"], H2)
        self.assertEqual(evidence["remote_verification"]["verified"], True)
        self.assertEqual(evidence["remote_verification"]["main_head_sha"], H3)
        validate_remote_delivery_evidence(snapshot, evidence)
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)
        names = [name for name, _ in recorded]
        self.assertIn("remote_delivery_node_appended", names)
        self.assertIn("remote_delivery_accepted", names)
        completed = [data.get("step") for name, data in recorded if name == "remote_delivery_step_completed"]
        self.assertEqual(completed, list(REMOTE_DELIVERY_STEPS))
        accepted = dict(recorded)["remote_delivery_accepted"]["delivery"]
        self.assertEqual(accepted["pr_url"], PR_URL)
        self.assertEqual(accepted["head_sha"], H1)
        self.assertEqual(accepted["merge_commit"], H2)
        self.assertEqual(accepted["source_branch"], evidence["source_branch"])
        self.assertEqual(accepted["target_branch"], "main")

    def test_failure_parks_blocked_unknown_without_completing_or_fabricating(self):
        snapshot = _snapshot()
        executor = FakeExecutor(fail_at="push")
        recorded, record_event = _events()
        drive_remote_delivery(snapshot, executor, record_event)
        node = snapshot.nodes[REMOTE_DELIVERY_NODE_ID]
        self.assertEqual(node["status"], "blocked_unknown")
        self.assertEqual(node["failure"]["step"], "push")
        self.assertTrue(node["failure"]["retryable"])
        evidence = snapshot.remote_delivery_evidence
        self.assertEqual(evidence["status"], "blocked_unknown")
        self.assertNotIn("pr_url", evidence)
        self.assertNotIn("create_pr", evidence["steps"])
        self.assertFalse(evaluate_v41_closeout(snapshot).allowed)
        decision = evaluate_v41_closeout(snapshot)
        self.assertIn("remote delivery is not accepted", decision.reasons)
        self.assertIn("remote_delivery_failed", [name for name, _ in recorded])

    def test_retry_resumes_from_the_failed_step_without_redoing_prior_steps(self):
        snapshot = _snapshot()
        first = FakeExecutor(fail_at="push")
        drive_remote_delivery(snapshot, first)
        retry = FakeExecutor()
        recorded, record_event = _events()
        drive_remote_delivery(snapshot, retry, record_event)
        # prepare is a persisted fact and must not re-run; push failed last
        # time and is retried, then the remaining steps run once.
        self.assertNotIn("prepare", retry.calls)
        self.assertEqual(retry.calls, ["push", "create_pr", "review", "merge", "verify"])
        self.assertEqual(snapshot.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "accepted")
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)

    def test_restart_after_crash_reuses_the_persisted_pr_and_merge_facts(self):
        snapshot = _snapshot()
        drive_remote_delivery(snapshot, FakeExecutor())
        self.assertEqual(snapshot.remote_delivery_evidence["pr_url"], PR_URL)
        # Simulate a restart: a fresh snapshot view over the same persisted
        # evidence must not re-create the PR or re-merge.
        restarted = _snapshot()
        restarted.nodes[REMOTE_DELIVERY_NODE_ID] = copy.deepcopy(snapshot.nodes[REMOTE_DELIVERY_NODE_ID])
        restarted.remote_delivery_evidence = copy.deepcopy(snapshot.remote_delivery_evidence)
        executor = FakeExecutor()
        self.assertFalse(drive_remote_delivery(restarted, executor))
        self.assertEqual(executor.calls, [])

    def test_digest_binding_change_invalidates_old_evidence(self):
        snapshot = _snapshot()
        drive_remote_delivery(snapshot, FakeExecutor(fail_at="merge"))
        # A binding digest move (here the topology digest; an authorization
        # digest move flows through reauthorization, which rewrites the
        # integration evidence and therefore the integration digest) must not
        # let the run reuse the old PR/merge facts.
        snapshot.topology_digest = "f" * 64
        executor = FakeExecutor()
        recorded, record_event = _events()
        drive_remote_delivery(snapshot, executor, record_event)
        self.assertIn("remote_delivery_evidence_invalidated", [name for name, _ in recorded])
        # Everything restarts: the PR is created again under the new binding.
        self.assertEqual(executor.calls, list(REMOTE_DELIVERY_STEPS))
        self.assertEqual(snapshot.remote_delivery_evidence["binding"]["topology_digest"], "f" * 64)
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)

    def test_integration_content_change_invalidates_old_evidence(self):
        snapshot = _snapshot()
        drive_remote_delivery(snapshot, FakeExecutor())
        changed = build_integration_review_evidence(
            snapshot,
            dict(CLEARED_CLAIM, findings=[{
                "severity": "p3", "status": "open", "detail": "观察项，不影响清零",
            }]),
            agentsmd_acceptance_refs=["AGENTS.md"],
            unverified_or_excluded=["deploy"],
        )
        snapshot.integration_review_evidence = changed
        executor = FakeExecutor()
        drive_remote_delivery(snapshot, executor)
        self.assertEqual(executor.calls, list(REMOTE_DELIVERY_STEPS))

    def test_review_findings_park_rework_and_never_complete(self):
        snapshot = _snapshot()
        executor = FakeExecutor(review={"approved": False, "changes_requested": True, "pr_state": "OPEN"})
        recorded, record_event = _events()
        drive_remote_delivery(snapshot, executor, record_event)
        node = snapshot.nodes[REMOTE_DELIVERY_NODE_ID]
        self.assertEqual(node["status"], "rework")
        self.assertEqual(snapshot.remote_delivery_evidence["status"], "rework")
        self.assertFalse(evaluate_v41_closeout(snapshot).allowed)
        self.assertIn("remote_delivery_rework", [name for name, _ in recorded])

    def test_unverified_remote_facts_block_acceptance(self):
        snapshot = _snapshot()
        executor = FakeExecutor(verify={"verified": False, "main_head_sha": H3, "merge_commit": H2, "pr_state": "OPEN"})
        drive_remote_delivery(snapshot, executor)
        self.assertEqual(snapshot.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "blocked_unknown")
        self.assertFalse(evaluate_v41_closeout(snapshot).allowed)

    def test_missing_approval_blocks_merge(self):
        snapshot = _snapshot()
        executor = FakeExecutor(review={"approved": False, "changes_requested": False, "pr_state": "OPEN"})
        drive_remote_delivery(snapshot, executor)
        self.assertEqual(snapshot.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "blocked_unknown")
        self.assertNotIn("merge", snapshot.remote_delivery_evidence["steps"])


class AlreadyDeliveredTests(unittest.TestCase):
    def test_already_delivered_prepare_skips_pr_ring_and_verifies(self):
        snapshot = _snapshot()
        executor = FakeExecutor()
        executor.facts["prepare"] = dict(
            executor.facts["prepare"], already_delivered=True, pr_url=PR_URL, pr_number=371,
            pr_head_sha=H1, merge_commit=H2)
        drive_remote_delivery(snapshot, executor)
        self.assertEqual(executor.calls, ["prepare", "verify"])
        self.assertEqual(snapshot.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "accepted")
        self.assertEqual(snapshot.remote_delivery_evidence["review_basis"], "already_delivered")
        self.assertEqual(snapshot.remote_delivery_evidence["merge_commit"], H2)
        self.assertTrue(evaluate_v41_closeout(snapshot).allowed)


class ReviewGateWaiverTests(unittest.TestCase):
    def _review(self, protection, reviewers=None, approved=False, changes=False):
        facts = {"approved": approved, "changes_requested": changes, "pr_state": "OPEN",
                 "review_protection": protection}
        if reviewers is not None:
            facts["requested_reviewers"] = reviewers
        return facts

    def test_no_protection_merges_on_integration_evidence(self):
        snapshot = _snapshot()
        drive_remote_delivery(snapshot, FakeExecutor(review=self._review("none")))
        self.assertEqual(snapshot.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "accepted")
        self.assertEqual(snapshot.remote_delivery_evidence["review_basis"], "integration_review_evidence")

    def test_changes_requested_still_rework(self):
        snapshot = _snapshot()
        executor = FakeExecutor(review=self._review("none", changes=True))
        drive_remote_delivery(snapshot, executor)
        self.assertEqual(snapshot.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "rework")
        self.assertNotIn("merge", executor.calls)

    def test_protection_required_or_unknown_waits_and_names_reviewer(self):
        for protection in ("required", "unknown"):
            snapshot = _snapshot()
            executor = FakeExecutor(review=self._review(protection, reviewers=["alice"]))
            drive_remote_delivery(snapshot, executor)
            node = snapshot.nodes[REMOTE_DELIVERY_NODE_ID]
            self.assertEqual(node["status"], "blocked_unknown")
            self.assertIn("alice", node["waiting_on"])
            self.assertIn("alice", node["failure"]["reason"])
            self.assertNotIn("merge", executor.calls)
            self.assertNotIn("review", snapshot.remote_delivery_evidence["steps"])

    def test_approved_records_github_review_basis(self):
        snapshot = _snapshot()
        drive_remote_delivery(snapshot, FakeExecutor())
        self.assertEqual(snapshot.remote_delivery_evidence["review_basis"], "github_review")

    def test_digest_drift_does_not_waive(self):
        snapshot = _snapshot()
        drive_remote_delivery(snapshot, FakeExecutor(review=self._review("required")))
        snapshot.integration_review_evidence = dict(
            snapshot.integration_review_evidence, drift_marker="x")
        executor = FakeExecutor(review=self._review("none"))
        drive_remote_delivery(snapshot, executor)
        node = snapshot.nodes[REMOTE_DELIVERY_NODE_ID]
        self.assertNotEqual(node["status"], "accepted")
        self.assertNotIn("merge", executor.calls)


class GhProtectionProbeTests(unittest.TestCase):
    def _probe(self, replies):
        from unittest import mock
        from vibe_guide import remote_delivery as rd

        def fake(repo, args, timeout=120):
            reply = replies[args[1].split("/", 3)[-1]] if False else None
            for key, value in sorted(replies.items(), key=lambda kv: -len(kv[0])):
                if args[1].endswith(key):
                    if isinstance(value, Exception):
                        raise value
                    return value
            raise RemoteDeliveryFailed("gh", "404", retryable=True)
        with mock.patch.object(rd, "_run_gh", fake):
            return rd.SubprocessRemoteDeliveryExecutor(".")._review_protection()

    def test_matrix(self):
        branch_off = '{"protected": false}'
        self.assertEqual(self._probe({"/branches/main": branch_off, "/rules/branches/main": "[]"}), "none")
        self.assertEqual(self._probe({"/branches/main": branch_off,
            "/rules/branches/main": '[{"type":"pull_request","parameters":{"required_approving_review_count":1}}]'}), "required")
        self.assertEqual(self._probe({"/branches/main": '{"protected": true}',
            "/branches/main/protection": '{"required_pull_request_reviews":{"required_approving_review_count":1}}'}), "required")
        self.assertEqual(self._probe({"/branches/main": '{"protected": true}',
            "/branches/main/protection": '{}', "/rules/branches/main": "[]"}), "none")
        self.assertEqual(self._probe({"/branches/main": "not json"}), "unknown")
        self.assertEqual(self._probe({"/branches/main": branch_off}), "unknown")
        self.assertEqual(self._probe({}), "unknown")


class RemoteDeliveryEvidenceValidationTests(unittest.TestCase):
    def accepted_evidence(self):
        snapshot = _snapshot()
        drive_remote_delivery(snapshot, FakeExecutor())
        return snapshot, snapshot.remote_delivery_evidence

    def test_binding_tracks_current_snapshot_digests(self):
        snapshot, evidence = self.accepted_evidence()
        self.assertTrue(binding_matches(evidence["binding"], delivery_binding(snapshot)))
        snapshot.authorization_digest = "e" * 64
        self.assertFalse(binding_matches(evidence["binding"], delivery_binding(snapshot)))

    def test_rejects_bad_head_sha(self):
        snapshot, evidence = self.accepted_evidence()
        evidence["head_sha"] = "not-a-sha"
        with self.assertRaises(ValueError):
            validate_remote_delivery_evidence(snapshot, evidence)

    def test_rejects_non_main_target(self):
        snapshot, evidence = self.accepted_evidence()
        evidence["target_branch"] = "develop"
        with self.assertRaises(ValueError):
            validate_remote_delivery_evidence(snapshot, evidence)

    def test_rejects_missing_pr_url_on_acceptance(self):
        snapshot, evidence = self.accepted_evidence()
        evidence.pop("pr_url")
        with self.assertRaises(ValueError):
            validate_remote_delivery_evidence(snapshot, evidence)

    def test_rejects_verification_merge_commit_mismatch(self):
        snapshot, evidence = self.accepted_evidence()
        evidence["remote_verification"]["merge_commit"] = H1
        with self.assertRaises(ValueError):
            validate_remote_delivery_evidence(snapshot, evidence)

    def test_rejects_unfinished_steps_on_acceptance(self):
        snapshot, evidence = self.accepted_evidence()
        evidence["steps"].pop("verify")
        with self.assertRaises(ValueError):
            validate_remote_delivery_evidence(snapshot, evidence)


class ReauthorizationAndLineageTests(unittest.TestCase):
    def test_revoking_remote_git_actions_retires_the_ring(self):
        snapshot = _snapshot(remote="allow")
        drive_remote_delivery(snapshot, FakeExecutor())
        self.assertEqual(snapshot.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "accepted")
        snapshot.authorization = {"remote_git_actions": "deny"}
        recorded, record_event = _events()
        self.assertTrue(drive_remote_delivery(snapshot, FakeExecutor(), record_event))
        self.assertNotIn(REMOTE_DELIVERY_NODE_ID, snapshot.nodes)
        self.assertEqual(snapshot.remote_delivery_evidence, {})
        self.assertIn("remote_delivery_retired", [name for name, _ in recorded])

    def test_degenerate_digest_after_acceptance_blocks_with_a_clear_reason(self):
        snapshot = _snapshot()
        drive_remote_delivery(snapshot, FakeExecutor())
        self.assertEqual(snapshot.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "accepted")
        snapshot.node_contract_digest = ""
        # The stale binding invalidates the evidence (old facts must not be
        # reused), and the lineage guard then refuses to re-drive with a
        # clear non-retryable reason instead of a misleading retry loop.
        self.assertTrue(drive_remote_delivery(snapshot, FakeExecutor()))
        node = snapshot.nodes[REMOTE_DELIVERY_NODE_ID]
        self.assertEqual(node["status"], "blocked_unknown")
        self.assertEqual(node["failure"]["step"], "lineage")
        self.assertFalse(node["failure"]["retryable"])

    def test_incomplete_lineage_digest_refuses_with_a_clear_reason(self):
        snapshot = _snapshot()
        snapshot.node_contract_digest = ""
        executor = FakeExecutor()
        recorded, record_event = _events()
        drive_remote_delivery(snapshot, executor, record_event)
        self.assertEqual(executor.calls, [])
        node = snapshot.nodes[REMOTE_DELIVERY_NODE_ID]
        self.assertEqual(node["status"], "blocked_unknown")
        self.assertFalse(node["failure"]["retryable"])
        self.assertIn("lineage", node["failure"]["reason"])
        failed = dict(recorded)["remote_delivery_failed"]
        self.assertEqual(failed["reason_class"], "remote_delivery_lineage_incomplete")


class SubprocessExecutorTests(unittest.TestCase):
    def test_create_pr_reuses_an_open_pr_for_the_same_head(self):
        import json as _json
        from unittest import mock

        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")

        def fake_gh(repo, args, timeout=120):
            self.assertEqual(args[:2], ["pr", "list"])
            return _json.dumps([{"url": PR_URL, "number": 371, "headRefOid": H1}])

        with mock.patch("vibe_guide.remote_delivery._run_gh", side_effect=fake_gh):
            facts = executor.execute("create_pr", {"source_branch": "vibe/x", "head_sha": H1})
        self.assertTrue(facts["reused"])
        self.assertEqual(facts["pr_url"], PR_URL)

    def test_merge_is_idempotent_when_the_pr_is_already_merged(self):
        import json as _json
        from unittest import mock

        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")

        def fake_gh(repo, args, timeout=120):
            if args[:2] == ["pr", "view"]:
                return _json.dumps({
                    "state": "MERGED",
                    "headRefOid": H1,
                    "mergeCommit": {"oid": H2},
                })
            raise AssertionError("unexpected gh call: {}".format(args))

        with mock.patch("vibe_guide.remote_delivery._run_gh", side_effect=fake_gh):
            facts = executor.execute(
                "merge", {"pr_url": PR_URL, "head_sha": H1}
            )
        self.assertTrue(facts["already_merged"])
        self.assertEqual(facts["merge_commit"], H2)

    def test_merge_reads_the_merge_commit_back_when_gh_prints_no_sha(self):
        import json as _json
        from unittest import mock

        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")
        views = [
            {"state": "OPEN", "headRefOid": H1, "mergeCommit": None},
            {"state": "MERGED", "mergeCommit": {"oid": H2}},
        ]

        def fake_gh(repo, args, timeout=120):
            if args[:2] == ["pr", "view"]:
                return _json.dumps(views.pop(0))
            if args[:2] == ["pr", "merge"]:
                return ""
            raise AssertionError("unexpected gh call: {}".format(args))

        with mock.patch("vibe_guide.remote_delivery._run_gh", side_effect=fake_gh):
            facts = executor.execute("merge", {"pr_url": PR_URL, "head_sha": H1})
        self.assertEqual(facts["merge_commit"], H2)

    def test_merge_readback_failure_keeps_its_reason(self):
        import json as _json
        from unittest import mock

        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")
        calls = {"view": 0}

        def fake_gh(repo, args, timeout=120):
            if args[:2] == ["pr", "view"]:
                calls["view"] += 1
                if calls["view"] == 1:
                    return _json.dumps({"state": "OPEN", "headRefOid": H1, "mergeCommit": None})
                raise RemoteDeliveryFailed("gh", "gh pr view exited 1", retryable=True)
            return ""

        with mock.patch("vibe_guide.remote_delivery._run_gh", side_effect=fake_gh):
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                executor.execute("merge", {"pr_url": PR_URL, "head_sha": H1})
        self.assertIn("read-back failed", caught.exception.reason)
        self.assertTrue(caught.exception.retryable)

    def test_merge_rejects_a_merged_pr_whose_head_differs(self):
        import json as _json
        from unittest import mock

        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")

        def fake_gh(repo, args, timeout=120):
            return _json.dumps({
                "state": "MERGED",
                "headRefOid": H3,
                "mergeCommit": {"oid": H2},
            })

        with mock.patch("vibe_guide.remote_delivery._run_gh", side_effect=fake_gh):
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                executor.execute("merge", {"pr_url": PR_URL, "head_sha": H1})
        self.assertFalse(caught.exception.retryable)

    def test_merge_unreadable_pr_state_fails_retryable(self):
        from unittest import mock

        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")
        with mock.patch("vibe_guide.remote_delivery._run_gh", return_value="not json"):
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                executor.execute("merge", {"pr_url": PR_URL, "head_sha": H1})
        self.assertTrue(caught.exception.retryable)

    def test_create_pr_falls_back_to_creating_when_no_open_pr_matches(self):
        import json as _json
        from unittest import mock

        executor = SubprocessRemoteDeliveryExecutor("/repo", plan_id="plan-1")
        calls = []

        def fake_gh(repo, args, timeout=120):
            calls.append(args[:2])
            if args[:2] == ["pr", "list"]:
                return "[]"
            return PR_URL

        with mock.patch("vibe_guide.remote_delivery._run_gh", side_effect=fake_gh):
            facts = executor.execute("create_pr", {"source_branch": "vibe/x", "head_sha": H1})
        self.assertFalse(facts["reused"])
        self.assertIn(["pr", "create"], calls)
        self.assertEqual(facts["pr_number"], 371)


import tempfile
from pathlib import Path, PurePath as _Path

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import (
    RunEvent,
    RunSnapshot,
    append_event,
    load_snapshot,
    save_snapshot,
)


class SnapshotPersistenceTests(unittest.TestCase):
    """The load-time validator admits only the monitor-driven delivery ring."""

    def _paths_and_record(self, remote="allow", directory=None):
        paths = ProjectPaths(_Path(directory))
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        node = DAGNode("n1", "n1", [], [], None, {"files": []}, "accepted")
        allowed = (
            ("commit", "push", "create_pr", "create_mr", "merge", "review", "test")
            if remote == "allow"
            else ("review", "test")
        )
        record = authorize(
            build_authorization_card(
                plan,
                [node],
                AgentCapabilities("fake", True, True, True, True, True, "full"),
                allowed_actions=allowed,
                remote_git_actions=remote,
            ),
            "AUTHORIZE",
        )
        append_event(
            paths,
            RunEvent(
                "run_started",
                {
                    "run_id": "run-1",
                    "authorization_digest": record.digest,
                    "node_contract_digest": record.node_contract_digest,
                    "node_ids": ["n1"],
                },
            ),
        )
        return paths, record

    def _snapshot(self, record, nodes, status="running", delivery=None, event_sequence=1):
        return RunSnapshot(
            "run-1", "plan-1", 1, status, nodes, {},
            authorization=record.to_dict(),
            authorization_digest=record.digest,
            node_contract_digest=record.node_contract_digest,
            event_sequence=event_sequence,
            remote_delivery_evidence=delivery or {},
        )

    def test_allow_run_snapshot_may_carry_the_delivery_ring(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, record = self._paths_and_record("allow", directory)
            nodes = {
                "n1": {"status": "running"},
                REMOTE_DELIVERY_NODE_ID: {"status": "planned", "monitor_driven": True},
            }
            save_snapshot(paths, self._snapshot(record, nodes))
            loaded = load_snapshot(paths, "run-1")
            self.assertIn(REMOTE_DELIVERY_NODE_ID, loaded.nodes)

    def test_deny_run_snapshot_still_loads_so_the_monitor_can_retire_the_ring(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, record = self._paths_and_record("deny", directory)
            nodes = {
                "n1": {"status": "running"},
                REMOTE_DELIVERY_NODE_ID: {"status": "planned"},
            }
            save_snapshot(paths, self._snapshot(record, nodes))
            loaded = load_snapshot(paths, "run-1")
            self.assertIn(REMOTE_DELIVERY_NODE_ID, loaded.nodes)

    def _accept_n1(self, paths, record):
        contract_digest = "c" * 64
        append_event(
            paths,
            RunEvent(
                "accepted",
                {
                    "run_id": "run-1",
                    "node_id": "n1",
                    "contract_digest": contract_digest,
                    "authorization_epoch": record.digest,
                },
            ),
            provenance={
                "role": "reviewer",
                "task_id": "task-r1",
                "generation": 1,
                "authorization_digest": record.digest,
                "node_contract_digest": record.node_contract_digest,
            },
        )
        return {
            "status": "accepted",
            "branch": "node/n1",
            "contract_digest": contract_digest,
            "acceptance": {
                "contract_digest": contract_digest,
                "authorization_epoch": record.digest,
            },
            "reviewer_identity": "task-r1",
            "review_generation": 1,
        }

    def _complete_run_fixture(self, directory):
        paths, record = self._paths_and_record("allow", directory)
        snapshot = _snapshot(remote="allow")
        snapshot.plan_id = "plan-1"
        snapshot.authorization_digest = record.digest
        snapshot.node_contract_digest = record.node_contract_digest
        snapshot.nodes = {"n1": self._accept_n1(paths, record)}
        snapshot.integration_review_evidence = build_integration_review_evidence(
            snapshot, CLEARED_CLAIM,
            agentsmd_acceptance_refs=["AGENTS.md"],
            unverified_or_excluded=["deploy"],
        )
        # Drive on a full run view (integration node present), then bind the
        # delivery ring onto the minimal authorized node set this fixture's
        # authorization record carries.
        drive_view = _snapshot(remote="allow")
        drive_view.authorization_digest = record.digest
        drive_view.node_contract_digest = record.node_contract_digest
        drive_remote_delivery(drive_view, FakeExecutor())
        self.assertEqual(drive_view.nodes[REMOTE_DELIVERY_NODE_ID]["status"], "accepted")
        snapshot.nodes[REMOTE_DELIVERY_NODE_ID] = drive_view.nodes[REMOTE_DELIVERY_NODE_ID]
        snapshot.remote_delivery_evidence = drive_view.remote_delivery_evidence
        # run_started + accepted events are both lineage for this snapshot.
        snapshot.event_sequence = 2
        snapshot.status = "complete"
        return paths, record, snapshot

    def test_complete_allow_run_loads_with_verified_delivery_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, record, snapshot = self._complete_run_fixture(directory)
            save_snapshot(paths, self._snapshot(
                record, snapshot.nodes, status="complete",
                delivery=snapshot.remote_delivery_evidence, event_sequence=2,
            ))
            loaded = load_snapshot(paths, "run-1")
            self.assertEqual(loaded.status, "complete")

    def test_complete_allow_run_without_delivery_evidence_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, record, snapshot = self._complete_run_fixture(directory)
            snapshot.remote_delivery_evidence = {}
            with self.assertRaises(ValueError):
                save_snapshot(paths, self._snapshot(
                    record, snapshot.nodes, status="complete", delivery={},
                    event_sequence=2,
                ))


class PrepareRealGitTests(unittest.TestCase):
    """The _prepare fix (review P1): merges land on the source branch inside
    a dedicated temporary worktree, never on the user's checked-out branch."""

    import os
    import subprocess as _subprocess

    def _git(self, repo, *args):
        return self._subprocess.run(
            ["git", "-C", str(repo)] + list(args),
            capture_output=True, text=True, check=True,
            env={
                **self.os.environ,
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            },
        ).stdout.strip()

    def _fixture(self, directory):
        root = Path(directory)
        origin = root / "origin.git"
        repo = root / "repo"
        self._subprocess.run(["git", "init", "--bare", str(origin)], capture_output=True, check=True)
        self._subprocess.run(["git", "init", str(repo)], capture_output=True, check=True)
        (repo / "base.txt").write_text("base\n")
        self._git(repo, "add", ".")
        self._git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "base")
        self._git(repo, "branch", "-M", "main")
        self._git(repo, "remote", "add", "origin", str(origin))
        self._git(repo, "push", "-u", "origin", "main")
        for name, changes in (
            ("node/a", {"base.txt": "a-version\n", "a.txt": "a\n"}),
            ("node/b", {"b.txt": "b\n"}),
        ):
            self._git(repo, "checkout", "-b", name)
            for fname, content in changes.items():
                (repo / fname).write_text(content)
            self._git(repo, "add", ".")
            self._git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", name)
        self._git(repo, "checkout", "main")
        return repo, origin

    def test_prepare_merges_node_branches_onto_source_without_touching_checkout(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, origin = self._fixture(directory)
            main_sha = self._git(repo, "rev-parse", "main")
            executor = SubprocessRemoteDeliveryExecutor(repo, plan_id="p1")
            facts = executor.execute("prepare", {"node_branches": ["node/a", "node/b"]})
            source = facts["source_branch"]
            self.assertEqual(source, "vibe/integration-candidate-p1")
            self.assertEqual(facts["target_branch"], "main")
            self.assertNotEqual(facts["head_sha"], main_sha)
            # The user's checked-out branch and worktree are untouched.
            self.assertEqual(self._git(repo, "rev-parse", "HEAD"), main_sha)
            self.assertEqual(self._git(repo, "status", "--porcelain"), "")
            # The source branch actually carries the node content, and the
            # temporary worktree was removed.
            self._git(repo, "merge-base", "--is-ancestor", "node/a", source)
            self._git(repo, "merge-base", "--is-ancestor", "node/b", source)
            self.assertFalse((repo / ".worktrees" / "remote-delivery-p1").exists())

    def test_push_publishes_the_source_branch_and_is_idempotent(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, origin = self._fixture(directory)
            executor = SubprocessRemoteDeliveryExecutor(repo, plan_id="p1")
            prepared = executor.execute("prepare", {"node_branches": ["node/a", "node/b"]})
            pushed = executor.execute(
                "push", {"source_branch": prepared["source_branch"], "head_sha": prepared["head_sha"]}
            )
            self.assertFalse(pushed["already_pushed"])
            remote_head = self._git(repo, "ls-remote", "origin", "refs/heads/" + prepared["source_branch"]).split()[0]
            self.assertEqual(remote_head, prepared["head_sha"])
            again = executor.execute(
                "push", {"source_branch": prepared["source_branch"], "head_sha": prepared["head_sha"]}
            )
            self.assertTrue(again["already_pushed"])

    def _commit(self, repo, message):
        self._git(repo, "add", ".")
        self._git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", message)

    def _delivered_fixture(self, directory):
        """A squash-merged delivery: node/sq carries sq.txt, main holds an unrelated
        squash commit with identical content, and refs/pull/371/head is the node tip."""
        repo, origin = self._fixture(directory)
        self._git(repo, "checkout", "-q", "-b", "node/sq", "main")
        (repo / "sq.txt").write_text("sq\n")
        self._commit(repo, "node/sq")
        node_tip = self._git(repo, "rev-parse", "HEAD")
        self._git(repo, "checkout", "-q", "main")
        (repo / "sq.txt").write_text("sq\n")
        self._commit(repo, "squash of #371")
        squash = self._git(repo, "rev-parse", "HEAD")
        self._git(repo, "push", "-q", "origin", "main")
        self._git(repo, "push", "-q", "origin", "node/sq:refs/pull/371/head")
        return repo, node_tip, squash

    def _merged_listing(self, head, merge, number=371):
        import json as _json
        return _json.dumps([{"url": PR_URL, "number": number, "headRefOid": head,
                             "mergeCommit": {"oid": merge}}])

    def _prepare_with(self, repo, listing, branches):
        from unittest import mock
        executor = SubprocessRemoteDeliveryExecutor(repo, plan_id="p1")
        with mock.patch("vibe_guide.remote_delivery._run_gh", return_value=listing):
            return executor.execute("prepare", {"node_branches": branches})

    def test_prepare_reuses_a_merged_pr_that_provably_delivered_the_node_branches(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            facts = self._prepare_with(repo, self._merged_listing(node_tip, squash), ["node/sq"])
            self.assertTrue(facts["already_delivered"])
            self.assertEqual(facts["merge_commit"], squash)
            self.assertEqual(facts["pr_url"], PR_URL)

    def test_prepare_refuses_when_the_pr_head_does_not_contain_the_node_branch(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            self._git(repo, "checkout", "-q", "--orphan", "other")
            self._git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                      "--allow-empty", "-m", "other")
            other = self._git(repo, "rev-parse", "HEAD")
            self._git(repo, "push", "-q", "-f", "origin", "other:refs/pull/371/head")
            self._git(repo, "checkout", "-q", "main")
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                self._prepare_with(repo, self._merged_listing(other, squash), ["node/sq"])
            self.assertFalse(caught.exception.retryable)

    def test_prepare_refuses_a_merge_commit_that_is_not_on_main(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                self._prepare_with(repo, self._merged_listing(node_tip, node_tip), ["node/sq"])
            self.assertFalse(caught.exception.retryable)

    def test_prepare_refuses_when_every_node_tip_is_already_in_main_history(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            # base commit: in main's history AND an ancestor of the PR head, so only
            # the "some tip must be outside main" rule can refuse this PR.
            self._git(repo, "branch", "node/empty", "main~1")
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                self._prepare_with(repo, self._merged_listing(node_tip, squash), ["node/empty"])
            self.assertFalse(caught.exception.retryable)

    def test_prepare_reuses_when_only_some_node_tips_are_already_in_main(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            # node/early is already in main's history; node/sq was squashed.
            # A legitimate partial delivery must still be recognised.
            self._git(repo, "branch", "node/early", "main~1")
            facts = self._prepare_with(
                repo, self._merged_listing(node_tip, squash), ["node/sq", "node/early"])
            self.assertTrue(facts["already_delivered"])

    def test_merged_pr_refuses_a_pr_head_that_carries_only_some_of_the_node_branches(self):
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            # node/other has its own commit that the PR head does not contain.
            self._git(repo, "checkout", "-q", "-b", "node/other", "main~1")
            (repo / "other.txt").write_text("other\n")
            self._commit(repo, "node/other")
            self._git(repo, "checkout", "-q", "main")
            executor = SubprocessRemoteDeliveryExecutor(repo, plan_id="p1")
            with mock.patch("vibe_guide.remote_delivery._run_gh",
                            return_value=self._merged_listing(node_tip, squash)):
                self.assertEqual(executor._merged_pr("src", ["node/sq", "node/other"]), {})
                self.assertTrue(executor._merged_pr("src", ["node/sq"]))

    def test_prepare_skips_an_unfetchable_entry_and_still_finds_a_later_valid_pr(self):
        import json as _json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            listing = _json.dumps([
                {"url": PR_URL, "number": 999, "headRefOid": node_tip, "mergeCommit": {"oid": squash}},
                {"url": PR_URL, "number": 371, "headRefOid": node_tip, "mergeCommit": {"oid": squash}},
            ])
            facts = self._prepare_with(repo, listing, ["node/sq"])
            self.assertTrue(facts["already_delivered"])
            self.assertEqual(facts["pr_number"], 371)

    def test_prepare_with_no_node_branches_never_reuses_a_merged_pr(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                self._prepare_with(repo, self._merged_listing(node_tip, squash), [])
            self.assertFalse(caught.exception.retryable)

    def test_prepare_treats_a_failed_pr_ref_fetch_as_transient(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                self._prepare_with(repo, self._merged_listing(node_tip, squash, number=999), ["node/sq"])
            self.assertTrue(caught.exception.retryable)

    def test_prepare_fails_loudly_when_the_merged_pr_list_hits_the_limit(self):
        import json as _json
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, node_tip, squash = self._delivered_fixture(directory)
            junk = [{"url": PR_URL, "number": 1000 + n, "headRefOid": "", "mergeCommit": None}
                    for n in range(100)]
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                self._prepare_with(repo, _json.dumps(junk), ["node/sq"])
            self.assertIn("limit", caught.exception.reason)
            self.assertFalse(caught.exception.retryable)

    def test_prepare_marks_merge_conflicts_as_non_retryable(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            repo, origin = self._fixture(directory)
            self._git(repo, "checkout", "-b", "node/c")
            (repo / "base.txt").write_text("conflicting\n")
            self._git(repo, "add", ".")
            self._git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "node/c")
            self._git(repo, "checkout", "main")
            executor = SubprocessRemoteDeliveryExecutor(repo, plan_id="p1")
            with self.assertRaises(RemoteDeliveryFailed) as caught:
                executor.execute("prepare", {"node_branches": ["node/a", "node/c"]})
            self.assertFalse(caught.exception.retryable)
            self.assertFalse((repo / ".worktrees" / "remote-delivery-p1").exists())


class ReplayTests(unittest.TestCase):
    """Delivery events are audit-only on replay: the snapshot is the truth."""

    def _monitor_and_event(self, role="system"):
        from vibe_guide.monitor import Monitor

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        harness = SnapshotPersistenceTests()
        paths, record = harness._paths_and_record("allow", directory.name)
        plan = Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        node = DAGNode("n1", "n1", [], [], None, {"files": []}, "running")
        monitor = Monitor(paths, plan, [node])
        snapshot = harness._snapshot(record, {"n1": {"status": "running"}}, event_sequence=1)
        save_snapshot(paths, snapshot)
        append_event(
            paths,
            RunEvent(
                "remote_delivery_node_appended",
                {
                    "run_id": "run-1",
                    "node_id": REMOTE_DELIVERY_NODE_ID,
                    "status": "planned",
                    "source": "monitor.remote_delivery",
                },
            ),
            provenance={
                "role": role,
                "authorization_digest": record.digest,
                "node_contract_digest": record.node_contract_digest,
            },
        )
        return monitor, load_snapshot(paths, "run-1")

    def test_delivery_event_is_acknowledged_without_rebuilding_the_node(self):
        monitor, snapshot = self._monitor_and_event()
        monitor._reconcile_unapplied_events(snapshot)
        self.assertEqual(snapshot.event_sequence, 2)
        self.assertNotIn(REMOTE_DELIVERY_NODE_ID, snapshot.nodes)

    def test_delivery_event_replay_requires_system_provenance(self):
        monitor, snapshot = self._monitor_and_event(role="developer")
        with self.assertRaises(ValueError):
            monitor._reconcile_unapplied_events(snapshot)


if __name__ == "__main__":
    unittest.main()
