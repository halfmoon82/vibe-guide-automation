"""Supervisor runtime consolidation: no idle spinning, Claude Code never rotates,
short supervisor card, validated mailbox write-back, protocol delivered by ref."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.paths import ProjectPaths
from vibe_guide.protocols import SHIPPED_PROTOCOL_DIGESTS, protocol_path
from vibe_guide.runners.provider_action import (
    _sdd_protocol_delivery,
    complete_mailbox_action,
    validate_result_shape,
)
from vibe_guide.supervisor import (
    BEAT_REPEAT_TEXT,
    IDLE_BEAT_TEXT,
    beat_repeat_check,
    beat_repeat_commit,
    register_supervisor_address,
    render_beat_text,
    supervisor_preflight,
)
from tests.test_issue_93_protocol_delivery import CONTRACT, _RecordingRunner

RUN = "run-1"


class _Case(unittest.TestCase):
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

    def record(self, name, tokens):
        path = Path(self.tmp.name) / name
        path.write_text(json.dumps({"type": "session_meta", "payload": {"id": name}}) + "\n"
                        + json.dumps({"context_tokens": tokens}) + "\n", encoding="utf-8")
        return path

    def register(self, provider, session_id="s1", record=None):
        return register_supervisor_address(
            self.paths, RUN, {"provider": provider, "session_id": session_id, "host": "local"},
            session_record=record)

    def request(self, operation, role="developer", sequence=1):
        return ProviderActionStore(self.paths).request(
            operation=operation, provider="claude-code", run_id=RUN, issue_id="node-a",
            role=role, generation=1, sequence=sequence,
            native_tool="ccd_session_mgmt__send_message", request={"threadId": "t1"})


class IdleAndRepeatTests(_Case):
    def test_idle_text_ends_the_turn(self):
        self.register("codex-app-visible")
        payload = {"state": "idle"}
        text = render_beat_text(self.paths, RUN, payload, plan_id="p", interval_minutes=30)
        self.assertEqual(text, IDLE_BEAT_TEXT)
        self.assertIn("本轮结束", text)
        self.assertTrue(payload["end_turn"])

    def test_same_fingerprint_within_window_stops(self):
        self.register("codex-app-visible")
        stop, fingerprint = beat_repeat_check(self.paths, RUN, now=1000.0)
        self.assertIsNone(stop)
        beat_repeat_commit(self.paths, RUN, fingerprint, {"state": "idle"}, now=1000.0)
        stop, _ = beat_repeat_check(self.paths, RUN, now=1005.0)
        self.assertEqual(stop["state"], "stop")
        self.assertEqual(stop["reason"], BEAT_REPEAT_TEXT)
        self.assertTrue(stop["end_turn"])
        payload = dict(stop)
        self.assertEqual(render_beat_text(self.paths, RUN, payload, plan_id="p"), BEAT_REPEAT_TEXT)

    def test_new_delivery_or_elapsed_window_runs_normally(self):
        self.register("codex-app-visible")
        _, fingerprint = beat_repeat_check(self.paths, RUN, now=1000.0)
        beat_repeat_commit(self.paths, RUN, fingerprint, {"state": "work"}, now=1000.0)
        self.request("wait")  # a new mailbox item changes the fingerprint
        stop, _ = beat_repeat_check(self.paths, RUN, now=1005.0)
        self.assertIsNone(stop)
        stop, fingerprint = beat_repeat_check(self.paths, RUN, now=1006.0)
        beat_repeat_commit(self.paths, RUN, fingerprint, {"state": "work"}, now=1006.0)
        stop, _ = beat_repeat_check(self.paths, RUN, now=1006.0 + 121)
        self.assertIsNone(stop)

    def test_unknown_beat_never_suppresses_retry(self):
        self.register("codex-app-visible")
        _, fingerprint = beat_repeat_check(self.paths, RUN, now=1000.0)
        beat_repeat_commit(self.paths, RUN, fingerprint, {"state": "unknown"}, now=1000.0)
        stop, _ = beat_repeat_check(self.paths, RUN, now=1001.0)
        self.assertIsNone(stop)


class RotationByProviderTests(_Case):
    def preflight(self, provider, tokens):
        record = self.record("shift.jsonl", 20000)
        self.register(provider, record=record)
        record.write_text(json.dumps({"context_tokens": tokens}), encoding="utf-8")
        with patch("vibe_guide.supervisor._mailbox_state",
                   return_value={"state": "idle", "reason": "nothing pending"}):
            return supervisor_preflight(self.paths, RUN, record)

    def test_claude_code_over_cap_works_with_context_pressure(self):
        result = self.preflight("claude-code-visible", 250000)
        self.assertEqual(result["state"], "work", result)
        self.assertIn("context_pressure", result)

    def test_codex_over_cap_still_rotates(self):
        self.assertEqual(self.preflight("codex-app-visible", 250000)["state"], "rotate")

    def test_claude_code_under_cap_is_unchanged(self):
        result = self.preflight("claude-code-visible", 50000)
        self.assertEqual(result["state"], "idle")
        self.assertNotIn("context_pressure", result)


class SupervisorCardTests(_Case):
    def test_card_is_short_and_published(self):
        path = protocol_path("supervisor-card")
        raw = path.read_bytes()
        self.assertLessEqual(len(raw), 3072)
        self.assertIn(hashlib.sha256(raw).hexdigest(), SHIPPED_PROTOCOL_DIGESTS["supervisor-card"])

    def test_work_prints_card_once_per_shift_then_reference(self):
        self.register("codex-app-visible", "shift-a")
        first, second = {"state": "work"}, {"state": "work"}
        text1 = render_beat_text(self.paths, RUN, first, plan_id="p")
        text2 = render_beat_text(self.paths, RUN, second, plan_id="p")
        sha = hashlib.sha256(protocol_path("supervisor-card").read_bytes()).hexdigest()
        self.assertIn("监工卡（每班一次", text1)
        self.assertNotIn("监工卡（每班一次", text2)
        for text in (text1, text2):
            self.assertIn(sha, text)
            self.assertIn(str(protocol_path("supervisor-card")), text)
        # A new shift prints the full card again.
        self.register("codex-app-visible", "shift-b", self.record("shift-b.jsonl", 1000))
        self.assertIn("监工卡（每班一次", render_beat_text(self.paths, RUN, {"state": "work"}, plan_id="p"))

    def test_digest_mismatch_makes_beat_unknown_with_reason(self):
        self.register("codex-app-visible")
        payload = {"state": "work"}
        with patch.dict(SHIPPED_PROTOCOL_DIGESTS, {"supervisor-card": frozenset({"0" * 64})}):
            text = render_beat_text(self.paths, RUN, payload, plan_id="p")
        self.assertEqual(payload["state"], "unknown")
        self.assertIn("supervisor card unavailable", payload["reason"])
        self.assertTrue(text.startswith("unknown"))


class MailboxWriteBackTests(_Case):
    def test_transient_wait_statuses_need_only_status(self):
        for status in ("timeout", "unknown", "error"):
            validate_result_shape("wait", {"status": status}, role="developer")
            validate_result_shape("wait", {"status": status, "cursor": "c1"}, role="developer")
            with self.assertRaises(ValueError) as caught:
                validate_result_shape("wait", {"status": status, "bogus": 1}, role="developer")
            self.assertIn("bogus", str(caught.exception))
            self.assertNotIn("event", str(caught.exception).replace("unexpected", ""))

    def test_developer_wait_without_delivery_evidence_is_rejected_unconsumed(self):
        action = self.request("wait")
        with self.assertRaises(ValueError) as caught:
            complete_mailbox_action(self.paths, RUN, action["action_id"],
                                    {"status": "complete", "event": "delivered"})
        self.assertIn("delivery_evidence", str(caught.exception))
        store = ProviderActionStore(self.paths)
        self.assertIsNone(store.result(action["action_id"]))
        self.assertIn(action["action_id"], [a["action_id"] for a in store.pending(RUN)])

    def test_extra_field_is_named(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape("wait", {"status": "complete", "event": "delivered",
                                           "delivery_evidence": {}, "bogus": 1}, role="developer")
        self.assertIn("bogus", str(caught.exception))

    def test_valid_result_is_stored_through_the_provider_store(self):
        action = self.request("wait")
        payload = {"status": "complete", "event": "delivered", "cursor": "c1",
                   "delivery_evidence": {"session_cwd": "/x"}}
        self.assertTrue(complete_mailbox_action(self.paths, RUN, action["action_id"],
                                                payload, native=False))
        self.assertEqual(ProviderActionStore(self.paths).result(action["action_id"]), payload)

    def test_other_run_and_resume_shape(self):
        action = self.request("resume")
        with self.assertRaises(ValueError):
            complete_mailbox_action(self.paths, "run-2", action["action_id"], {"resumed": True})
        with self.assertRaises(ValueError) as caught:
            validate_result_shape("resume", {"ok": True})
        self.assertIn("resumed", str(caught.exception))
        validate_result_shape("resume", {"resumed": True})

    def test_create_needs_binding_identity(self):
        with self.assertRaises(ValueError) as caught:
            validate_result_shape("create", {"binding": {}})
        self.assertIn("binding", str(caught.exception))
        validate_result_shape("create", {"binding": {"threadId": "t", "hostId": "h"}})


class ProtocolRefDefaultTests(unittest.TestCase):
    def test_default_is_ref_and_inline_is_explicit(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_sdd_protocol_delivery(Path(d)), "ref")
            (Path(d) / ".vibe").mkdir()
            (Path(d) / ".vibe" / "config.json").write_text(json.dumps({"sdd_protocol_delivery": "inline"}))
            self.assertEqual(_sdd_protocol_delivery(Path(d)), "inline")

    def test_default_dispatch_prompt_has_path_and_sha_only(self):
        name = "visible-sdd-worker"
        with tempfile.TemporaryDirectory() as d:
            runner = _RecordingRunner(ProjectPaths(Path(d)))
            runner.task_binding(dict(CONTRACT), Path(d), "run-216", "running")
            prompt = [r for op, r in runner.requests if op == "create"][0]["prompt"]
        digest = hashlib.sha256(protocol_path(name).read_bytes()).hexdigest()
        self.assertIn(str(protocol_path(name)), prompt)
        self.assertIn(digest, prompt)
        self.assertNotIn("## 1. 固定流程", prompt)


if __name__ == "__main__":
    unittest.main()


def _cli_calls_beat_entry():
    from vibe_guide import cli
    return "supervisor_beat(" in Path(cli.__file__).read_text(encoding="utf-8")


@unittest.skipUnless(_cli_calls_beat_entry(),
                     "cli.py supervisor-beat 尚未接入 supervisor.supervisor_beat（待 scope-expand）")
class BeatEntryWiringTests(_Case):
    """The beat features must be reachable from ``vibe supervisor-beat``."""

    def beat(self, record):
        from vibe_guide.cli import run_cli
        return run_cli(["supervisor-beat", "--run-id", RUN, "--session-record", str(record)], self.root)

    def test_cli_idle_ends_turn_and_repeat_stops(self):
        record = self.record("shift.jsonl", 1000)
        self.register("codex-app-visible", record=record)
        with patch("vibe_guide.supervisor._mailbox_state",
                   return_value={"state": "idle", "reason": "nothing pending"}):
            first = self.beat(record).payload
            second = self.beat(record).payload
        self.assertEqual(first["state"], "idle")
        self.assertTrue(first["end_turn"])
        self.assertEqual(second["state"], "stop")
        self.assertTrue(second["end_turn"])

    def test_cli_new_delivery_runs_normally(self):
        record = self.record("shift.jsonl", 1000)
        self.register("codex-app-visible", record=record)
        with patch("vibe_guide.supervisor._mailbox_state",
                   return_value={"state": "idle", "reason": "nothing pending"}):
            self.beat(record)
            self.request("wait")
            self.assertNotEqual(self.beat(record).payload["state"], "stop")

    def test_cli_work_prints_card_then_reference(self):
        record = self.record("shift.jsonl", 1000)
        self.register("codex-app-visible", record=record)
        with patch("vibe_guide.supervisor._mailbox_state",
                   return_value={"state": "work", "reason": "pending"}):
            first = self.beat(record).text
            self.request("wait")
            second = self.beat(record).text
        self.assertIn("监工卡（每班一次", first)
        self.assertNotIn("监工卡（每班一次", second)
