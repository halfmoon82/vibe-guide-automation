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
from pathlib import Path as _Path

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

    def test_deny_run_snapshot_rejects_the_delivery_ring(self):
        with tempfile.TemporaryDirectory() as directory:
            paths, record = self._paths_and_record("deny", directory)
            nodes = {
                "n1": {"status": "running"},
                REMOTE_DELIVERY_NODE_ID: {"status": "planned"},
            }
            with self.assertRaises(ValueError):
                save_snapshot(paths, self._snapshot(record, nodes))

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
