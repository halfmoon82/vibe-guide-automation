"""Self-heal wiring: stall detection and the heal pack reach the heartbeat,
the status label, the resume text and the ``vibe heal-verify`` command."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.test_stuck_recovery_278 import _node
from vibe_guide import stall, supervisor
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.cli import _snapshot_result, run_cli
from vibe_guide.models import AgentCapabilities, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    _EVENT_DATA_KEYS,
    load_events,
    load_snapshot,
    map_user_status,
    save_snapshot,
)

NODE = "n1"
INVESTIGATE_LABEL = "需要监工排查"
ESCALATE_LABEL = "需要人处理"
RESUMING_TEXT = "监工已启动并自动推进中"


class IncidentFixture(unittest.TestCase):
    """A run whose only node is retry_pending behind a resume receipt the reader refuses."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.paths = ProjectPaths(self.root)
        (self.paths.vibe).mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(self.paths, build_contract(self.root, provider="fake", host_id="local"))
        gate = patch("vibe_guide.adapters.task_provider.require_entry")
        gate.start()
        self.addCleanup(gate.stop)
        nodes = [_node(NODE)]
        plan = Plan("plan-1", 1, "docs/prd.md", [item.id for item in nodes], "draft")
        capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
        card = build_authorization_card(plan, nodes, capabilities)
        monitor = Monitor(self.paths, plan, nodes)
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), FakeRunner())
        self.run_id = snapshot.run_id
        node = snapshot.nodes[NODE]
        node["status"] = "retry_pending"
        node["active_task"] = None
        node["retryable_action"] = {"action": "resume", "attempt": 1}
        snapshot.handles.pop(NODE, None)
        node["user_status"] = map_user_status(node)
        save_snapshot(self.paths, snapshot)
        self.store = ProviderActionStore(self.paths)
        self.action = self.store.request(
            operation="resume", provider="claude-code", run_id=self.run_id,
            issue_id=NODE, role="developer", generation=1, sequence=1,
            native_tool="ccd_session_mgmt__send_message", request={"threadId": "t1"},
        )
        self.store.complete(self.action["action_id"], {"threadId": "t1"}, native=False)
        self.record = self.root / "rollout.jsonl"
        self.record.write_text(json.dumps({"type": "event_msg", "payload": {
            "type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000}}}}) + "\n")

    def beat(self):
        return run_cli(["supervisor-beat", "--run-id", self.run_id,
                        "--session-record", str(self.record)], self.root)

    def verify(self):
        return run_cli(["heal-verify", "--run-id", self.run_id, "--node", NODE, "--json"],
                       self.root)

    def fix_receipt(self):
        path = self.root / ".vibe" / "provider-actions" / "results" / (
            self.action["action_id"] + ".json")
        record = json.loads(path.read_text(encoding="utf-8"))
        record["payload"] = {"resumed": True}
        path.write_text(json.dumps(record), encoding="utf-8")

    def attempts(self):
        return [e for e in load_events(self.paths, self.run_id) if e["event"] == "self_heal_attempt"]

    def progress_entry(self):
        return stall.load_progress(self.paths, self.run_id)["nodes"][NODE]


class StateLabelTests(unittest.TestCase):
    retrying = {"status": "retry_pending", "retryable_action": {"action": "resume"}}

    def test_stalled_investigate_replaces_the_auto_repair_label(self):
        self.assertEqual(map_user_status(dict(self.retrying)), "自动修复中")
        marked = dict(self.retrying, stall_tier="supervisor_investigate")
        self.assertEqual(map_user_status(marked), INVESTIGATE_LABEL)

    def test_escalated_node_reads_the_need_human_label_not_the_decision_label(self):
        marked = dict(self.retrying, stall_tier="escalate_to_pm")
        self.assertEqual(map_user_status(marked), ESCALATE_LABEL)
        self.assertNotEqual(map_user_status(marked), "需要你决定")

    def test_auto_retry_tier_keeps_the_auto_repair_label(self):
        self.assertEqual(map_user_status(dict(self.retrying, stall_tier="auto_retry")), "自动修复中")

    def test_marker_only_relabels_nodes_that_would_read_auto_repair(self):
        for status, expected in (("planned", "准备中"), ("running", "已启动"),
                                 ("blocked_design", "需要你决定")):
            with self.subTest(status=status):
                marked = {"status": status, "stall_tier": "supervisor_investigate"}
                self.assertEqual(map_user_status(marked), expected)

    def test_new_label_is_in_the_public_vocabulary(self):
        from vibe_guide.state import USER_VISIBLE_STATES
        self.assertIn(INVESTIGATE_LABEL, USER_VISIBLE_STATES)
        self.assertIn(ESCALATE_LABEL, USER_VISIBLE_STATES)


class EventKeyTests(unittest.TestCase):
    def test_self_heal_attempt_fields_are_allowlisted(self):
        for key in ("node_id", "run_id", "action_id", "operation", "outcome",
                    "differences", "tier", "unchanged_beats"):
            self.assertIn(key, _EVENT_DATA_KEYS)


class BeatTests(IncidentFixture):
    def test_investigate_branch_appears_on_the_third_unchanged_beat_only(self):
        for _ in range(2):
            out = self.beat()
            self.assertNotIn("需要监工排查：", out.text)
            self.assertNotIn("heal-verify", out.text)
        out = self.beat()
        self.assertTrue(out.text.startswith("需要监工排查："), out.text)
        self.assertIn(NODE, out.text)
        self.assertIn("3", out.text)
        self.assertIn("vibe heal-verify --run-id {} --node {}".format(self.run_id, NODE), out.text)
        self.assertNotIn("--recheck", out.text)
        self.assertNotIn("stall-check", out.text)

    def test_investigate_branch_carries_the_pack_in_the_output_itself(self):
        for _ in range(3):
            out = self.beat()
        for fragment in ("missing field: resumed", "resumed", "threadId", "results", "requests",
                         "repair_entries"):
            self.assertIn(fragment, out.text)

    def test_beat_payload_carries_the_pack_and_never_the_receipt_value(self):
        for _ in range(3):
            out = self.beat()
        entry = out.payload["stall"]["nodes"][NODE]
        self.assertEqual(entry["tier"], "supervisor_investigate")
        self.assertEqual(entry["unchanged_beats"], 3)
        self.assertEqual(entry["pack"]["differences"], ["missing field: resumed"])
        self.assertEqual(entry["pack"]["expected"][0]["field"], "resumed")

    def test_each_beat_assesses_once_and_persists_the_record(self):
        with patch.object(stall, "record_beat", wraps=stall.record_beat) as spy:
            self.beat()
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(self.progress_entry()["unchanged_beats"], 1)
        self.beat()
        self.assertEqual(self.progress_entry()["unchanged_beats"], 2)

    def test_escalation_after_two_attempts_addresses_the_product_manager(self):
        for _ in range(3):
            self.beat()
        self.verify()
        self.verify()
        out = self.beat()
        self.assertTrue(out.text.startswith("需要人处理："), out.text)
        self.assertEqual(out.payload["stall"]["nodes"][NODE]["tier"], "escalate_to_pm")
        self.assertIn("missing field: resumed", out.text)
        self.assertEqual(out.text.count("self_heal_attempt"), 2)
        self.assertNotIn("需要产品经理", out.text)

    def test_progress_resets_the_count_and_the_branch_disappears(self):
        for _ in range(3):
            self.beat()
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE]["handoff_file"] = ".vibe/handoff.md"
        save_snapshot(self.paths, snapshot)
        out = self.beat()
        self.assertNotIn("需要监工排查：", out.text)
        self.assertEqual(self.progress_entry()["unchanged_beats"], 1)

    def test_waiting_node_that_would_read_preparing_is_not_flagged(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE].pop("retryable_action")
        snapshot.nodes[NODE]["status"] = "planned"
        save_snapshot(self.paths, snapshot)
        for _ in range(4):
            out = self.beat()
        self.assertNotIn("需要监工排查：", out.text)
        self.assertEqual(out.payload["stall"]["nodes"], {})

    def test_failed_assessment_on_a_readable_run_is_visible_in_the_text(self):
        with patch.object(stall, "record_beat", side_effect=OSError("disk")):
            out = self.beat()
        self.assertEqual(out.payload["stall"]["status"], "unknown")
        self.assertIn("空转评估未知", out.text)
        self.assertIn("OSError", out.text)

    def test_unreadable_run_is_reported_unknown_not_clean(self):
        out = run_cli(["supervisor-beat", "--run-id", "run-missing",
                       "--session-record", str(self.record)], self.root)
        self.assertEqual(out.payload["stall"]["status"], "unknown")


class HealVerifyTests(IncidentFixture):
    def test_there_is_no_separate_pack_command_or_recheck_flag(self):
        for argv in (["stall-check", "--run-id", self.run_id, "--node", NODE],
                     ["heal-verify", "--run-id", self.run_id, "--node", NODE, "--recheck"]):
            with self.subTest(argv=argv):
                out = run_cli(argv, self.root)
                self.assertNotEqual(out.exit_code, 0)
                self.assertEqual(self.attempts(), [])

    def test_failed_verify_keeps_state_and_records_a_failed_attempt(self):
        for _ in range(3):
            self.beat()
        before = self.progress_entry()
        out = self.verify()
        self.assertEqual(out.payload["status"], "verify_failed")
        self.assertNotEqual(out.exit_code, 0)
        self.assertEqual(out.payload["differences"], ["missing field: resumed"])
        self.assertEqual(self.progress_entry(), before)
        events = self.attempts()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["outcome"], "failed")

    def test_passed_verify_records_the_attempt_and_clears_the_stall_count(self):
        for _ in range(3):
            self.beat()
        self.fix_receipt()
        out = self.verify()
        self.assertEqual(out.payload["status"], "verify_passed")
        self.assertEqual(out.exit_code, 0)
        self.assertEqual(self.progress_entry()["unchanged_beats"], 0)
        data = self.attempts()[0]["data"]
        self.assertEqual(data["outcome"], "passed")
        self.assertEqual(data["node_id"], NODE)
        self.assertEqual(data["run_id"], self.run_id)
        self.assertEqual(data["operation"], "resume")
        self.assertEqual(data["action_id"], self.action["action_id"])
        self.assertEqual(data["tier"], "supervisor_investigate")
        self.assertEqual(data["unchanged_beats"], 3)
        self.assertEqual(data["differences"], [])

    def test_a_consumed_fix_cannot_pass_a_later_unrelated_stall(self):
        for _ in range(3):
            self.beat()
        self.fix_receipt()
        self.assertEqual(self.verify().payload["status"], "verify_passed")
        again = self.verify()
        self.assertEqual(again.payload["status"], "verify_failed")

    def test_a_pack_from_an_earlier_stall_state_is_not_reused(self):
        for _ in range(3):
            self.beat()
        self.fix_receipt()
        snapshot = load_snapshot(self.paths, self.run_id)
        snapshot.nodes[NODE]["handoff_file"] = ".vibe/handoff.md"
        save_snapshot(self.paths, snapshot)
        out = self.verify()
        self.assertEqual(out.payload["status"], "verify_failed")

    def test_verify_without_any_refused_receipt_does_not_pass(self):
        snapshot = load_snapshot(self.paths, self.run_id)
        out = self.verify()
        self.assertEqual(out.payload["status"], "verify_failed")
        self.assertEqual(snapshot.nodes[NODE]["status"], "retry_pending")

    def test_unknown_node_is_refused(self):
        out = run_cli(["heal-verify", "--run-id", self.run_id, "--node", "ghost", "--json"], self.root)
        self.assertEqual(out.payload["status"], "blocked_invalid")

    def test_missing_arguments_are_refused(self):
        out = run_cli(["heal-verify", "--run-id", self.run_id, "--json"], self.root)
        self.assertEqual(out.payload["status"], "blocked_invalid")


class ResumeTextTests(IncidentFixture):
    def stalled(self):
        for _ in range(3):
            self.beat()

    def status(self):
        return _snapshot_result(
            "resume", load_snapshot(self.paths, self.run_id), False, paths=self.paths)

    def test_status_text_while_stalled_names_the_new_label_not_auto_repair(self):
        self.stalled()
        out = self.status()
        self.assertNotIn(RESUMING_TEXT, out.text)
        self.assertIn(INVESTIGATE_LABEL, out.text)
        self.assertEqual(out.payload["nodes"][NODE]["user_status"], INVESTIGATE_LABEL)

    def test_not_stalled_retry_keeps_the_auto_repair_text(self):
        self.beat()
        out = self.status()
        self.assertIn(RESUMING_TEXT, out.text)
        self.assertEqual(out.payload["nodes"][NODE]["user_status"], "自动修复中")

    def test_marker_is_never_persisted_into_the_snapshot(self):
        self.stalled()
        self.status()
        node = load_snapshot(self.paths, self.run_id).nodes[NODE]
        self.assertNotIn("stall_tier", node)


class IntegrityTests(IncidentFixture):
    def test_tampered_event_log_blocks_the_verify_and_writes_nothing(self):
        for _ in range(3):
            self.beat()
        self.fix_receipt()
        path = self.paths.vibe / "runs" / self.run_id / "events.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        record = json.loads(lines[0])
        record["data"]["tampered"] = True
        lines[0] = json.dumps(record, sort_keys=True, separators=(",", ":"))
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        size = path.stat().st_size
        out = self.verify()
        self.assertEqual(out.payload["status"], "blocked_integrity")
        self.assertNotEqual(out.exit_code, 0)
        self.assertEqual(path.stat().st_size, size)
        self.assertEqual(self.progress_entry()["unchanged_beats"], 3)

    def test_hand_edited_state_json_blocks_even_with_a_previous_snapshot(self):
        for _ in range(3):
            self.beat()
        self.fix_receipt()
        path = self.paths.vibe / "runs" / self.run_id / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["authorization_digest"] = "0" * 64
        path.write_text(json.dumps(state), encoding="utf-8")
        out = self.verify()
        self.assertEqual(out.payload["status"], "blocked_integrity")
        self.assertEqual(self.attempts(), [])

    def test_a_broken_chain_is_reported_even_before_any_fix(self):
        path = self.paths.vibe / "runs" / self.run_id / "events.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace('"event"', '"evnt"', 1),
                        encoding="utf-8")
        out = self.verify()
        self.assertEqual(out.payload["status"], "blocked_integrity")


if __name__ == "__main__":
    unittest.main()
