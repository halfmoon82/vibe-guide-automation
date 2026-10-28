"""CLI: mailbox write-back, blocked_unknown disposition, publish defaults (5.2.0)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_monitor as tm
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.authorization import (
    DEFAULT_HEARTBEAT_INTERVAL_MINUTES,
    HEARTBEAT_INTERVAL_CHOICES,
    HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES,
)
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.publish import _default_heartbeat_minutes, _dispatch_rulings
from vibe_guide.runners.provider_action import validate_result_shape
from vibe_guide.state import load_snapshot

RUN = "run-1"
UNJUDGEABLE = "无法确认 2.3.7 的 props"


class MailboxCommandTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".vibe").mkdir(parents=True)
        (self.root / ".vibe" / "state.json").write_text("{}", encoding="utf-8")
        self.paths = ProjectPaths(self.root)
        gate = patch("vibe_guide.adapters.task_provider.require_entry")
        gate.start()
        self.addCleanup(gate.stop)

    def request(self, operation, role="developer"):
        return ProviderActionStore(self.paths).request(
            operation=operation, provider="claude-code", run_id=RUN, issue_id="node-a",
            role=role, generation=1, sequence=1,
            native_tool="ccd_session_mgmt__send_message", request={"threadId": "t1"})

    def cli(self, *args):
        return run_cli(["supervisor-mailbox", "--run-id", RUN, "--json", *args], self.root)

    def result_file(self, payload):
        path = Path(self.tmp.name) / "result.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return str(path)

    def test_template_then_complete_then_complete_again(self):
        action_id = self.request("wait")["action_id"]
        template = self.cli("--template", action_id)
        self.assertEqual(template.exit_code, 0, template.payload)
        example = template.payload["template"]
        validate_result_shape("wait", example, role="developer")

        done = self.cli("--complete", action_id, "--result", self.result_file(example))
        self.assertEqual((done.exit_code, done.payload["status"]), (0, "completed"), done.payload)
        self.assertEqual(ProviderActionStore(self.paths).result(action_id), example)
        self.assertNotIn(action_id, [a["action_id"] for a in ProviderActionStore(self.paths).pending(RUN)])

        again = self.cli("--complete", action_id, "--result", self.result_file(example))
        self.assertEqual((again.exit_code, again.payload["status"]), (0, "already_completed"))

    def test_a_second_different_answer_is_reported_and_writes_nothing(self):
        action_id = self.request("resume")["action_id"]
        self.assertEqual(self.cli("--complete", action_id, "--result", self.result_file({"resumed": True})).exit_code, 0)
        observations = self.root / ".vibe" / "provider-actions" / "observations"
        before = sorted(p.name for p in observations.glob("*")) if observations.exists() else []
        other = self.cli("--complete", action_id, "--result", self.result_file({"resumed": True, "status": "ok"}))
        self.assertEqual(other.payload["status"], "already_completed")
        after = sorted(p.name for p in observations.glob("*")) if observations.exists() else []
        self.assertEqual(before, after)

    def test_every_operation_template_passes_the_shape_check(self):
        for index, (operation, role) in enumerate((("wait", "developer"), ("wait", "reviewer"),
                                                   ("create", "developer"), ("resume", "developer"),
                                                   ("locate", "developer"))):
            store = ProviderActionStore(self.paths)
            action = store.request(
                operation=operation, provider="claude-code", run_id=RUN, issue_id="node-" + str(index),
                role=role, generation=1, sequence=1,
                native_tool="ccd_session_mgmt__send_message", request={"threadId": "t1"})
            example = self.cli("--template", action["action_id"]).payload["template"]
            validate_result_shape(operation, example, role=role)

    def test_invalid_result_is_refused_and_leaves_the_request_pending(self):
        action_id = self.request("wait")["action_id"]
        bad = self.cli("--complete", action_id, "--result",
                       self.result_file({"status": "complete", "event": "delivered"}))
        self.assertEqual(bad.exit_code, 3)
        self.assertIn("delivery_evidence", bad.payload["reason"])
        store = ProviderActionStore(self.paths)
        self.assertIsNone(store.result(action_id))
        self.assertIn(action_id, [a["action_id"] for a in store.pending(RUN)])

    def test_missing_result_unknown_action_and_bad_json_are_readable_errors(self):
        action_id = self.request("wait")["action_id"]
        missing = self.cli("--complete", action_id)
        self.assertEqual(missing.exit_code, 3)
        self.assertIn("--result", missing.payload["reason"])
        unknown = self.cli("--template", "action-" + "0" * 32)
        self.assertEqual(unknown.exit_code, 3)
        path = Path(self.tmp.name) / "broken.json"
        path.write_text("{", encoding="utf-8")
        broken = self.cli("--complete", action_id, "--result", str(path))
        self.assertEqual(broken.exit_code, 3)
        self.assertIn("JSON", broken.payload["reason"])
        self.assertIsNone(ProviderActionStore(self.paths).result(action_id))

    def test_result_can_come_from_stdin(self):
        action_id = self.request("resume")["action_id"]
        with patch("sys.stdin") as stdin:
            stdin.read.return_value = json.dumps({"resumed": True})
            done = self.cli("--complete", action_id, "--result", "-")
        self.assertEqual(done.payload["status"], "completed")


class BlockedUnknownCommandTests(unittest.TestCase):
    setUp = tm.VisibleDispatchTests.setUp
    tearDown = tm.VisibleDispatchTests.tearDown
    authorized_monitor = tm.VisibleDispatchTests.authorized_monitor

    def paused_run(self):
        payload, nodes = tm.load_v46_dispatch_fixture()
        monitor, record = self.authorized_monitor(nodes, topology_rulings=payload["topology_rulings"])
        events = [("complete", {
            "evidence": "delivery",
            "in_session_review": {
                "protocol": tm.VISIBLE_SDD_PROTOCOL_REF,
                "evidence_ref": "session-delivery#review-round-1",
                "clearance": {"p0": 0, "p1": 0, "p2": 0},
                "coverage": {"mode": "ocr", "listed": 3, "reviewed": 3, "skipped": []},
                "environment_facts_ref": "none",
                "blocked_unknowns": [UNJUDGEABLE],
            },
        })]
        runner = tm.VisibleSddRunner(events={("sdd-a", "developer"): events})
        snapshot = monitor.start(record, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes["sdd-a"]["status"], "blocked_unknown")
        return monitor, snapshot

    def cli(self, monitor, *args):
        with patch("vibe_guide.cli.Monitor", return_value=monitor), \
                patch("vibe_guide.cli._load_plan", return_value=(None, monitor.plan, monitor.nodes, None)), \
                patch("vibe_guide.cli._observed_topology_rulings", return_value={}):
            return run_cli(["blocked-unknown", "--json", *args], self.paths.root)

    def test_disposition_is_recorded_and_visible_in_the_snapshot(self):
        monitor, snapshot = self.paused_run()
        result = self.cli(monitor, "--run-id", snapshot.run_id, "--node", "sdd-a",
                          "--disposition", "confirmed_benign", "--reason", "已复核")
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(result.payload["items"], [UNJUDGEABLE])
        current = load_snapshot(self.paths, snapshot.run_id).nodes["sdd-a"]
        self.assertEqual(current["blocked_unknowns_pending"], [])
        self.assertEqual(
            current["blocked_unknown_dispositions"],
            [{"item": UNJUDGEABLE, "disposition": "confirmed_benign", "note": "已复核"}],
        )

    def test_missing_arguments_and_bad_values_change_nothing(self):
        monitor, snapshot = self.paused_run()
        before = load_snapshot(self.paths, snapshot.run_id).nodes["sdd-a"]
        for args, word in (
            (("--run-id", snapshot.run_id, "--node", "sdd-a", "--disposition", "confirmed_benign"), "--reason"),
            (("--run-id", snapshot.run_id, "--node", "sdd-a", "--disposition", "nope", "--reason", "x"), "--disposition"),
            (("--run-id", snapshot.run_id, "--node", "ghost", "--disposition", "confirmed_benign", "--reason", "x"), "ghost"),
            (("--run-id", snapshot.run_id, "--node", "sdd-a", "--disposition", "confirmed_benign",
              "--reason", "x", "--item", "not pending"), "待处置"),
        ):
            result = self.cli(monitor, *args)
            self.assertEqual(result.exit_code, 3, args)
            self.assertIn(word, result.payload["reason"])
        self.assertEqual(load_snapshot(self.paths, snapshot.run_id).nodes["sdd-a"], before)


class PublishDefaultsTests(unittest.TestCase):
    def test_choices_are_defined_once_and_the_fallback_is_60(self):
        self.assertEqual(HEARTBEAT_INTERVAL_CHOICES, (DEFAULT_HEARTBEAT_INTERVAL_MINUTES,
                                                      HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES))
        self.assertEqual(HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES, 60)
        from vibe_guide import supervisor
        self.assertEqual(supervisor.HEARTBEAT_INTERVAL_PUSH_VERIFIED_MINUTES, 60)
        self.assertEqual(supervisor.HEARTBEAT_INTERVAL_DEFAULT_MINUTES, 10)

    def test_default_heartbeat_follows_push_verification(self):
        verified = [{"minutes": 10, "available": True}, {"minutes": 60, "available": True}]
        forced = [{"minutes": 10, "available": True},
                  {"minutes": 60, "available": True, "forced": True, "cost": "x"}]
        unknown = [{"minutes": 10, "available": True}, {"minutes": 60, "available": False}]
        self.assertEqual(_default_heartbeat_minutes(verified), 60)
        self.assertEqual(_default_heartbeat_minutes(forced), 10)
        self.assertEqual(_default_heartbeat_minutes(unknown), 10)

    def test_dispatch_mode_rules_claude_code_background_unless_visible(self):
        self.assertEqual(_dispatch_rulings({}, "claude-code", None), {"claude-code": "background"})
        self.assertEqual(_dispatch_rulings({"claude-code": "background"}, "claude-code", "visible"), {})
        self.assertEqual(_dispatch_rulings({}, "codex-app", None), {})
        with self.assertRaises(ValueError):
            _dispatch_rulings({}, "claude-code", "sideways")

    def test_help_has_no_authorization_token_and_lists_the_new_commands(self):
        import subprocess
        import sys
        help_text = subprocess.run([sys.executable, "-m", "vibe_guide", "--help"], text=True,
                                   capture_output=True, cwd=str(Path(__file__).resolve().parents[1])).stdout
        self.assertNotIn("--authorization-token", help_text)
        self.assertIn("blocked-unknown", help_text)
        self.assertIn("--complete", help_text)

    def test_legacy_executable_digest_acceptance_is_gone(self):
        from vibe_guide import authorization
        source = Path(authorization.__file__).read_text(encoding="utf-8")
        self.assertNotIn("legacy_executable_contract_digest(nodes),", source)


if __name__ == "__main__":
    unittest.main()
