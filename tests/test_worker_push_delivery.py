"""ISSUE-91 worker-push-delivery: worker self-report CLI + dispatch steps.

Anchors: missing completion_marker -> nonzero exit, nothing on disk; a legal
report is consumed by the next resume via the wait result; identical re-
reports stay idempotent; dispatch prompt carries the fixed completion steps.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
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

    def _dispatch(self, worktree=".worktrees/n1", branch="node/n1", role="developer"):
        action_id = "action-create-run-1-n1-{}".format(role)
        (self.store._directory("requests") / (action_id + ".json")).write_text(json.dumps({
            "schema_version": 1, "action_id": action_id, "operation": "create",
            "provider": "workbuddy", "run_id": "run-1", "issue_id": "n1",
            "role": role, "generation": 1, "sequence": 0, "native_tool": "x",
            "request": {"child_binding": {
                "worktree": worktree, "branch": branch, "allowlist": ["app.py"],
            }},
            "request_digest": "0" * 64,
        }), encoding="utf-8")

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
        with self.assertRaisesRegex(ValueError, "main project: app.py"):
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

    def test_worker_protocol_documents_the_landing_check(self):
        text = (Path(__file__).resolve().parents[1] / "vibe_guide" / "protocols"
                / "visible-sdd-worker.md").read_text(encoding="utf-8")
        for token in ("按 git 实况核对落点", "allowlisted files are modified in the main project",
                      "reviewer 只读，不做此核对"):
            self.assertIn(token, text)


class DispatchPromptTests(unittest.TestCase):
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
            )
            contract = {
                "run_id": "run-1", "node_id": "n1", "role": "developer",
                "generation": 3, "continuation": True,
            }
            with patch.object(provider_action, "require_complex_monitor_dispatch"), \
                    patch.object(provider_action, "load_task_binding", return_value=binding), \
                    patch.object(provider_action, "binding_contract_enabled", return_value=False):
                runner.start(contract, Path(d))
            prompt = dict(seen)["resume"]["prompt"]
            self.assertIn("--generation 3", prompt)
            tail = prompt.split("一致性纠偏证据必须原样绑定：", 1)[1]
            json.loads(tail)


if __name__ == "__main__":
    unittest.main()
