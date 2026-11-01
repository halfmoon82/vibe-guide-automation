"""wake-and-handoff: a session that is not the registered supervisor cannot run beat/preflight.

Acceptance 4: a non-registered session using someone else's record gets
``register_first`` (never ``rotate``) and the event stream keeps the refusal.
Acceptance 5: a successor on a different model/effort gets a warning at
``supervisor-register`` that never blocks registration.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events
from vibe_guide.supervisor import (
    BEAT_REJECTED_EVENT,
    record_owner_gate,
    register_supervisor_address,
    session_model_tier,
    shift_model_warning,
    supervisor_beat,
    supervisor_preflight,
)

RUN = "run-owner"


def _record(directory, name, tokens=1000, model=None, effort=None):
    lines = [json.dumps({"type": "session_meta", "payload": {"id": name}})]
    if model:
        lines.append(json.dumps({"type": "turn_context", "payload": {
            "model": model, **({"effort": effort} if effort else {})}}))
    lines.append(json.dumps({"context_tokens": tokens}))
    path = Path(directory) / (name + ".jsonl")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "project"
        (self.root / ".vibe").mkdir(parents=True)
        self.paths = ProjectPaths(self.root)

    def register(self, session_id, record):
        return register_supervisor_address(
            self.paths, RUN,
            {"provider": "codex-app-visible", "session_id": session_id, "host": "local"},
            session_record=record)


class RecordOwnerTests(_Case):
    def setUp(self):
        super().setUp()
        self.mine = _record(self.tmp.name, "shift-a")
        self.register("shift-a", self.mine)
        self.other = _record(self.tmp.name, "shift-b", tokens=900000)

    def test_foreign_record_gets_register_first_not_rotate(self):
        # 900k tokens would rotate if the record were trusted.
        result = record_owner_gate(self.paths, RUN, self.other)
        self.assertEqual(result["state"], "register_first")
        self.assertEqual(result["reason_code"], "record_not_registered")
        self.assertIn("supervisor-register", result["register_command"])
        self.assertEqual(supervisor_preflight(self.paths, RUN, self.other)["state"], "rotate")

    def test_refusal_is_written_to_the_event_stream(self):
        record_owner_gate(self.paths, RUN, self.other)
        events = [e for e in load_events(self.paths, RUN) if e.get("event") == BEAT_REJECTED_EVENT]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["reason_code"], "record_not_registered")

    def test_beat_refuses_before_any_state_and_never_rotates(self):
        payload, text, ok = supervisor_beat(self.paths, RUN, self.other)
        self.assertEqual(payload["state"], "register_first")
        self.assertNotEqual(payload["state"], "rotate")
        self.assertFalse(ok)
        self.assertIn("register_first", text)
        self.assertTrue(payload["end_turn"])

    def test_registered_record_passes_the_gate(self):
        self.assertIsNone(record_owner_gate(self.paths, RUN, self.mine))
        payload, _text, _ok = supervisor_beat(self.paths, RUN, self.mine)
        self.assertNotEqual(payload["state"], "register_first")

    def test_unbound_registration_is_not_judged(self):
        other_run = "run-unbound"
        register_supervisor_address(
            self.paths, other_run,
            {"provider": "codex-app-visible", "session_id": "x", "host": "local"})
        self.assertIsNone(record_owner_gate(self.paths, other_run, self.other))

    def test_cli_preflight_returns_register_first(self):
        out = run_cli(["supervisor-preflight", "--run-id", RUN, "--session-record", str(self.other),
                       "--json"], self.root)
        self.assertEqual(out.payload["state"], "register_first")
        self.assertNotEqual(out.exit_code, 0)


class ModelWarningTests(_Case):
    def test_tier_is_read_from_turn_context(self):
        record = _record(self.tmp.name, "s", model="gpt-5", effort="high")
        self.assertEqual(session_model_tier(record), {"model": "gpt-5", "effort": "high"})

    def test_different_model_warns_but_registration_succeeds(self):
        self.register("a", _record(self.tmp.name, "a", model="gpt-5", effort="high"))
        entry = self.register("b", _record(self.tmp.name, "b", model="gpt-4", effort="high"))
        self.assertEqual(entry["model"], "gpt-4")
        warning = shift_model_warning(self.paths, RUN)
        self.assertIn("gpt-5", warning)
        self.assertIn("gpt-4", warning)

    def test_different_effort_warns(self):
        self.register("a", _record(self.tmp.name, "a", model="gpt-5", effort="high"))
        self.register("b", _record(self.tmp.name, "b", model="gpt-5", effort="low"))
        self.assertIn("思考档位", shift_model_warning(self.paths, RUN))

    def test_same_model_and_effort_is_silent(self):
        self.register("a", _record(self.tmp.name, "a", model="gpt-5", effort="high"))
        self.register("b", _record(self.tmp.name, "b", model="gpt-5", effort="high"))
        self.assertIsNone(shift_model_warning(self.paths, RUN))

    def test_cli_register_prints_the_warning(self):
        self.register("a", _record(self.tmp.name, "a", model="gpt-5", effort="high"))
        second = _record(self.tmp.name, "b", model="gpt-4", effort="high")
        out = run_cli(["supervisor-register", "--run-id", RUN, "--provider", "codex-app-visible",
                       "--session-id", "b", "--host", "local", "--session-record", str(second),
                       "--json"], self.root)
        self.assertEqual(out.payload["status"], "ok")
        self.assertIn("gpt-5", out.payload["model_warning"])


if __name__ == "__main__":
    unittest.main()
