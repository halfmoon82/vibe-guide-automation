import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide import heal_pack
from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.heal_pack import build_heal_pack, recheck_heal_pack
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners import provider_action
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.state import durable_projection, redact_provider_text

RUN, NODE, ROLE = "run-1", "integration-review", "developer"


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".vibe").mkdir()
        (self.root / ".vibe" / "state.json").write_text("{}", encoding="utf-8")
        gate = patch("vibe_guide.adapters.task_provider.require_entry")
        gate.start()
        self.addCleanup(gate.stop)
        self.paths = ProjectPaths(self.root)
        self.store = ProviderActionStore(self.paths)

    def action(self, operation, sequence=1, generation=1, node=NODE):
        return self.store.request(
            operation=operation, provider="claude-code", run_id=RUN, issue_id=node,
            role=ROLE, generation=generation, sequence=sequence,
            native_tool="ccd_session_mgmt__send_message",
            request={"threadId": "t1"},
        )

    def answer(self, action, payload):
        self.store.complete(action["action_id"], payload, native=False)

    def result_path(self, action):
        return self.root / ".vibe" / "provider-actions" / "results" / (action["action_id"] + ".json")

    def rewrite_payload(self, action, payload):
        path = self.result_path(action)
        record = json.loads(path.read_text(encoding="utf-8"))
        record["payload"] = payload
        path.write_text(json.dumps(record), encoding="utf-8")

    def pack(self, **kwargs):
        return build_heal_pack(self.paths, RUN, NODE, ROLE, **kwargs)


class ResumeReceiptTests(_Fixture):
    def test_incident_receipt_is_named_field_by_field(self):
        action = self.action("resume")
        self.answer(action, {"threadId": "t1"})
        pack = self.pack(node_status="retry_pending", blocker="provider_resume")
        self.assertTrue(pack["recognized"])
        self.assertEqual(pack["operation"], "resume")
        self.assertEqual(pack["expected"], [{"field": "resumed", "type": "bool", "requirement": "true"}])
        self.assertEqual(pack["actual"], [{"field": "threadId", "type": "str"}])
        self.assertEqual(pack["differences"], ["missing field: resumed"])
        self.assertTrue(pack["files"][1].endswith(action["action_id"] + ".json"))
        self.assertIn("results", pack["files"][1])
        self.assertTrue(pack["repair_entries"])

    def test_pack_carries_no_receipt_values(self):
        action = self.action("resume")
        self.answer(action, {"threadId": "SECRET-VALUE-123", "resumed": "yes"})
        text = json.dumps(self.pack())
        self.assertNotIn("SECRET-VALUE-123", text)
        self.assertNotIn('"yes"', text)

    def test_wrong_type_is_named(self):
        self.answer(self.action("resume"), {"resumed": "true"})
        pack = self.pack()
        self.assertEqual(pack["actual"], [{"field": "resumed", "type": "str"}])
        self.assertIn("must be true", pack["differences"][0])

    def test_fixed_receipt_passes_the_recheck_and_unfixed_does_not(self):
        action = self.action("resume")
        self.answer(action, {"threadId": "t1"})
        pack = self.pack()
        before = recheck_heal_pack(self.paths, pack)
        self.assertFalse(before["passed"])
        self.assertEqual(before["differences"], ["missing field: resumed"])
        self.rewrite_payload(action, {"resumed": True})
        after = recheck_heal_pack(self.paths, pack)
        self.assertEqual(after, {"passed": True, "differences": []})

    def reader_refuses(self, payload):
        """Run the real ``poll`` on a pending resume carrying ``payload``."""
        runner = ProviderActionRunner(self.paths, "claude-code", "claude-code-visible")
        self.store = runner.store
        self._seq = getattr(self, "_seq", 0) + 1
        action = self.action("resume", sequence=self._seq)
        self.answer(action, payload)
        handle = type("Handle", (), {"run_id": "h%d" % self._seq})()
        metadata = {
            "node_id": NODE, "role": ROLE, "run_id": RUN, "task_id": "t1",
            "generation": 1, "pending_action": action["action_id"],
            "pending_operation": "resume",
        }
        runner.store._atomic(runner._handle_path(handle.run_id), metadata)
        try:
            events = runner.poll(handle)
        except Exception:
            return False
        return any(
            e.event == "visibility_unknown"
            and "resume is unverified" in e.data.get("reason", "")
            for e in events
        )

    def test_reader_and_pack_agree_on_every_case(self):
        for payload in ({}, {"resumed": True}, {"resumed": False}, {"resumed": 1},
                        {"resumed": "true"}, {"threadId": "t1"}):
            with self.subTest(payload=payload):
                self.assertEqual(
                    bool(provider_action.resume_receipt_differences(payload)),
                    self.reader_refuses(payload),
                )

    def test_incident_receipt_is_refused_by_the_reader_and_named_by_the_pack(self):
        self.assertTrue(self.reader_refuses({"threadId": "t1"}))
        self.assertEqual(self.pack()["differences"], ["missing field: resumed"])


