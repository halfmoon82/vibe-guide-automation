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
                )
            self.assertFalse((Path(d) / "deliveries").exists())

    def test_legal_report_completes_pending_wait(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            action = _wait_request(store)
            outcome = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            )
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
            )
            second = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            )
            self.assertFalse(first["duplicate"])
            self.assertTrue(second["duplicate"])
            results = list(
                (Path(d) / ".vibe" / "provider-actions" / "results").glob("action-*.json")
            )
            self.assertEqual(len(results), 1, "no second event/result")

    def test_report_without_pending_wait_still_lands_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            outcome = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            )
            self.assertFalse(outcome["consumed"])
            records = list((Path(d) / ".vibe" / "provider-actions" / "deliveries").glob("*.json"))
            self.assertEqual(len(records), 1)


    def test_flattened_evidence_is_rejected_without_writing(self):
        with tempfile.TemporaryDirectory() as d:
            store = _store(d)
            with self.assertRaises(ValueError):
                store.record_worker_delivery(
                    "run-1", "n1", "developer", dict(GOOD["delivery_evidence"])
                )
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
                        store.record_worker_delivery(run_id, node, role, dict(GOOD))
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
            )
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
            early = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            )
            self.assertFalse(early["consumed"])
            self.assertEqual(store.unconsumed_deliveries("run-1"), ["n1"])
            action = _wait_request(store)
            result = store.result(action["action_id"])
            self.assertIsNotNone(result, "archived delivery must fill the new wait")
            self.assertEqual(result["event"], "delivered")
            self.assertEqual(store.unconsumed_deliveries("run-1"), [])
            again = store.record_worker_delivery(
                "run-1", "n1", "developer", dict(GOOD)
            )
            self.assertTrue(again["duplicate"])
            self.assertFalse(again["consumed"])


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
            "worker-deliver", "--run-id", "run-1", "--node", "n1",
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
            "worker-deliver", "--run-id", "run-1", "--node", "n1",
            "--payload", json.dumps(GOOD),
        ])
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertTrue(result.payload["consumed"])


    def test_escaping_run_id_is_blocked(self):
        result = self.cli([
            "worker-deliver", "--run-id", "../../escaped", "--node", "n1",
            "--payload", json.dumps(GOOD),
        ])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "blocked_invalid")
        self.assertFalse((self.root.parent / "escaped-n1-developer.json").exists())


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
            for token in (
                "worker-deliver",
                "supervisor-address",
                "唤醒信号",
                "心跳兜底",
            ):
                self.assertIn(token, prompt, token)


if __name__ == "__main__":
    unittest.main()
