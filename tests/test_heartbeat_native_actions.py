import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore, ProviderUnavailable
from vibe_guide.models import DAGNode, WorkerProfile
from vibe_guide.monitor import Monitor
from vibe_guide.contracts import RunEvent
from vibe_guide.state import RunSnapshot
from vibe_guide.state import acquire_writer_lease, read_writer_lease
from vibe_guide.task_registry import TaskBinding
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import ProviderActionRunner


def _profile(allowlist=None):
    return WorkerProfile(
        worker="worker", model="default", reasoning="normal", fallbacks=[],
        selection_basis={"issue_complexity_ref": "issue-1", "complexity_band": "standard", "risk_tags": [], "availability_evidence": "configured"},
        worktree=".worktrees/issue-1", branch="node/issue-1", allowlist=list(allowlist or ["vibe_guide/monitor.py"]), writer="worker",
    )


class HeartbeatNativeActionTests(unittest.TestCase):
    def test_scope_expansion_updates_canonical_profile_for_reviewer(self):
        monitor = Monitor.__new__(Monitor)
        node = DAGNode("issue-1", "Issue", [], [], None, {"worker_profile": _profile().to_dict()}, "planned")
        current = {"scope_expansions": ["tests/test_issue.py"], "contract_overrides": {}}
        contract = {"worker_profile": _profile().to_dict(), "files": ["vibe_guide/monitor.py"]}
        profile = monitor._prepare_worker_profile(node, contract, current, "reviewer")
        self.assertIn("tests/test_issue.py", profile.allowlist)
        self.assertEqual(profile.to_dict(), contract["worker_profile"])

    def test_local_contract_failure_is_structured_and_does_not_advance_generation(self):
        monitor = Monitor.__new__(Monitor)

        class FailingRunner:
            def start(self, contract, worktree):
                raise ProviderUnavailable("worker model route conflicts with child binding")

        result = monitor._dispatch_with_intent(
            FailingRunner(), {"node_id": "issue-1", "role": "reviewer", "generation": 9}, {"generation": 9}
        )
        self.assertEqual(result["kind"], "contract_error")
        self.assertEqual(result["reason_code"], "worker_profile_conflict")
        self.assertEqual(result["phase"], "start")
        self.assertTrue(result["action_ref"])
        self.assertFalse(result["advance_generation"])

    def test_stale_reconciliation_requires_bound_accepted_evidence_and_keeps_request(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            (store.root / "requests").mkdir(parents=True)
            request = {
                "schema_version": 1, "action_id": "action-old", "operation": "create", "provider": "codex",
                "run_id": "run-1", "issue_id": "issue-154", "role": "developer", "generation": 2,
                "authorization_digest": "a" * 64, "native_tool": "codex_app__create_thread", "request": {"authorization_digest": "a" * 64},
                "request_digest": "b" * 64,
            }
            store._atomic(store.root / "requests" / "action-old.json", request)
            evidence = {
                "run_id": "run-1", "authorization_digest": "a" * 64,
                # The accepted role projection is the current generation;
                # the mailbox request belongs to the superseded generation.
                "nodes": {"issue-154": {"status": "accepted", "accepted_generation": 3, "roles": {"developer": {"generation": 3}}}},
            }
            changed = store.reconcile_stale("run-1", evidence)
            self.assertEqual(changed, ["action-old"])
            self.assertEqual(store.pending("run-1"), [])
            self.assertTrue((store.root / "requests" / "action-old.json").is_file())
            audit = store._read(store.root / "stale" / "action-old.json")
            self.assertEqual(audit["status"], "stale")
            self.assertEqual(audit["reason"], "accepted_generation_superseded")

    def test_stale_reconciliation_keeps_unknown_evidence_pending(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            (store.root / "requests").mkdir(parents=True)
            request = {
                "schema_version": 1, "action_id": "action-unknown", "operation": "create", "provider": "codex",
                "run_id": "run-1", "issue_id": "issue-154", "role": "developer", "generation": 2,
                "authorization_digest": "a" * 64, "native_tool": "codex_app__create_thread", "request": {"authorization_digest": "a" * 64},
                "request_digest": "b" * 64,
            }
            store._atomic(store.root / "requests" / "action-unknown.json", request)
            changed = store.reconcile_stale("run-1", {"run_id": "run-1", "nodes": {}})
            self.assertEqual(changed, [])
            self.assertEqual(len(store.pending("run-1")), 1)

    def test_stale_reconciliation_rejects_top_level_only_auth_when_envelope_exists(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            (store.root / "requests").mkdir(parents=True)
            request = {
                "schema_version": 1, "action_id": "action-partial", "operation": "create", "provider": "codex",
                "run_id": "run-1", "issue_id": "issue-154", "role": "developer", "generation": 2,
                "authorization_digest": "a" * 64, "native_tool": "codex_app__create_thread", "request": {},
                "request_digest": "b" * 64,
            }
            store._atomic(store.root / "requests" / "action-partial.json", request)
            evidence = {
                "run_id": "run-1", "authorization_digest": "a" * 64,
                "nodes": {"issue-154": {"status": "accepted", "accepted_generation": 3, "roles": {"developer": {"generation": 2}}}},
            }
            self.assertEqual(store.reconcile_stale("run-1", evidence), [])
            self.assertEqual(len(store.pending("run-1")), 1)

    def test_tampered_stale_marker_does_not_hide_request(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            (store.root / "requests").mkdir(parents=True)
            (store.root / "stale").mkdir(parents=True)
            request = {
                "schema_version": 1, "action_id": "action-tampered", "operation": "create", "provider": "codex",
                "run_id": "run-1", "issue_id": "issue-154", "role": "developer", "generation": 2,
                "request": {"authorization_digest": "a" * 64}, "request_digest": "b" * 64,
            }
            store._atomic(store.root / "requests" / "action-tampered.json", request)
            store._atomic(store.root / "stale" / "action-tampered.json", {"status": "stale", "action_id": "wrong", "request_digest": "b" * 64})
            self.assertEqual(len(store.pending("run-1")), 1)

    def test_stale_reconciliation_uses_role_specific_accepted_generation(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            request_dir = store.root / "requests"
            request_dir.mkdir(parents=True)
            request = {
                "schema_version": 1, "action_id": "review-old", "operation": "create", "provider": "codex",
                "run_id": "run-1", "issue_id": "issue-1", "role": "reviewer", "generation": 1,
                "authorization_digest": "a" * 64, "request": {"authorization_digest": "a" * 64},
                "request_digest": "b" * 64,
            }
            store._atomic(request_dir / "action-review-old.json", request)
            evidence = {
                "run_id": "run-1", "authorization_digest": "a" * 64,
                "nodes": {"issue-1": {
                    "status": "accepted", "accepted_generation": 3,
                    "roles": {"developer": {"generation": 3, "accepted_generation": 3},
                              "reviewer": {"generation": 1, "accepted_generation": 1}},
                }},
            }
            self.assertEqual(store.reconcile_stale("run-1", evidence), [])
            self.assertEqual(len(store.pending("run-1")), 1)

    def test_stale_reconciliation_closes_old_reviewer_when_developer_is_newer(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            request_dir = store.root / "requests"
            request_dir.mkdir(parents=True)
            store._atomic(request_dir / "action-review-old.json", {
                "schema_version": 1, "action_id": "review-old", "operation": "create",
                "provider": "codex", "run_id": "run-1", "issue_id": "issue-1",
                "role": "reviewer", "generation": 1,
                "authorization_digest": "a" * 64,
                "request": {"authorization_digest": "a" * 64},
                "request_digest": "b" * 64,
            })
            evidence = {
                "run_id": "run-1", "authorization_digest": "a" * 64,
                "nodes": {"issue-1": {
                    "status": "accepted", "accepted_generation": 3,
                    "roles": {
                        "developer": {"generation": 3, "accepted_generation": 3},
                        "reviewer": {"generation": 2, "accepted_generation": 2},
                    },
                }},
            }
            self.assertEqual(store.reconcile_stale("run-1", evidence), ["review-old"])
            self.assertEqual(store.pending("run-1"), [])

    def test_symlinked_stale_directory_cannot_hide_pending_request(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            paths = ProjectPaths(Path(root))
            store = ProviderActionStore(paths)
            request_dir = store.root / "requests"
            request_dir.mkdir(parents=True)
            store._atomic(request_dir / "action-x.json", {
                "schema_version": 1, "action_id": "x", "operation": "create", "run_id": "run-1",
                "issue_id": "issue-1", "role": "developer", "generation": 1,
                "request_digest": "a" * 64, "request": {},
            })
            (store.root / "stale").symlink_to(Path(outside), target_is_directory=True)
            with self.assertRaises(ValueError):
                store.pending("run-1")

    def test_reviewer_continuation_prompt_binds_generation_and_review_evidence(self):
        runner = ProviderActionRunner.__new__(ProviderActionRunner)
        prompt = runner._continuation_prompt({
            "node_id": "issue-148", "role": "reviewer", "generation": 10,
            "delivery_path": "deliveries/issue-148", "status_file": "reviewer-status.json",
            "completion_marker": "reviewer-status.json:g10",
        })
        self.assertIn("generation 10", prompt)
        self.assertIn("业务 review", prompt)
        self.assertIn("deliveries/issue-148", prompt)
        self.assertIn("reviewer-status.json", prompt)
        self.assertIn("reviewer-status.json:g10", prompt)

    def test_continuation_carries_real_product_contract_for_both_roles(self):
        runner = ProviderActionRunner.__new__(ProviderActionRunner)
        for role in ('developer','reviewer'):
            prompt=runner._continuation_prompt({'node_id':'issue-148','role':role,'generation':10,
                'input':'类型错误', 'output':'结构化错误分类', 'error_behavior':'保留unknown',
                'acceptance_example':'超时保持可恢复', 'spec_path':'docs/approved.md',
                'worktree':'.worktrees/issue-148','branch':'node/issue-148','files':['src/errors.py'],
                'status_file':'reviewer-status.json','handoff_file':'reviewer-delivery.md','delivery_path':'developer-delivery.md'})
            for value in ('结构化错误分类','保留unknown','超时保持可恢复','docs/approved.md','.worktrees/issue-148','src/errors.py','reviewer-delivery.md','执行请求'):
                self.assertIn(value,prompt)

    def test_delivered_event_projects_real_delivery_evidence_for_reviewer_continuation(self):
        monitor = Monitor.__new__(Monitor)
        monitor.nodes = {
            "issue-148": DAGNode(
                "issue-148", "Issue", [], [], None,
                {"status_file": "reviewer-status.json", "files": ["src/app.py"]},
                "planned",
            )
        }
        snapshot = RunSnapshot(
            "run-1", "plan-1", 1, "running",
            {"issue-148": {
                "status": "running", "active_role": "developer",
                "active_task": {"role": "developer", "task_id": "dev-1", "generation": 3, "handle_id": "h-1"},
                "developer_identity": "dev-1", "developer_generation": 3,
                "reviewer_started": False, "reviewer_identity": None,
                "review_generation": 0, "worktree": ".worktrees/issue-148", "branch": "node/issue-148",
            }}, {"issue-148": "h-1"}, execution_engine="legacy")
        monitor._require_snapshot_authorization = lambda _snapshot: None
        monitor._record_runner_event = lambda *_args: None
        monitor._set_binding_status = lambda *_args: None
        monitor._load_task_binding = lambda *_args: TaskBinding(
            provider="fake", mode="background", issue_id="issue-148", role="developer",
            task_id="dev-1", worktree=".worktrees/issue-148", branch="node/issue-148",
            run_id="run-1", status="running", generation=3,
        )
        monitor._start_task = lambda *args: None
        event = RunEvent("delivered", {
            "node_id": "issue-148", "role": "developer", "task_id": "dev-1",
            "handle_id": "h-1", "generation": 3,
            "delivery_evidence": {
                "completion_marker": "DONE:g3", "delivery_path": "deliveries/issue-148/g3",
                "thread_status": "complete",
            },
        })
        monitor._apply_event(snapshot, "issue-148", "h-1", event, object())
        self.assertEqual(snapshot.nodes["issue-148"]["delivery_evidence"], {
            "completion_marker": "DONE:g3", "delivery_path": "deliveries/issue-148/g3",
            "thread_status": "complete",
        })
        self.assertEqual(snapshot.nodes["issue-148"]["delivery_path"], "deliveries/issue-148/g3")

    def test_profile_validation_failure_after_lease_is_structured_and_releases_lease(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor.plan = type("Plan", (), {"version": 1})()
            node = DAGNode(
                "issue-1", "Issue", [], [], None,
                {"files": ["src/app.py"], "worker": "worker"}, "planned",
            )
            monitor.nodes = {"issue-1": node}
            current = {
                "status": "planned", "worker": "worker", "worktree": ".worktrees/issue-1",
                "branch": "node/issue-1", "developer_generation": 0,
                "review_generation": 0, "reviewer_started": False,
                "retryable_action": None, "active_task": None,
                "active_role": None, "start_intent": None,
            }
            snapshot = RunSnapshot(
                "run-1", "plan-1", 1, "running", {"issue-1": current}, {},
                authorization_digest="a" * 64,
            )
            monitor._require_snapshot_authorization = lambda _snapshot: type(
                "Auth", (), {"allowed_actions": [], "file_scope": [], "digest": "a" * 64,
                              "execution_engine": "monitor", "engine_mode": "parallel",
                              "engine_evidence_ref": "e", "node_ids": ("issue-1",)}
            )()
            monitor._supervisor_writer_rejection = lambda *_args: None
            monitor._node_dispatch_topology = lambda *_args: "dual-visible"
            monitor._consistency_binding = lambda *_args: {}
            monitor._prepare_worker_profile = lambda *_args: (_ for _ in ()).throw(
                ValueError("worker model profile is invalid")
            )
            monitor._record = lambda *_args, **_kwargs: None
            monitor._mark_blocked_unknown = lambda snap, node_id, reason, **_kwargs: snap.nodes[node_id].update(
                {"status": "blocked_unknown", "reason": reason}
            )
            self.assertTrue(acquire_writer_lease(
                paths, "issue-1", current["worktree"], "run-1", "digest", "developer", 1
            ))
            from unittest.mock import patch
            with patch("vibe_guide.monitor.save_snapshot"):
                result = monitor._start_task(snapshot, "issue-1", "developer", "develop", object(), False)
            self.assertFalse(result)
            self.assertEqual(current["status"], "blocked_unknown")
            self.assertEqual(current["developer_generation"], 0)
            self.assertEqual(current["retryable_action"]["reason_code"], "dispatch_contract_invalid")
            self.assertIsNone(read_writer_lease(paths, "issue-1", current["worktree"]))


if __name__ == "__main__":
    unittest.main()
