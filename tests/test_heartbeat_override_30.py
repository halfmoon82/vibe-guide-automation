"""30 minutes on a platform without verified push wake-up is a forced choice.

The card lists it as selectable with its price; only an explicit product
choice records it; the runtime honours that choice instead of silently
returning to 10.  Everything else keeps behaving as before.
"""
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.authorization import (
    AuthorizationRecord, authorize, build_authorization_card, is_authorization_integrity_valid,
)
from vibe_guide.cli import _render_card_options
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import (
    FORCED_INTERVAL_COST,
    authorization_choices,
    heartbeat_interval,
    heartbeat_interval_options,
    register_supervisor_address,
)

CAPS = AgentCapabilities("claude-code", True, True, True, False, True, "full")


def _plan():
    node = DAGNode("n1", "N1", [], [], None,
                   {"input": "i", "output": "o", "error_behavior": "e",
                    "acceptance_example": "a", "adapter_id": "claude-code", "files": ["a.py"]},
                   "planned")
    plan = Plan("p1", 1, ".vibe/plans/p1/prd.md", ["n1"], "confirmed_pending_authorization",
                authorization_required=True, nodes=[node])
    return plan, [node]


class ForcedThirtyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / ".vibe").mkdir()
        self.paths = ProjectPaths(self.root)

    def _card(self, minutes, options=None):
        plan, nodes = _plan()
        options = options if options is not None else heartbeat_interval_options(
            self.paths, "claude-code")
        return build_authorization_card(
            plan, nodes, CAPS, heartbeat_interval_minutes=minutes,
            heartbeat_interval_options=options)

    def test_publishing_30_without_verification_succeeds_and_marks_the_force(self):
        card = self._card(30)
        self.assertEqual(card.heartbeat_interval_minutes, 30)
        entry = card.heartbeat_interval_options[1]
        self.assertTrue(entry["forced"])
        self.assertEqual(entry["cost"], FORCED_INTERVAL_COST)
        self.assertIn("最长 30 分钟", entry["cost"])
        rendered = _render_card_options(card.heartbeat_interval_options, "minutes", "分钟")
        self.assertIn("强制选择", rendered)
        self.assertIn(entry["cost"], rendered)

    def test_choosing_10_leaves_the_card_as_before(self):
        card = self._card(10)
        self.assertEqual(card.heartbeat_interval_minutes, 10)
        self.assertEqual(card.heartbeat_interval_options[0],
                         {"minutes": 10, "available": True, "reason": ""})

    def test_a_forced_tier_without_a_cost_refuses_publication(self):
        options = [{"minutes": 10, "available": True, "reason": ""},
                   {"minutes": 30, "available": True, "reason": "x", "forced": True}]
        with self.assertRaises(ValueError) as raised:
            self._card(30, options)
        self.assertIn("cost", str(raised.exception))
        options[1]["cost"] = "  "
        with self.assertRaises(ValueError):
            self._card(30, options)

    def test_no_choice_is_never_30_by_default(self):
        # Offering the forced tier does not select it: the runtime stays at 10.
        register_supervisor_address(
            self.paths, "run-1", {"provider": "claude-code", "session_id": "S", "host": "mac"})
        interval = heartbeat_interval(self.paths, "run-1", decision_only=False)
        self.assertEqual(interval["minutes"], 10)

    def _authorize(self, minutes, card):
        directory = self.paths.vibe / "plans" / "p1"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "authorization.json").write_text(json.dumps(
            authorize(card, "AUTHORIZE").to_dict()))
        (directory / "authorization-card.json").write_text(json.dumps(card.to_dict()))
        from unittest.mock import patch
        from types import SimpleNamespace
        return patch("vibe_guide.supervisor.load_snapshot",
                     return_value=SimpleNamespace(plan_id="p1"))

    def test_runtime_honours_the_forced_choice(self):
        register_supervisor_address(
            self.paths, "run-1", {"provider": "claude-code", "session_id": "S", "host": "mac"})
        with self._authorize(30, self._card(30)):
            self.assertTrue(authorization_choices(self.paths, "run-1")["heartbeat_interval_forced"])
            interval = heartbeat_interval(self.paths, "run-1", decision_only=False)
        self.assertEqual(interval["minutes"], 30)
        self.assertEqual(interval["mode"], "forced")
        self.assertIn("30 分钟", interval["reason"])

    def test_forced_choice_is_in_the_signed_record(self):
        record = authorize(self._card(30), "AUTHORIZE")
        data = record.to_dict()
        self.assertIs(data["heartbeat_interval_forced"], True)
        self.assertEqual(data["heartbeat_interval_forced_cost"], FORCED_INTERVAL_COST)
        self.assertTrue(is_authorization_integrity_valid(record))
        # The marker is under the digest: stripping it breaks the record.
        stripped = {k: v for k, v in data.items() if not k.startswith("heartbeat_interval_forced")}
        self.assertFalse(is_authorization_integrity_valid(AuthorizationRecord.from_dict(stripped)))
        self.assertEqual(AuthorizationRecord.from_dict(data), record)

    def test_unforced_records_carry_no_marker_and_old_records_load(self):
        for minutes in (10, None):
            data = authorize(self._card(minutes), "AUTHORIZE").to_dict()
            self.assertNotIn("heartbeat_interval_forced", data)
            self.assertNotIn("heartbeat_interval_forced_cost", data)
            self.assertTrue(is_authorization_integrity_valid(AuthorizationRecord.from_dict(data)))

    def test_verified_push_makes_30_unforced_in_the_record(self):
        (self.root / ".vibe/push-wake-verification.json").write_text(json.dumps(
            {"platforms": {"claude-code": {"success": [{"evidence_ref": "s"}],
                                           "failure": [{"evidence_ref": "f"}]}}}))
        data = authorize(self._card(30), "AUTHORIZE").to_dict()
        self.assertEqual(data["heartbeat_interval_minutes"], 30)
        self.assertNotIn("heartbeat_interval_forced", data)

    def test_a_30_without_a_forced_card_still_falls_back(self):
        register_supervisor_address(
            self.paths, "run-1", {"provider": "claude-code", "session_id": "S", "host": "mac"})
        plain = [{"minutes": 10, "available": True, "reason": ""},
                 {"minutes": 30, "available": True, "reason": ""}]
        with self._authorize(30, self._card(30, plain)):
            interval = heartbeat_interval(self.paths, "run-1", decision_only=False)
        self.assertEqual(interval["minutes"], 10)

    def test_verified_platform_behaviour_is_unchanged(self):
        (self.root / ".vibe/push-wake-verification.json").write_text(json.dumps(
            {"platforms": {"claude-code": {"success": [{"evidence_ref": "s"}],
                                           "failure": [{"evidence_ref": "f"}]}}}))
        entry = heartbeat_interval_options(self.paths, "claude-code")[1]
        self.assertEqual(entry, {"minutes": 30, "available": True,
                                 "reason": entry["reason"]})


if __name__ == "__main__":
    unittest.main()
