"""ISSUE-91 worker-push-delivery: worker self-report CLI + dispatch steps.

Anchors: missing completion_marker -> nonzero exit, nothing on disk; a legal
report is consumed by the next resume via the wait result; identical re-
reports stay idempotent; dispatch prompt carries the fixed completion steps.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import (
    ProviderActionStore,
    _requested_project_mount,
)
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths


def _store(root):
    return ProviderActionStore(ProjectPaths(Path(root)))

def _wait_request(store, run_id="run-1", node="n1"):
    return store.request(
        operation="wait",
        provider="codex",
        run_id=run_id,
        issue_id=node,
        role="developer",
        generation=1,
        native_tool="codex_app__wait_threads",
        request={"threadId": "t-1", "timeoutMs": 0},
    )

def _dispatched(store, generation=1, run_id="run-1", node="n1"):
    """Record the create the monitor sends before any worker can report."""
    action_id = "action-create-{}-{}-g{}".format(run_id, node, generation)
    (store._directory("requests") / (action_id + ".json")).write_text(json.dumps({
        "schema_version": 1, "action_id": action_id, "operation": "create",
        "provider": "codex", "run_id": run_id, "issue_id": node,
        "role": "developer", "generation": generation, "sequence": 0,
        "native_tool": "codex_app__create_thread", "request": {"prompt": "x"},
        "request_digest": "0" * 64,
    }), encoding="utf-8")


GOOD = {
    "delivery_evidence": {
        "completion_marker": "DELIVERY_COMPLETE",
        "delivery_path": ".vibe/runs/run-1/n1/delivery.md",
        "thread_status": "completed",
    },
}


class WorkerDeliveryTests(unittest.TestCase):
    def test_missing_marker_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            with self.assertRaises(ValueError):
                store.record_worker_delivery(
                    "run-1", "n1", "developer", {"delivery_path": "x"}
                , 1)
            self.assertFalse((Path(d) / "deliveries").exists())

    def test_legal_report_completes_pending_wait(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            action = _wait_request(store)
            outcome = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            , 1)
            self.assertTrue(outcome["consumed"])
            self.assertEqual(store.pending(), [])
            result = store.result(action["action_id"])
            self.assertEqual(
                result["delivery_evidence"]["completion_marker"], "DELIVERY_COMPLETE"
            )

    def test_duplicate_report_is_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            _wait_request(store)
            first = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            , 1)
            second = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            , 1)
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            results = list(
                (Path(d) / ".vibe" / "provider-actions" / "results").glob("action-*.json")
            )
            self.assertEqual(len(results), 1, "no second event/result")

    def test_report_without_pending_wait_still_lands_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            _dispatched(store)
            outcome = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            , 1)
            self.assertFalse(outcome["consumed"])
            records = list((Path(d) / ".vibe" / "provider-actions" / "deliveries").glob("*.json"))
            self.assertEqual(len(records), 1)


    def test_flattened_evidence_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            with self.assertRaises(ValueError):
                store.record_worker_delivery(
                    "run-1", "n1", "developer", dict(GOOD["delivery_evidence"])
                , 1)
            self.assertFalse(
                (Path(d) / ".vibe" / "provider-actions" / "deliveries").exists()
            )

    def test_path_components_cannot_escape_the_mailbox(self):
        with tempfile.TemporaryDirectory() as d:
            project = Path(d) / "project"
            project.mkdir()
            store = _store(project)
            for run_id, node, role in (
                ("../../../../escaped", "n1", "developer"),
                ("x/../..", "n1", "developer"),
                ("run-1", "../n1", "developer"),
                ("run-1", "n1", "../.."),
            ):
                with self.subTest(run_id=run_id, node=node, role=role):
                    with self.assertRaises(ValueError):
                        store.record_worker_delivery(run_id, node, role, dict(GOOD), 1)
            written = [p for p in Path(d).rglob("*.json")]
            self.assertEqual(written, [])

    def test_consumed_report_reaches_monitor_as_delivered_event(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        with tempfile.TemporaryDirectory() as d:
            runner = ProviderActionRunner(
                ProjectPaths(Path(d)), "codex", "codex-app-visible"
            )
            action = _wait_request(runner.store)
            review = {
                "protocol": "vibe_guide/protocols/visible-sdd-worker.md",
                "evidence_ref": "session#1",
                "clearance": {"p0": 0, "p1": 0, "p2": 0},
            }
            runner.store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD, in_session_review=review)
            , 1)
            result = runner.store.result(action["action_id"])
            handle = type("Handle", (), {"run_id": "h1"})()
            metadata = {"node_id": "n1", "role": "developer", "run_id": "run-1"}
            runner.store._atomic(runner._handle_path(handle.run_id), metadata)
            events = runner._wait_result(handle, metadata, {"node_id": "n1"}, result)
            self.assertEqual([event.event for event in events], ["delivered"])
            self.assertEqual(
                events[0].data["delivery_evidence"], GOOD["delivery_evidence"]
            )
            self.assertEqual(events[0].data["in_session_review"], review)

    def test_report_before_wait_is_picked_up_by_the_next_wait(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as d, patch(
            "vibe_guide.adapters.task_provider.require_entry"
        ):
            # A live run always has state.json; the entry gate itself is
            # covered elsewhere, so it is stubbed to let the wait through.
            (Path(d) / ".vibe").mkdir()
            (Path(d) / ".vibe" / "state.json").write_text("{}", encoding="utf-8")
            store = _store(d)
            _dispatched(store)
            early = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            , 1)
            self.assertFalse(early["consumed"])
            self.assertEqual(store.unconsumed_deliveries("run-1"), ["n1"])
            action = _wait_request(store)
            result = store.result(action["action_id"])
            self.assertIsNotNone(result, "archived delivery must fill the new wait")
            self.assertEqual(result["event"], "delivered")
            self.assertEqual(store.unconsumed_deliveries("run-1"), [])
            again = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            , 1)
            self.assertTrue(again["duplicate"])
            self.assertFalse(again["consumed"])


class GenerationBindingTests(unittest.TestCase):
    """Archived self-reports are bound to one node generation."""

    def setUp(self):
        from unittest.mock import patch

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        # A live run always has state.json; the entry gate itself is covered
        # elsewhere, so it is stubbed to let several waits through.
        (self.root / ".vibe").mkdir()
        (self.root / ".vibe" / "state.json").write_text("{}", encoding="utf-8")
        gate = patch("vibe_guide.adapters.task_provider.require_entry")
        gate.start()
        self.addCleanup(gate.stop)
        self.store = _store(self.root)

    def _wait(self, generation, sequence=1, purpose=None):
        request = {"threadId": "t-1", "timeoutMs": 0}
        if purpose:
            request["purpose"] = purpose
        return self.store.request(
            operation="wait", provider="codex", run_id="run-1", issue_id="n1",
            role="developer", generation=generation,
            native_tool="codex_app__wait_threads", request=request,
            sequence=sequence,
        )

    def test_binding_probe_wait_never_takes_the_delivery(self):
        _dispatched(self.store)
        self.store.record_worker_delivery("run-1", "n1", "developer", dict(GOOD), 1)
        probe = self._wait(1, sequence=0, purpose="binding_probe")
        self.assertIsNone(self.store.result(probe["action_id"]))
        real = self._wait(1, sequence=1)
        self.assertEqual(self.store.result(real["action_id"])["event"], "delivered")

    def test_rework_generation_never_takes_an_older_report(self):
        _dispatched(self.store)
        self.store.record_worker_delivery("run-1", "n1", "developer", dict(GOOD), 1)
        rework = self._wait(2)
        self.assertIsNone(self.store.result(rework["action_id"]))
        self.assertEqual(self.store.unconsumed_deliveries("run-1"), [])

    def test_same_payload_in_a_new_generation_is_not_a_duplicate(self):
        first = self._wait(1)
        self.store.record_worker_delivery("run-1", "n1", "developer", dict(GOOD), 1)
        self.assertIsNotNone(self.store.result(first["action_id"]))
        second = self._wait(2)
        outcome = self.store.record_worker_delivery(
            "run-1", "n1", "developer", dict(GOOD), 2
        )
        self.assertFalse(outcome["duplicate"])
        self.assertTrue(outcome["consumed"])
        self.assertEqual(self.store.result(second["action_id"])["event"], "delivered")

    def test_stale_generation_report_is_refused_not_recorded(self):
        first = self._wait(1)
        self.store.record_worker_delivery("run-1", "n1", "developer", dict(GOOD), 1)
        self.assertIsNotNone(self.store.result(first["action_id"]))
        rework = self._wait(2)
        newer = {"delivery_evidence": dict(GOOD["delivery_evidence"], delivery_path="v2")}
        with self.assertRaises(ValueError):
            self.store.record_worker_delivery("run-1", "n1", "developer", newer, 1)
        self.assertIsNone(self.store.result(rework["action_id"]))
        self.assertFalse(
            self.store._delivery_path("run-1", "n1", "developer", 1).read_text(
                encoding="utf-8"
            ).count("v2")
        )

    def test_future_generation_report_is_refused_not_archived(self):
        current = self._wait(1)
        with self.assertRaises(ValueError):
            self.store.record_worker_delivery("run-1", "n1", "developer", dict(GOOD), 4)
        self.assertFalse(
            self.store._delivery_path("run-1", "n1", "developer", 4).exists()
        )
        self.assertEqual(self.store.unconsumed_deliveries("run-1"), [])
        self.assertIsNone(self.store.result(current["action_id"]))

    def test_undispatched_node_report_is_refused_not_archived(self):
        _dispatched(self.store)
        with self.assertRaises(ValueError):
            self.store.record_worker_delivery("run-1", "n1x", "developer", dict(GOOD), 1)
        with self.assertRaises(ValueError):
            self.store.record_worker_delivery("run-1", "n1", "reviewer", dict(GOOD), 1)
        with self.assertRaises(ValueError):
            self.store.record_worker_delivery("run-2", "n1", "developer", dict(GOOD), 1)
        self.assertEqual(self.store.unconsumed_deliveries("run-1"), [])
        self.assertEqual(self.store.unconsumed_deliveries("run-2"), [])

    def test_symlinked_deliveries_dir_is_refused(self):
        outside = self.root / "outside"
        outside.mkdir()
        actions = self.root / ".vibe" / "provider-actions"
        actions.mkdir(parents=True)
        (actions / "deliveries").symlink_to(outside, target_is_directory=True)
        with self.assertRaises((OSError, ValueError)):
            self.store.record_worker_delivery("run-1", "n1", "developer", dict(GOOD), 1)
        self.assertEqual(list(outside.iterdir()), [])


class WorkerDeliverCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()

    def cli(self, argv):
        return run_cli(argv + ["--json"], self.root)

    def test_missing_marker_exits_nonzero_and_writes_nothing(self):
        result = self.cli([
            "worker-deliver", "--generation", "1", "--run-id", "run-1", "--node", "n1",
            "--payload", json.dumps({"delivery_path": "x"}),
        ])
        self.assertNotEqual(result.exit_code, 0)
        self.assertFalse(
            (self.root / ".vibe" / "provider-actions" / "deliveries").exists()
        )

    def test_legal_payload_records_and_consumes(self):
        store = _store(self.root)
        _wait_request(store)
        result = self.cli([
            "worker-deliver", "--generation", "1", "--run-id", "run-1", "--node", "n1",
            "--payload", json.dumps(GOOD),
        ])
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertTrue(result.payload["consumed"])


    def test_escaping_run_id_is_blocked(self):
        result = self.cli([
            "worker-deliver", "--generation", "1", "--run-id", "../../escaped", "--node", "n1",
            "--payload", json.dumps(GOOD),
        ])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "blocked_invalid")
        self.assertFalse((self.root.parent / "escaped-n1-developer.json").exists())

    def test_delivery_warning_reaches_the_payload_and_the_message(self):
        # ISSUE-219: the advisory must reach the worker's eyes, not only the
        # JSON -- the worker is who can tell the supervisor to re-dispatch.
        store = _store(self.root)
        _wait_request(store)
        _dispatched(store)
        path = store._dispatch_head_path("run-1", "n1", "developer", 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"head": "a" * 40, "project_mount": "none"}),
                        encoding="utf-8")
        result = self.cli([
            "worker-deliver", "--generation", "1", "--run-id", "run-1", "--node", "n1",
            "--payload", json.dumps(GOOD),
        ])
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertIn("asked the host for no project mount",
                      result.payload["warnings"][0])
        self.assertIn("注意：", result.text)

    def test_delivery_without_a_warning_keeps_the_plain_message(self):
        store = _store(self.root)
        _wait_request(store)
        _dispatched(store)
        path = store._dispatch_head_path("run-1", "n1", "developer", 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"head": "a" * 40,
                                    "project_mount": "project:p-1"}),
                        encoding="utf-8")
        result = self.cli([
            "worker-deliver", "--generation", "1", "--run-id", "run-1", "--node", "n1",
            "--payload", json.dumps(GOOD),
        ])
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertNotIn("warnings", result.payload)
        self.assertNotIn("注意：", result.text)

    def test_legacy_record_keeps_the_plain_message(self):
        # A record written before the mount field existed must not warn: those
        # are the runs that already worked (review P2).
        store = _store(self.root)
        _wait_request(store)
        _dispatched(store)
        path = store._dispatch_head_path("run-1", "n1", "developer", 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"head": "a" * 40}), encoding="utf-8")
        result = self.cli([
            "worker-deliver", "--generation", "1", "--run-id", "run-1", "--node", "n1",
            "--payload", json.dumps(GOOD),
        ])
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertNotIn("warnings", result.payload)
        self.assertNotIn("注意：", result.text)


def subprocess_out(cwd, *args):
    import subprocess

    return subprocess.run(["git", "-C", str(cwd)] + list(args), check=True,
                          capture_output=True, text=True).stdout.strip()


class LandingCheckTests(unittest.TestCase):
    """A developer report is refused when the work is not in its worktree.

    Uses a real git repo + worktree: the check reads git facts, not the
    worker's claim, so a fake tree would test nothing.
    """

    def setUp(self):
        import subprocess

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()

        def git(*args, cwd=self.root):
            subprocess.run(["git", "-C", str(cwd)] + list(args), check=True,
                           capture_output=True)

        self.git = git
        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (self.root / "app.py").write_text("v1\n", encoding="utf-8")
        (self.root / ".gitignore").write_text(".vibe/\n.worktrees/\n", encoding="utf-8")
        git("add", "app.py", ".gitignore")
        git("commit", "-q", "-m", "init")
        git("worktree", "add", "-q", "-b", "node/n1", ".worktrees/n1")
        self.store = _store(self.root)

    def _dispatch(self, worktree=".worktrees/n1", branch="node/n1", role="developer",
                  allowlist=("app.py",), project_id="project-1", generation=1):
        request = {
            "child_binding": {
                "worktree": worktree, "branch": branch, "allowlist": list(allowlist),
            },
            "target": {"type": "project", "projectId": project_id},
        }
        action_id = "action-create-run-1-n1-{}-g{}".format(role, generation)
        (self.store._directory("requests") / (action_id + ".json")).write_text(json.dumps({
            "schema_version": 1, "action_id": action_id, "operation": "create",
            "provider": "workbuddy", "run_id": "run-1", "issue_id": "n1",
            "role": role, "generation": generation, "sequence": 0, "native_tool": "x",
            "request": request,
            "request_digest": "0" * 64,
        }), encoding="utf-8")
        # Mirrors ProviderActionStore.request for a child-bound dispatch.  The
        # mount value comes from the production helper, so a change to what a
        # request records cannot drift away from what these tests assert.
        self.store._record_dispatch_head(
            "run-1", "n1", role, generation, request["child_binding"],
            _requested_project_mount(request, "create"),
        )

    def _deliver(self, role="developer"):
        return self.store.record_worker_delivery("run-1", "n1", role, dict(GOOD), 1)

    def _archived(self):
        return list((self.root / ".vibe" / "provider-actions" / "deliveries").glob("*.json"))

    def test_work_in_the_worktree_is_recorded(self):
        self._dispatch()
        (self.root / ".worktrees" / "n1" / "app.py").write_text("v2\n", encoding="utf-8")
        self.assertTrue(self._deliver()["recorded"])
        self.assertEqual(len(self._archived()), 1)

    def test_allowlisted_edit_in_main_tree_is_refused(self):
        self._dispatch()
        (self.root / "app.py").write_text("v2 in the wrong tree\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "allowlisted files: app.py"):
            self._deliver()
        self.assertEqual(self._archived(), [])

    def test_missing_worktree_is_refused(self):
        self._dispatch(worktree=".worktrees/gone")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self._deliver()
        self.assertEqual(self._archived(), [])

    def test_worktree_on_wrong_branch_is_refused(self):
        self._dispatch()
        self.git("checkout", "-q", "-b", "other", cwd=self.root / ".worktrees" / "n1")
        with self.assertRaisesRegex(ValueError, "contract says 'node/n1'"):
            self._deliver()

    def test_reviewer_report_is_not_landing_checked(self):
        # Reviewers are read-only; a user's own main-tree edit must not
        # block their verdict.
        self._dispatch(role="reviewer")
        (self.root / "app.py").write_text("user edit\n", encoding="utf-8")
        self.assertTrue(self._deliver(role="reviewer")["recorded"])

    def test_dot_worktree_means_main_tree_by_contract(self):
        self._dispatch(worktree=".", branch="")
        (self.root / "app.py").write_text("v2 in main by contract\n", encoding="utf-8")
        self.assertTrue(self._deliver()["recorded"])

    def test_absolute_worktree_path_is_resolved(self):
        self._dispatch(worktree=str(self.root / ".worktrees" / "n1"))
        self.assertTrue(self._deliver()["recorded"])

    def test_untracked_allowlisted_file_in_main_tree_is_refused(self):
        self._dispatch(allowlist=["app.py", "pkg/new.py"])
        (self.root / "pkg").mkdir()
        (self.root / "pkg" / "new.py").write_text("x\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, r"allowlisted files: pkg/new\.py \("):
            self._deliver()

    def test_rename_reports_the_new_path_only(self):
        self._dispatch(allowlist=["app.py", "new.py"])
        self.git("mv", "app.py", "new.py")
        with self.assertRaisesRegex(ValueError, r"allowlisted files: new\.py \("):
            self._deliver()

    def test_worktree_side_rename_reports_the_new_path_only(self):
        self._dispatch(allowlist=["app.py", "new.py"])
        (self.root / "app.py").rename(self.root / "new.py")
        self.git("add", "-N", "new.py")
        with self.assertRaisesRegex(ValueError, r"allowlisted files: new\.py \("):
            self._deliver()

    def test_unreadable_git_is_reported_as_unverifiable(self):
        plain = Path(self.tmp.name) / "not-a-repo"
        plain.mkdir()
        self._dispatch(worktree=str(plain))
        with self.assertRaisesRegex(ValueError, "^cannot verify delivery landing"):
            self._deliver()

    def test_work_committed_onto_main_after_dispatch_is_refused(self):
        self._dispatch()
        (self.root / "app.py").write_text("v2\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "misplaced")
        with self.assertRaisesRegex(ValueError, "committed this node's allowlisted files after it was dispatched"):
            self._deliver()
        self.assertEqual(self._archived(), [])

    def test_unrelated_commit_after_dispatch_is_recorded(self):
        self._dispatch()
        (self.root / "other.py").write_text("x\n", encoding="utf-8")
        self.git("add", "other.py")
        self.git("commit", "-q", "-m", "unrelated")
        self.assertTrue(self._deliver()["recorded"])

    def test_commit_before_dispatch_is_not_counted(self):
        # A post-merge rework: earlier generations already landed on main.
        (self.root / "app.py").write_text("merged earlier\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "merged")
        self._dispatch()
        self.assertTrue(self._deliver()["recorded"])

    def test_dispatch_without_recorded_head_is_not_history_checked(self):
        # ponytail ceiling: dispatches made before dispatch-heads existed.
        self._dispatch()
        self.store._dispatch_head_path("run-1", "n1", "developer", 1).unlink()
        (self.root / "app.py").write_text("v2\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "misplaced")
        self.assertTrue(self._deliver()["recorded"])

    def test_branch_switch_since_dispatch_is_unverifiable(self):
        # A rewritten / switched main history would list unrelated commits.
        self._dispatch()
        path = self.store._dispatch_head_path("run-1", "n1", "developer", 1)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["head"] = "f" * 40
        path.write_text(json.dumps(record), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "^cannot verify delivery landing: main project HEAD no longer descends"):
            self._deliver()

    def test_real_branch_switch_since_dispatch_is_unverifiable(self):
        # Main moved to a branch that does not contain the dispatch baseline.
        self._dispatch()
        (self.root / "app.py").write_text("on main after dispatch\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "main moves")
        self.git("checkout", "-q", "-b", "side", "HEAD~1")
        self.git("checkout", "-q", "--orphan", "fresh-history")
        self.git("commit", "-q", "-m", "rewritten")
        with self.assertRaisesRegex(ValueError, "no longer descends from the dispatch baseline"):
            self._deliver()

    def test_symlinked_dispatch_record_is_not_written_through(self):
        fresh = self._fresh_repo("fresh")
        store = _store(fresh)
        outside = Path(self.tmp.name) / "outside.json"
        heads = store._directory("dispatch-heads")
        (heads / store._dispatch_head_path("run-9", "n9", "developer", 1).name).symlink_to(outside)
        store._record_dispatch_head("run-9", "n9", "developer", 1, {"worktree": "."})
        self.assertFalse(outside.exists())

    def test_failed_record_write_leaves_no_partial_file(self):
        from unittest.mock import patch

        fresh = self._fresh_repo("fresh")
        store = _store(fresh)
        with patch("vibe_guide.adapters.task_provider.os.write", side_effect=OSError("disk full")):
            store._record_dispatch_head("run-9", "n9", "developer", 1, {"worktree": "."})
        self.assertFalse(store._dispatch_head_path("run-9", "n9", "developer", 1).exists())


    def test_overlapping_merge_does_not_block_work_done_in_the_worktree(self):
        # Review P1: the supervisor merges another node touching app.py into
        # main while this node did its own work in its worktree.
        self._dispatch()
        wt = self.root / ".worktrees" / "n1"
        (wt / "app.py").write_text("n1 work\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "n1 work", cwd=wt)
        (self.root / "app.py").write_text("other node merged\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "merge other node")
        self.assertTrue(self._deliver()["recorded"])

    def test_uncommitted_worktree_work_also_counts_as_own_work(self):
        self._dispatch()
        (self.root / ".worktrees" / "n1" / "app.py").write_text("wip\n", encoding="utf-8")
        (self.root / "app.py").write_text("other node merged\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "merge other node")
        self.assertTrue(self._deliver()["recorded"])

    def test_rework_commits_from_earlier_generations_do_not_count(self):
        # Generation 1 committed on the node branch; generation 2 (the one
        # dispatched here) is a report-only rework and main moved meanwhile.
        wt = self.root / ".worktrees" / "n1"
        (wt / "app.py").write_text("gen1\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "gen1", cwd=wt)
        self._dispatch()
        (self.root / "app.py").write_text("misplaced\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "misplaced")
        with self.assertRaisesRegex(ValueError, "committed this node's allowlisted files"):
            self._deliver()

    def test_committed_refusal_does_not_tell_the_worker_to_revert(self):
        self._dispatch()
        (self.root / "app.py").write_text("v2\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "misplaced")
        with self.assertRaises(ValueError) as caught:
            self._deliver()
        self.assertIn("do not revert commits you did not make", str(caught.exception))
        self.assertNotIn("restore the main tree", str(caught.exception))

    def test_unwritable_dispatch_heads_does_not_fail_the_request(self):
        # Creating .vibe/ first would trip the session gate, so the blocked
        # directory is simulated at the one place request() touches it.
        from unittest.mock import patch

        fresh = self._fresh_repo("fresh")
        store = _store(fresh)
        real = store._directory

        def directory(name):
            if name == "dispatch-heads":
                raise PermissionError("read-only")
            return real(name)

        with patch.object(store, "_directory", side_effect=directory):
            store.request(operation="create", provider="workbuddy", run_id="run-9",
                          issue_id="n9", role="developer", generation=1, native_tool="x",
                          request={"child_binding": {"worktree": ".worktrees/n9"}})
        self.assertEqual(len(list(store._directory("requests").glob("action-*.json"))), 1)
        self.assertIsNone(store._dispatch_head("run-9", "n9", "developer", 1))

    def test_dispatch_head_is_recorded_before_the_request_is_visible(self):
        # Review P2: a host that sees the request may start work at once.
        from unittest.mock import patch

        fresh = self._fresh_repo("fresh")
        store = _store(fresh)
        seen = []
        real = store._record_dispatch_head

        def record(*args):
            seen.append(list(store._directory("requests").glob("action-*.json")))
            return real(*args)

        with patch.object(store, "_record_dispatch_head", side_effect=record):
            store.request(operation="create", provider="workbuddy", run_id="run-9",
                          issue_id="n9", role="developer", generation=1, native_tool="x",
                          request={"child_binding": {"worktree": ".worktrees/n9"}})
        self.assertEqual(seen, [[]])

    def test_torn_dispatch_record_skips_the_history_check(self):
        self._dispatch()
        self.store._dispatch_head_path("run-1", "n1", "developer", 1).write_text("{", encoding="utf-8")
        (self.root / "app.py").write_text("v2\n", encoding="utf-8")
        self.git("commit", "-q", "-am", "misplaced")
        self.assertTrue(self._deliver()["recorded"])

    def _fresh_repo(self, name):
        # No .vibe yet: the request gate lets only a pre-session store through
        # without a full session, which is enough to exercise request().
        fresh = Path(self.tmp.name) / name
        fresh.mkdir()
        for args in (("init", "-q", "-b", "main"), ("config", "user.email", "t@t"),
                     ("config", "user.name", "t")):
            self.git(*args, cwd=fresh)
        (fresh / ".gitignore").write_text(".vibe/\n", encoding="utf-8")
        self.git("add", ".gitignore", cwd=fresh)
        self.git("commit", "-q", "-m", "init", cwd=fresh)
        return fresh

    def test_child_bound_request_records_the_dispatch_head(self):
        fresh = self._fresh_repo("fresh")
        store = _store(fresh)
        store.request(operation="create", provider="workbuddy", run_id="run-9",
                      issue_id="n9", role="developer", generation=1, native_tool="x",
                      request={"child_binding": {"worktree": ".worktrees/n9"}})
        self.assertEqual(store._dispatch_head("run-9", "n9", "developer", 1),
                         subprocess_out(fresh, "rev-parse", "HEAD"))

    def test_unbound_request_records_no_dispatch_head(self):
        store = _store(self._fresh_repo("fresh"))
        store.request(operation="wait", provider="workbuddy", run_id="run-9",
                      issue_id="n9", role="developer", generation=1, native_tool="x",
                      request={"threadId": "t"})
        self.assertIsNone(store._dispatch_head("run-9", "n9", "developer", 1))

    def test_dispatch_head_is_write_once_per_generation(self):
        # locate / resume of the same generation must not move the baseline.
        first = subprocess_out(self.root, "rev-parse", "HEAD")
        self._dispatch()
        (self.root / "other.py").write_text("x\n", encoding="utf-8")
        self.git("add", "other.py")
        self.git("commit", "-q", "-m", "later")
        self.store._record_dispatch_head("run-1", "n1", "developer", 1, {"worktree": ".worktrees/n1"})
        self.assertEqual(self.store._dispatch_head("run-1", "n1", "developer", 1), first)

    def test_identical_rereport_is_not_rechecked(self):
        self._dispatch()
        self._deliver()
        (self.root / "app.py").write_text("later main edit\n", encoding="utf-8")
        self.assertTrue(self._deliver()["duplicate"])

    def test_worker_protocol_documents_the_landing_check(self):
        text = (Path(__file__).resolve().parents[1] / "vibe_guide" / "protocols"
                / "visible-sdd-worker.md").read_text(encoding="utf-8")
        for token in ("按 git 实况核对落点", "uncommitted changes to this node's allowlisted files",
                      "reviewer（只读）", "合同 worktree 就是主项目根（`.`）", "已知盲区",
                      "cannot verify delivery landing", "派发时的主项目 HEAD",
                      "committed this node's allowlisted files after it was dispatched"):
            self.assertIn(token, text)

    # --- ISSUE-219: what a dispatch recorded about its project mount --------
    #
    # vibe cannot observe the sandbox roots the host granted the worker, so the
    # dispatch record stores the mount the create request *asked* for.  Only an
    # explicit "asked for none" warns; a record that predates the field, and a
    # generation with no record at all, are legacy and stay silent.  The note
    # is advisory: delivery still lands.

    def _record(self, generation=1):
        path = self.store._dispatch_head_path("run-1", "n1", "developer", generation)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_project_mount_from_the_create_request_is_recorded(self):
        self._dispatch(project_id="project-7")
        self.assertEqual(self._record()["project_mount"], "project:project-7")

    def test_create_that_asked_for_no_mount_records_an_explicit_none(self):
        # Not the same as an absent key: absent means the record predates the
        # field, and the two must never be confused (review P2).
        self._dispatch(project_id="   ")
        self.assertEqual(self._record()["project_mount"], "none")

    def test_recorded_mount_produces_no_warning(self):
        self._dispatch(project_id="project-7")
        self.assertNotIn("warnings", self._deliver())

    def test_missing_mount_warns_without_blocking_delivery(self):
        self._dispatch(project_id=None)
        outcome = self._deliver()
        self.assertTrue(outcome["recorded"])
        self.assertEqual(len(self._archived()), 1)
        self.assertIn("asked the host for no project mount", outcome["warnings"][0])

    def test_dispatch_without_any_record_is_not_warned(self):
        # ponytail ceiling: dispatches predating dispatch-heads.  Warning there
        # would fire on every run that already worked.
        self._dispatch()
        self.store._dispatch_head_path("run-1", "n1", "developer", 1).unlink()
        self.assertNotIn("warnings", self._deliver())

    def test_record_predating_the_field_is_not_warned(self):
        # Review P2: the first cut warned on any record without a mount, which
        # is every record written before this change -- a false alarm on runs
        # that already worked.  The modelling matters: the file exists, it just
        # has no mount field.
        self._dispatch()
        path = self.store._dispatch_head_path("run-1", "n1", "developer", 1)
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("project_mount", None)
        path.write_text(json.dumps(record), encoding="utf-8")
        self.assertNotIn("project_mount", self._record())
        self.assertNotIn("warnings", self._deliver())

    def test_reviewer_delivery_also_carries_the_warning(self):
        # A reviewer reads the worktree too; the sandbox note is not
        # developer-only even though the landing *check* is.
        self._dispatch(role="reviewer", project_id=None)
        outcome = self._deliver(role="reviewer")
        self.assertTrue(outcome["recorded"])
        self.assertIn("asked the host for no project mount", outcome["warnings"][0])

    def test_duplicate_report_keeps_the_warning(self):
        self._dispatch(project_id=None)
        self.assertTrue(self._deliver()["warnings"])
        again = self._deliver()
        self.assertTrue(again["duplicate"])
        self.assertTrue(again["warnings"])

    def test_session_root_inside_the_project_is_not_warned(self):
        # The one ground truth: what the worker session reports about itself.
        self._dispatch()
        payload = {
            "delivery_evidence": dict(GOOD["delivery_evidence"],
                                      session_cwd=str(self.root)),
        }
        self.assertNotIn("warnings", self.store.record_worker_delivery(
            "run-1", "n1", "developer", payload, 1))

    def test_session_root_outside_the_project_warns(self):
        # Review P2: a valid projectId in the request proves nothing -- the
        # 2026-10-16 run's create named one and the session was still opened
        # projectless, starting under Documents/Codex.  Only the worker's own
        # reported session root can see that.
        self._dispatch()
        stray = self.root.parent / "Codex" / "2026-10-15" / "m-en-00"
        stray.mkdir(parents=True)
        payload = {
            "delivery_evidence": dict(GOOD["delivery_evidence"],
                                      session_cwd=str(stray)),
        }
        outcome = self.store.record_worker_delivery(
            "run-1", "n1", "developer", payload, 1)
        self.assertTrue(outcome["recorded"])
        self.assertIn("very likely projectless", outcome["warnings"][0])

    def test_session_root_inside_the_node_worktree_is_not_warned(self):
        # A worktree environment may place the session in the node worktree,
        # which is a legitimate mount even though it is a separate tree.
        self._dispatch()
        payload = {
            "delivery_evidence": dict(GOOD["delivery_evidence"],
                                      session_cwd=str(self.root / ".worktrees" / "n1")),
        }
        self.assertNotIn("warnings", self.store.record_worker_delivery(
            "run-1", "n1", "developer", payload, 1))

    def test_absent_session_root_is_not_evidence(self):
        # Prompts older than this check do not carry the field; a missing value
        # is not a bad session and must never turn into a warning.
        self._dispatch()
        self.assertNotIn("warnings", self._deliver())

    def test_non_string_session_root_is_refused(self):
        self._dispatch()
        payload = {
            "delivery_evidence": dict(GOOD["delivery_evidence"], session_cwd=7),
        }
        with self.assertRaises(ValueError):
            self.store.record_worker_delivery("run-1", "n1", "developer", payload, 1)

    def test_worker_protocol_documents_the_project_mount_requirement(self):
        text = (Path(__file__).resolve().parents[1] / "vibe_guide" / "protocols"
                / "visible-sdd-worker.md").read_text(encoding="utf-8")
        for token in ("必须挂载 project", "禁止静默落 projectless",
                      "派发负载不能携带权限", "停下告诉监工",
                      "delivery_evidence.session_cwd", "不能改变挂载"):
            self.assertIn(token, text)


class ProjectMountRecordTests(unittest.TestCase):
    """ISSUE-219 review P1: a rework generation must inherit its create's mount.

    These drive ``ProviderActionStore.request`` -- the wiring the monitor
    actually uses -- instead of hand-writing the dispatch record, so the
    inheritance a rework depends on is exercised end to end.  A resume cannot
    change a session's mount, so a rework that recorded "no mount asked for"
    would fire the delivery warning on every rework round.
    """

    def setUp(self):
        import subprocess
        from unittest.mock import patch

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()

        def git(*args):
            subprocess.run(["git", "-C", str(self.root)] + list(args), check=True,
                           capture_output=True)

        git("init", "-q", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (self.root / "app.py").write_text("v1\n", encoding="utf-8")
        git("add", "app.py")
        git("commit", "-q", "-m", "init")
        git("worktree", "add", "-q", "-b", "node/n1", ".worktrees/n1")
        (self.root / ".vibe").mkdir()
        (self.root / ".vibe" / "state.json").write_text("{}", encoding="utf-8")
        gate = patch("vibe_guide.adapters.task_provider.require_entry")
        gate.start()
        self.addCleanup(gate.stop)
        self.store = _store(self.root)

    _BINDING = {
        "worktree": ".worktrees/n1", "branch": "node/n1", "allowlist": ["app.py"],
    }

    def _create(self, project_id="project-7", generation=1):
        return self.store.request(
            operation="create", provider="codex", run_id="run-1", issue_id="n1",
            role="developer", generation=generation,
            native_tool="codex_app__create_thread",
            request={
                "prompt": "work",
                "child_binding": dict(self._BINDING),
                "target": {"type": "project", "projectId": project_id},
            },
        )

    def _resume(self, generation):
        return self.store.request(
            operation="resume", provider="codex", run_id="run-1", issue_id="n1",
            role="developer", generation=generation,
            native_tool="codex_app__send_message_to_thread",
            request={
                "threadId": "t-1", "hostId": "h-1", "prompt": "rework",
                "child_binding": dict(self._BINDING),
            },
        )

    def _record(self, generation):
        return self.store._dispatch_record("run-1", "n1", "developer", generation)

    def test_create_records_the_mount_it_asked_for(self):
        self._create(project_id="project-7")
        self.assertEqual(self._record(1)["project_mount"], "project:project-7")

    def test_rework_generation_inherits_the_create_mount(self):
        self._create(project_id="project-7")
        self._resume(2)
        self.assertEqual(self._record(2)["project_mount"], "project:project-7")

    def test_rework_generation_is_not_warned(self):
        self._create(project_id="project-7")
        self._resume(2)
        outcome = self.store.record_worker_delivery(
            "run-1", "n1", "developer", dict(GOOD), 2)
        self.assertTrue(outcome["recorded"])
        self.assertNotIn("warnings", outcome)

    def test_rework_generation_of_a_mountless_create_is_still_warned(self):
        # Inheritance must not launder a genuinely mountless lineage.
        self._create(project_id="   ")
        self._resume(2)
        self.assertEqual(self._record(2)["project_mount"], "none")
        outcome = self.store.record_worker_delivery(
            "run-1", "n1", "developer", dict(GOOD), 2)
        self.assertIn("asked the host for no project mount", outcome["warnings"][0])

    def test_unwritable_record_leaves_the_mount_absent_not_none(self):
        # ponytail: an unwritable dispatch record is treated as absent.  Absent
        # is legacy, never "asked for none", so it must not warn.
        from unittest.mock import patch

        with patch("vibe_guide.adapters.task_provider.os.write",
                   side_effect=OSError("disk full")):
            self._create(project_id="project-7")
        self.assertFalse(
            self.store._dispatch_head_path("run-1", "n1", "developer", 1).exists()
        )
        outcome = self.store.record_worker_delivery(
            "run-1", "n1", "developer", dict(GOOD), 1)
        self.assertTrue(outcome["recorded"])
        self.assertNotIn("warnings", outcome)


class DispatchPromptTests(unittest.TestCase):
    def test_reviewer_create_includes_delivery_and_supervisor_consumer_boundary(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        with tempfile.TemporaryDirectory() as d:
            runner = ProviderActionRunner(ProjectPaths(Path(d)), 'codex', 'codex-app-visible')
            seen = []
            def native_result(contract, run_id, operation, request):
                seen.append((operation, request))
                return {'create': {'binding': {'task_id': 'reviewer', 'host': 'mac'}},
                        'locate': {'located': True},
                        'visibility': {'visible': True, 'direct_enter': True}}[operation]
            runner._require_result = native_result
            runner.task_binding({'node_id': 'n1', 'role': 'reviewer', 'generation': 4,
                'project_id': 'p', 'topology': 'dual-visible', 'worktree': 'w',
                'branch': 'b', 'files': []}, Path(d), 'run-1', 'review')
            prompt = dict(seen)['create']['prompt']
            for required in ('worker-deliver', '--role reviewer', '--generation 4',
                             'supervisor-address --run-id run-1 --json', '已授权通道',
                             '登记主监工', '不得调用 core resume',
                             # ISSUE-219: the session root is the only vantage
                             # that sees the sandbox the host actually granted,
                             # so every dispatch prompt has to ask for it.
                             'delivery_evidence.session_cwd'):
                self.assertIn(required, prompt)

    def test_both_continuations_keep_workers_out_of_run_consumption(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        for role in ('developer', 'reviewer'):
            with self.subTest(role=role):
                prompt = ProviderActionRunner._continuation_prompt({
                    'node_id': 'n1', 'role': role, 'generation': 4, 'run_id': 'run-1'})
                for required in ('worker-deliver', '--run-id run-1', '--role ' + role, '--generation 4',
                                 'supervisor-address --run-id run-1 --json',
                                 '登记主监工', '不得调用 core resume',
                                 'delivery_evidence.session_cwd'):
                    self.assertIn(required, prompt)

    def test_create_prompt_carries_completion_steps(self):
        with tempfile.TemporaryDirectory() as d:
            from vibe_guide.runners.provider_action import ProviderActionRunner

            runner = ProviderActionRunner(
                ProjectPaths(Path(d)), "codex", "codex-app-visible"
            )
            seen = []

            def fake(contract, run_id, operation, request):
                seen.append((operation, request))
                return {
                    "create": {"binding": {"task_id": "t", "host": "mac"}},
                    "locate": {"located": True},
                    "visibility": {"visible": True, "direct_enter": True},
                }[operation]

            runner._require_result = fake
            contract = {
                "node_id": "n1", "role": "developer", "generation": 1,
                "project_id": "p", "topology": "dual-visible",
                "worktree": "w", "branch": "b", "files": [],
            }
            runner.task_binding(contract, Path(d), "run-1", "running")
            prompt = dict(seen)["create"]["prompt"]
            self.assertIn(
                "cd {} && vibe worker-deliver".format(ProjectPaths(Path(d)).root), prompt
            )
            self.assertIn("--generation 1", prompt)
            for token in (
                "worker-deliver",
                "supervisor-address",
                "唤醒信号",
                "心跳兜底",
            ):
                self.assertIn(token, prompt, token)

    def test_rework_resume_prompt_carries_the_new_generation(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from vibe_guide.runners import provider_action
        from tests.test_heartbeat_native_actions import _profile
        from vibe_guide.runners.provider_action import ProviderActionRunner

        with tempfile.TemporaryDirectory() as d:
            runner = ProviderActionRunner(
                ProjectPaths(Path(d)), "codex", "codex-app-visible"
            )
            seen = []

            def fake_action(contract, run_id, operation, request):
                seen.append((operation, request))
                return {"action_id": "action-r"}

            runner._action = fake_action
            runner.binding_gate = lambda contract, binding: SimpleNamespace(verified=True)
            binding = SimpleNamespace(
                task_id="t", host="mac", cursor=None, capability_contract_digest=None,
                model="default", reasoning="normal", route_digest=_profile().route_digest,
            )
            contract = {
                "run_id": "run-1", "node_id": "issue-1", "role": "developer",
                "generation": 3, "continuation": True,
                "goal": "修复错误分类", "worktree": ".worktrees/issue-148",
                "branch": "node/issue-148", "files": ["vibe_guide/monitor.py"],
                "status_file": "developer-status.json", "handoff_file": "developer-delivery.md",
                "worker_profile": _profile().to_dict(),
            }
            with patch.object(provider_action, "require_complex_monitor_dispatch"), \
                    patch.object(provider_action, "load_task_binding", return_value=binding), \
                    patch.object(provider_action, "binding_contract_enabled", return_value=False):
                runner.start(contract, Path(d))
            self.assertEqual(dict(seen)["resume"]["model"], "default")
            self.assertEqual(dict(seen)["resume"]["thinking"], "medium")
            prompt = dict(seen)["resume"]["prompt"]
            self.assertIn("--generation 3", prompt)
            for expected in ("修复错误分类", ".worktrees/issue-148", "node/issue-148", "vibe_guide/monitor.py", "developer-status.json", "developer-delivery.md", "执行请求"):
                self.assertIn(expected, prompt)

            tail = prompt.split("一致性纠偏证据必须原样绑定：", 1)[1]
            json.loads(tail)


if __name__ == "__main__":
    unittest.main()
