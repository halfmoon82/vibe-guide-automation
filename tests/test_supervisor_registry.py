"""ISSUE-91: supervisor preflight, address registry, rotation rules.

Acceptance anchors: 70k tokens -> rotate; 30k + no pending + active worker
-> idle; pending create -> work; missing record -> unknown; register A then
B -> query returns B and history keeps A.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from unittest.mock import patch
from vibe_guide.supervisor import (
    current_supervisor_address,
    register_supervisor_address,
    supervisor_preflight,
)


def _record(directory, tokens):
    path = Path(directory) / "session.json"
    path.write_text(json.dumps({"token_count": tokens}), encoding="utf-8")
    return str(path)


class _FakeSnapshot:
    def __init__(self, statuses):
        self.nodes = {
            node: {"status": status} for node, status in statuses.items()
        }


def _patch_snapshot(statuses):
    return patch(
        "vibe_guide.supervisor.load_snapshot",
        return_value=_FakeSnapshot(statuses),
    )


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)

    def test_over_threshold_rotates(self):
        rec = _record(self.tmp.name, 70000)
        result = supervisor_preflight(self.paths, "run-1", rec)
        self.assertEqual(result["state"], "rotate")

    def test_threshold_is_configurable(self):
        rec = _record(self.tmp.name, 70000)
        result = supervisor_preflight(
            self.paths, "run-1", rec, token_threshold=100000
        )
        self.assertNotEqual(result["state"], "rotate")

    def test_under_threshold_no_pending_active_worker_is_idle(self):
        rec = _record(self.tmp.name, 30000)
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(self.paths, "run-1", rec)
        self.assertEqual(result["state"], "idle")

    def test_pending_request_is_work(self):
        rec = _record(self.tmp.name, 30000)
        store_dir = self.paths.vibe / "provider-actions" / "requests"
        store_dir.mkdir(parents=True)
        (store_dir / "action-x.json").write_text(json.dumps({
            "schema_version": 1, "action_id": "action-x",
            "operation": "create", "provider": "codex",
            "run_id": "run-1", "issue_id": "n1", "role": "developer",
            "generation": 1, "sequence": 0,
            "native_tool": "codex_app__create_thread",
            "request": {"prompt": "x"},
            "request_digest": "0" * 64,
        }), encoding="utf-8")
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(self.paths, "run-1", rec)
        self.assertEqual(result["state"], "work")

    def test_delivered_worker_is_work(self):
        rec = _record(self.tmp.name, 30000)
        with _patch_snapshot({"n1": "delivered"}):
            result = supervisor_preflight(self.paths, "run-1", rec)
        self.assertEqual(result["state"], "work")

    def test_nodes_needing_service_are_work_not_idle(self):
        rec = _record(self.tmp.name, 30000)
        for status in ("retry_pending", "blocked_unknown", "start_pending",
                       "delivered", "brief_pending", "something_new"):
            with self.subTest(status=status):
                with _patch_snapshot({"n1": "running", "n2": status}):
                    result = supervisor_preflight(self.paths, "run-1", rec)
                self.assertEqual(result["state"], "work")

    def test_active_and_settled_nodes_stay_idle(self):
        rec = _record(self.tmp.name, 30000)
        for status in ("review", "rework", "accepted", "failed", "stopped",
                       "skipped_by_user", "blocked_design",
                       "blocked_by_required_node"):
            with self.subTest(status=status):
                with _patch_snapshot({"n1": "running", "n2": status}):
                    result = supervisor_preflight(self.paths, "run-1", rec)
                self.assertEqual(result["state"], "idle")

    def test_unconsumed_self_report_is_work(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        ProviderActionStore(self.paths).record_worker_delivery(
            "run-1", "n1", "developer",
            {"delivery_evidence": {
                "completion_marker": "M", "delivery_path": "x",
                "thread_status": "completed",
            }},
            1,
        )
        rec = _record(self.tmp.name, 30000)
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(self.paths, "run-1", rec)
        self.assertEqual(result["state"], "work")

    def test_other_runs_pending_request_does_not_wake_this_run(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore

        ProviderActionStore(self.paths).request(
            operation="wait", provider="codex", run_id="run-other",
            issue_id="n1", role="developer", generation=1,
            native_tool="codex_app__wait_threads", request={"t": 1},
        )
        rec = _record(self.tmp.name, 30000)
        with _patch_snapshot({"n1": "running"}):
            result = supervisor_preflight(self.paths, "run-1", rec)
        self.assertEqual(result["state"], "idle")

    def test_claude_code_jsonl_record_reads_latest_usage(self):
        path = Path(self.tmp.name) / "session.jsonl"
        lines = [
            {"type": "user", "message": {"content": "hi"}},
            {"type": "assistant", "message": {"usage": {
                "input_tokens": 10, "cache_read_input_tokens": 20000,
                "cache_creation_input_tokens": 500}}},
            {"type": "assistant", "message": {"usage": {
                "input_tokens": 5, "cache_read_input_tokens": 70000,
                "cache_creation_input_tokens": 100}}},
        ]
        path.write_text(
            "\n".join(json.dumps(line) for line in lines) + '\n{"partial',
            encoding="utf-8",
        )
        result = supervisor_preflight(self.paths, "run-1", str(path))
        self.assertEqual(result["state"], "rotate")
        self.assertEqual(result["tokens"], 70105)

    def test_missing_record_is_unknown_not_idle(self):
        result = supervisor_preflight(self.paths, "run-1", "/nonexistent.json")
        self.assertEqual(result["state"], "unknown")

    def test_unparseable_record_is_unknown(self):
        bad = Path(self.tmp.name) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        result = supervisor_preflight(self.paths, "run-1", str(bad))
        self.assertEqual(result["state"], "unknown")


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()
        self.paths = ProjectPaths(self.root)

    def _entry(self, session):
        return {"provider": "codex", "session_id": session, "host": "mac"}

    def test_register_then_rotate_keeps_history(self):
        register_supervisor_address(self.paths, "run-1", self._entry("A"))
        register_supervisor_address(self.paths, "run-1", self._entry("B"))
        result = current_supervisor_address(self.paths, "run-1")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["current"]["session_id"], "B")
        self.assertEqual(
            [item["session_id"] for item in result["history"]], ["A"]
        )

    def test_query_without_registration_is_unknown(self):
        result = current_supervisor_address(self.paths, "run-1")
        self.assertEqual(result["status"], "unknown")

    def test_credentials_are_rejected(self):
        with self.assertRaises(ValueError):
            register_supervisor_address(
                self.paths, "run-1",
                {**self._entry("A"), "auth_token": "secret"},
            )


class SupervisorCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        self.root.mkdir()

    def cli(self, argv):
        return run_cli(argv + ["--json"], self.root)

    def test_register_and_query_roundtrip(self):
        created = self.cli([
            "supervisor-register", "--run-id", "run-1",
            "--provider", "codex", "--session-id", "A", "--host", "mac",
        ])
        self.assertEqual(created.exit_code, 0, created.payload)
        self.cli([
            "supervisor-register", "--run-id", "run-1",
            "--provider", "codex", "--session-id", "B", "--host", "mac",
        ])
        result = self.cli(["supervisor-address", "--run-id", "run-1"])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["current"]["session_id"], "B")
        self.assertEqual(
            [item["session_id"] for item in result.payload["history"]], ["A"]
        )

    def test_unregistered_query_is_unknown(self):
        result = self.cli(["supervisor-address", "--run-id", "run-1"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "unknown")

    def test_address_query_with_escaping_run_id_is_blocked(self):
        result = self.cli(["supervisor-address", "--run-id", "../x"])
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.payload["status"], "blocked_invalid")

    def test_preflight_cli_rotate(self):
        rec = Path(self.tmp.name) / "s.json"
        rec.write_text(json.dumps({"token_count": 70000}), encoding="utf-8")
        result = self.cli([
            "supervisor-preflight", "--run-id", "run-1",
            "--session-record", str(rec),
        ])
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.payload["state"], "rotate")


if __name__ == "__main__":
    unittest.main()