class WaitResultTests(_Fixture):
    def reader_says_cursor_invalid(self, payload):
        """Run the real ``_wait_result``; also report whether it kept the wait pending."""
        runner = ProviderActionRunner(self.paths, "claude-code", "claude-code-visible")
        handle = type("Handle", (), {"run_id": "hw"})()
        metadata = {"node_id": NODE, "role": ROLE, "run_id": RUN, "pending_action": "x",
                    "pending_operation": "wait"}
        runner.store._atomic(runner._handle_path("hw"), dict(metadata))
        with patch.object(provider_action, "load_task_binding"), \
                patch.object(provider_action, "save_task_binding"):
            events = runner._wait_result(handle, dict(metadata), {"node_id": NODE}, payload)
        invalid = any(e.data.get("reason") == "provider cursor is invalid" for e in events)
        kept = runner.store._read(runner._handle_path("hw")).get("pending_action")
        return invalid, kept

    def test_reader_and_pack_agree_on_every_cursor(self):
        for cursor in (None, "", "ok", "a" * 4096, "a" * 4097, "a\x00b", 5, ["c"]):
            with self.subTest(cursor=cursor):
                payload = {"status": "timeout"}
                if cursor is not None:
                    payload["cursor"] = cursor
                invalid, kept = self.reader_says_cursor_invalid(payload)
                self.assertEqual(bool(provider_action.wait_cursor_differences(payload)), invalid)
                self.assertEqual(kept == "x", invalid)

    def test_bad_cursor_is_named_and_recheck_follows_the_file(self):
        action = self.action("wait")
        self.answer(action, {"status": "completed", "event": "delivered", "cursor": ""})
        pack = self.pack()
        self.assertEqual(pack["operation"], "wait")
        self.assertEqual([item["field"] for item in pack["expected"]], ["cursor"])
        self.assertIn("cursor", pack["differences"][0])
        self.assertFalse(recheck_heal_pack(self.paths, pack)["passed"])
        self.rewrite_payload(action, {"status": "completed", "event": "delivered", "cursor": "c1"})
        self.assertTrue(recheck_heal_pack(self.paths, pack)["passed"])

    def test_consumed_wait_result_has_nothing_to_repair(self):
        payloads = ({"status": "weird", "event": "nope"},
                    {"status": "completed"},
                    {"status": "timeout"})
        for sequence, payload in enumerate(payloads, start=1):
            with self.subTest(payload=payload):
                self.answer(self.action("wait", sequence=sequence), payload)
                latest = self.store.result(self.action("wait", sequence=sequence)["action_id"])
                self.assertEqual(latest, payload)
                self.assertFalse(self.pack()["recognized"])
                self.assertEqual(self.pack()["repair_entries"], [])


class GenericPackTests(_Fixture):
    def test_unrecognized_block_gets_a_generic_pack(self):
        pack = self.pack(
            node_status="blocked_unknown", blocker="mystery",
            recent_events=["tick", "worker_started", "bad event name!", 7, "tick"],
        )
        self.assertFalse(pack["recognized"])
        self.assertEqual(pack["note"], heal_pack.UNRECOGNIZED_NOTE)
        self.assertEqual(pack["expected"], [])
        self.assertEqual(pack["node_status"], "blocked_unknown")
        self.assertEqual(pack["blocker_class"], "mystery")
        self.assertEqual(pack["recent_event_types"], ["tick", "worker_started", "tick"])

    def test_pack_ignores_other_nodes_and_unanswered_actions(self):
        self.action("resume", node="other-node")
        self.answer(self.action("resume", sequence=2, node="other-node"), {"threadId": "t"})
        self.action("resume", sequence=3)
        self.assertFalse(self.pack()["recognized"])

    def test_latest_answered_action_wins(self):
        self.answer(self.action("resume", sequence=1), {"threadId": "t1"})
        self.answer(self.action("resume", sequence=2), {"resumed": True})
        self.assertFalse(self.pack()["recognized"])

    def test_unreadable_result_record_is_reported_not_guessed(self):
        action = self.action("resume")
        self.answer(action, {"resumed": True})
        self.result_path(action).write_text("not json", encoding="utf-8")
        pack = self.pack()
        self.assertTrue(pack["recognized"])
        self.assertEqual(pack["expected"], [])
        self.assertIn("readable", pack["differences"][0])
        self.assertFalse(recheck_heal_pack(self.paths, pack)["passed"])

    def test_recheck_of_a_generic_pack_never_passes(self):
        result = recheck_heal_pack(self.paths, self.pack())
        self.assertFalse(result["passed"])

    def test_caller_supplied_labels_are_not_echoed_unless_identifiers(self):
        pack = self.pack(node_status="has space", blocker="provider text with sk-abc/1")
        self.assertEqual((pack["node_status"], pack["blocker_class"]), ("", ""))
        pack = self.pack(node_status="retry_pending", blocker="provider_resume")
        self.assertEqual((pack["node_status"], pack["blocker_class"]), ("retry_pending", "provider_resume"))

    def test_recheck_tolerates_a_missing_pack(self):
        self.assertFalse(recheck_heal_pack(self.paths, None)["passed"])
        self.assertFalse(recheck_heal_pack(self.paths, {})["passed"])

    def test_recheck_trusts_the_stored_request_not_the_pack_operation(self):
        action = self.action("resume")
        self.answer(action, {"threadId": "t1"})
        pack = dict(self.pack(), operation="wait")
        self.assertFalse(recheck_heal_pack(self.paths, pack)["passed"])

    def test_field_names_that_are_not_identifiers_are_not_echoed(self):
        self.answer(self.action("resume"), {"has space and /path/to/file": 1})
        self.assertEqual(self.pack()["actual"], [{"field": "<unnameable>", "type": "int"}])


class RedactionTests(_Fixture):
    def test_redaction_keeps_the_pack_intact(self):
        self.answer(self.action("resume"), {"threadId": "t1"})
        pack = self.pack(node_status="retry_pending", blocker="provider_resume")
        self.assertEqual(durable_projection(pack)["differences"], pack["differences"])
        self.assertEqual(durable_projection(pack)["expected"], pack["expected"])
        self.assertEqual(durable_projection(pack)["actual"], pack["actual"])
        self.assertEqual(durable_projection(pack)["note"], pack["note"])


if __name__ == "__main__":
    unittest.main()
