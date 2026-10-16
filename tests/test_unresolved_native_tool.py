"""A host "No such tool available" reply is a routing fault, not a result.

Reproduces 2026-10-14 (sessions 653f1b36 / 8eb26fbb / d050dc9b): some model
gateway routes returned helper tool calls with the ``fn__<server>__`` prefix
stripped, so the host answered "No such tool available: list_events".  The
supervisor wrote nothing useful, handed off to a new session three times, and
each successor hit the same fault.  ``complete()`` must keep such a reply as
evidence only, leave the request pending, and tell the caller to retry once
and then stop instead of handing off.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.task_provider import (
    ProviderActionStore,
    UnresolvedNativeTool,
    _canonical_digest,
)
from vibe_guide.paths import ProjectPaths

HOST_ERROR = "<tool_use_error>Error: No such tool available: list_events</tool_use_error>"


class UnresolvedNativeToolTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = ProviderActionStore(ProjectPaths(Path(self._tmp.name)))
        basis = {"operation": "wait", "provider": "claude-code-visible",
                 "run_id": "run-1", "issue_id": "n1", "role": "developer",
                 "generation": 1, "sequence": 0}
        self.action_id = "action-" + _canonical_digest(basis)[:32]
        payload = {"schema_version": 1, "action_id": self.action_id, **basis,
                   "native_tool": "ccd_session_mgmt__list_events", "request": {}}
        payload["request_digest"] = _canonical_digest(payload)
        self.store._atomic(
            self.store._directory("requests") / (self.action_id + ".json"), payload)

    def _observations(self):
        return sorted(self.store._directory("observations").glob(
            self.action_id + "-unresolved-tool-*.json"))

    def test_host_tool_error_is_not_stored_as_result(self):
        with self.assertRaises(UnresolvedNativeTool) as caught:
            self.store.complete(self.action_id, {"status": "failed", "error": HOST_ERROR})
        error = caught.exception
        self.assertEqual(error.called_tool, "list_events")
        self.assertEqual(error.expected_tool, "ccd_session_mgmt__list_events")
        self.assertEqual(error.attempts, 1)
        self.assertIn("retry", str(error))
        self.assertIsNone(self.store.result(self.action_id))
        self.assertEqual(len(self._observations()), 1)
        record = json.loads(self._observations()[0].read_text())
        self.assertEqual(record["kind"], "unresolved_native_tool")
        self.assertEqual(record["called_tool"], "list_events")
        self.assertEqual(record["payload"]["error"], HOST_ERROR)

    def test_second_failure_tells_caller_to_stop_not_hand_off(self):
        for _ in range(2):
            with self.assertRaises(UnresolvedNativeTool) as caught:
                self.store.complete(self.action_id, {"error": HOST_ERROR})
        error = caught.exception
        self.assertEqual(error.attempts, 2)
        self.assertIn("stop", str(error))
        self.assertIn("do not hand off", str(error))
        self.assertEqual(len(self._observations()), 2)
        self.assertIsNone(self.store.result(self.action_id))

    def test_request_stays_serviceable_after_route_recovers(self):
        with self.assertRaises(UnresolvedNativeTool):
            self.store.complete(self.action_id, {"reason": HOST_ERROR})
        self.assertTrue(self.store.complete(self.action_id, {"events": [], "cursor": "c1"}))
        self.assertEqual(self.store.result(self.action_id), {"events": [], "cursor": "c1"})

    def test_raw_host_reply_is_caught_under_any_top_level_key(self):
        for payload in ({"result": HOST_ERROR}, {"output": "  " + HOST_ERROR},
                        {"error": {"message": HOST_ERROR}}, {"detail": "Error: No such tool available: spawn_task"}):
            with self.subTest(payload=payload):
                with self.assertRaises(UnresolvedNativeTool):
                    self.store.complete(self.action_id, payload)
        self.assertIsNone(self.store.result(self.action_id))

    def test_error_is_not_a_value_error(self):
        # Callers catch ValueError as "invalid record"; this must not be swallowed there.
        self.assertFalse(issubclass(UnresolvedNativeTool, ValueError))

    def test_mid_sentence_mention_is_a_normal_result(self):
        payload = {"reason": "上次出现 No such tool available: x，已排除", "cursor": "c3"}
        self.assertTrue(self.store.complete(self.action_id, payload))

    def test_worker_self_report_is_not_screened(self):
        payload = {"reason": HOST_ERROR, "status": "completed"}
        self.assertTrue(self.store.complete(self.action_id, payload, native=False))
        self.assertEqual(self._observations(), [])

    def test_delivery_consumer_does_not_screen_worker_text(self):
        report = {"run_id": "run-1", "issue_id": "n1", "role": "developer", "generation": 1,
                  "payload": {"reason": HOST_ERROR}}
        self.store._directory("deliveries")
        self.store._atomic(self.store._delivery_path("run-1", "n1", "developer", 1), report)
        self.assertTrue(self.store._consume_delivery_locked("run-1", "n1", "developer", 1))
        self.assertEqual(self.store.result(self.action_id)["reason"], HOST_ERROR)
        self.assertEqual(self._observations(), [])

    def test_transcript_text_quoting_the_error_is_a_normal_result(self):
        payload = {"events": [{"text": "earlier: " + HOST_ERROR}], "cursor": "c2"}
        self.assertTrue(self.store.complete(self.action_id, payload))
        self.assertEqual(self.store.result(self.action_id), payload)
        self.assertEqual(self._observations(), [])


class ProtocolRuleTests(unittest.TestCase):
    def _lines(self):
        from vibe_guide import protocols
        return (Path(protocols.__file__).parent / "prd-guide.md").read_text().splitlines()

    def test_mailbox_section_says_retry_once_then_stop(self):
        rule = [line for line in self._lines() if line.startswith("**宿主报\"找不到工具\"时不交班**")]
        self.assertEqual(len(rule), 1)
        for phrase in ("complete()", "UnresolvedNativeTool", "attempts", "第 2 次仍失败：停止服务信箱", "不要开交班会话"):
            self.assertIn(phrase, rule[0])

    def test_recovery_list_excludes_it_from_node_failures(self):
        lines = self._lines()
        start = lines.index("`blocked_unknown` 的标准恢复动作按原因执行：")
        row = [line for line in lines[start:start + 10] if line.startswith("- 宿主报 `No such tool available`")]
        self.assertEqual(len(row), 1)
        self.assertIn("不是节点故障", row[0])
        self.assertIn("不交班", row[0])


if __name__ == "__main__":
    unittest.main()
