"""#277: persisted failures keep their structured classification readable.

Before this fix, a block only persisted provider free text under ``reason``,
which the durability sanitizer replaced wholesale with
``[REDACTED_PROVIDER_TEXT]``.  ``vibe status`` and the event log could not
say *which* error happened (missing worktree, stopped session, lease
conflict, capability-observation failure) without a probe-side monkey
patch.  The contract:

* ``reason_class`` / ``error_type`` / ``reason_code`` are program-authored
  fixed enums and type names -- they persist verbatim on the node and on
  the event, inside and outside otherwise-redacted provider payloads;
* provider free text is still redacted on write, including text embedded
  in dicts; field names that look like secrets stay masked;
* ``VIBE_VERBOSE=1`` is the opt-in debug switch that keeps the original
  text for live debugging.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.contracts import RunEvent
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import (
    _REDACTED_PROVIDER_TEXT,
    _sanitize_event_data,
    append_event,
    load_events,
    load_snapshot,
    redact_provider_text,
    save_snapshot,
    RunSnapshot,
    STRUCTURED_REASON_KEYS,
)


def _node(node_id):
    return DAGNode(
        node_id,
        node_id,
        [],
        [],
        "g1",
        {
            "files": [node_id + ".py"],
            "worker": "worker-" + node_id,
            "worktree": ".worktrees/" + node_id,
            "worker_profile": {"worker": "codex", "model": "test", "reasoning": "normal", "fallbacks": [], "selection_basis": {"issue_complexity_ref": node_id, "complexity_band": "standard", "risk_tags": [], "availability_evidence": "test"}, "writer": "writer", "worktree": ".worktrees/" + node_id, "branch": "branch-" + node_id, "allowlist": [node_id + ".py"]},
        },
        "ready",
    )


class RedactProviderTextStructureTests(unittest.TestCase):
    def test_structured_keys_survive_inside_a_redacted_payload(self):
        payload = {
            "reason": "provider said token abc123 lease conflict",
            "reason_class": "binding_unknown",
            "reason_code": "worktree_missing",
            "error_type": "FileNotFoundError",
            "proof": {"output": "raw provider output"},
        }
        result = redact_provider_text(payload)
        self.assertEqual(result["reason"], _REDACTED_PROVIDER_TEXT)
        self.assertEqual(result["reason_class"], "binding_unknown")
        self.assertEqual(result["reason_code"], "worktree_missing")
        self.assertEqual(result["error_type"], "FileNotFoundError")
        # Nested provider payload keeps being redacted.
        self.assertEqual(result["proof"], {"output": _REDACTED_PROVIDER_TEXT})

    def test_structured_keys_do_not_widen_for_containers(self):
        # A container under a structured name is provider payload, not an
        # enum -- it must still be redacted, not passed through.
        result = redact_provider_text({"reason_class": ["leak", "me"]})
        self.assertEqual(
            result["reason_class"], [_REDACTED_PROVIDER_TEXT, _REDACTED_PROVIDER_TEXT]
        )

    def test_secret_named_keys_stay_dropped(self):
        result = redact_provider_text(
            {"reason": "x", "api_token": "abc", "reason_class": "binding_unknown"}
        )
        self.assertNotIn("api_token", result)
        self.assertEqual(result["reason_class"], "binding_unknown")

    def test_provider_text_keys_are_disjoint_from_structured_keys(self):
        # Re-pinning the #256 contract: nothing may sit on both sides of the
        # redaction boundary.
        from vibe_guide.state import _PROVIDER_TEXT_KEYS

        self.assertFalse(set(_PROVIDER_TEXT_KEYS) & set(STRUCTURED_REASON_KEYS))

    def test_verbose_env_keeps_provider_text_verbatim(self):
        with mock.patch.dict(os.environ, {"VIBE_VERBOSE": "1"}):
            self.assertEqual(
                redact_provider_text("provider raw text"), "provider raw text"
            )
            # Field-name secret masking is still applied under verbose.
            self.assertEqual(
                redact_provider_text({"token": "abc", "reason": "x"}),
                {"token": "[REDACTED]", "reason": "x"},
            )
        with mock.patch.dict(os.environ, {"VIBE_VERBOSE": "0"}):
            self.assertEqual(
                redact_provider_text("provider raw text"), _REDACTED_PROVIDER_TEXT
            )


class EventStructuredFieldTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))

    def tearDown(self):
        self.temporary.cleanup()

    def test_error_type_survives_event_sanitization(self):
        data = _sanitize_event_data(
            {
                "run_id": "run-1",
                "node_id": "n1",
                "reason": "provider raw text",
                "reason_class": "binding_unknown",
                "reason_code": "worktree_missing",
                "error_type": "FileNotFoundError",
            }
        )
        self.assertEqual(data["reason"], _REDACTED_PROVIDER_TEXT)
        self.assertEqual(data["reason_class"], "binding_unknown")
        self.assertEqual(data["reason_code"], "worktree_missing")
        self.assertEqual(data["error_type"], "FileNotFoundError")

    def test_blocked_unknown_event_persists_structured_fields(self):
        append_event(
            self.paths,
            RunEvent(
                "blocked_unknown",
                {
                    "run_id": "run-1",
                    "node_id": "n1",
                    "reason": "provider raw text with details",
                    "reason_class": "binding_unknown",
                    "error_type": "ConnectionError",
                },
            ),
        )
        (record,) = load_events(self.paths, "run-1")
        self.assertEqual(record["data"]["reason"], _REDACTED_PROVIDER_TEXT)
        self.assertEqual(record["data"]["reason_class"], "binding_unknown")
        self.assertEqual(record["data"]["error_type"], "ConnectionError")


class MarkBlockedUnknownStructureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        (self.paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
        (self.paths.vibe / "state.json").write_text(
            '{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8"
        )
        save_contract(
            self.paths,
            build_contract(self.paths.root, provider="fake", host_id="local"),
        )
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
        node = _node("n1")
        plan = Plan("plan-1", 1, "docs/prd.md", [node.id], "draft")
        card = build_authorization_card(plan, [node], self.capabilities)
        self.monitor = Monitor(self.paths, plan, [node])
        # ``start`` runs the real snapshot-creation path so the persisted
        # record carries a valid authorization lineage.
        self.snapshot = self.monitor.start(authorize(card, "AUTHORIZE"), FakeRunner())

    def tearDown(self):
        self.temporary.cleanup()

    def _persisted_node(self):
        save_snapshot(self.paths, self.snapshot)
        return load_snapshot(self.paths, self.snapshot.run_id).nodes["n1"]

    def test_node_reason_class_survives_persistence(self):
        self.monitor._mark_blocked_unknown(
            self.snapshot, "n1", "writer lease is already owned by another run",
            quarantine_lease=False,
        )
        node = self._persisted_node()
        self.assertEqual(node["status"], "blocked_unknown")
        self.assertEqual(node["reason"], _REDACTED_PROVIDER_TEXT)
        # The readable structured classification the spec asks for:
        # a lease conflict is classified binding_unknown.
        self.assertEqual(node["reason_class"], "binding_unknown")
        self.assertNotIn("error_type", node)
        self.assertNotIn("reason_code", node)

    def test_exception_reason_stamps_error_type(self):
        self.monitor._mark_blocked_unknown(
            self.snapshot,
            "n1",
            ConnectionError("connection timed out"),
            quarantine_lease=False,
        )
        node = self._persisted_node()
        self.assertEqual(node["error_type"], "ConnectionError")
        self.assertEqual(node["reason_class"], "retryable")

    def test_cause_stamps_error_type_without_changing_reason(self):
        self.monitor._mark_blocked_unknown(
            self.snapshot,
            "n1",
            "task binding rejected before start (ValueError)",
            quarantine_lease=False,
            cause=ValueError("bad contract"),
        )
        node = self._persisted_node()
        self.assertEqual(node["error_type"], "ValueError")

    def test_reason_code_stamps_on_node_and_event(self):
        self.monitor._mark_blocked_unknown(
            self.snapshot,
            "n1",
            "provider dispatch contract rejected",
            quarantine_lease=False,
            reason_code="dispatch_contract_invalid",
        )
        node = self._persisted_node()
        self.assertEqual(node["reason_code"], "dispatch_contract_invalid")
        events = load_events(self.paths, self.snapshot.run_id)
        blocked = [e for e in events if e["event"] == "blocked_unknown"]
        self.assertTrue(blocked)
        self.assertEqual(blocked[-1]["data"]["reason_class"], "binding_unknown")
        self.assertEqual(blocked[-1]["data"]["reason_code"], "dispatch_contract_invalid")

    def test_stale_structured_fields_are_cleared(self):
        self.monitor._mark_blocked_unknown(
            self.snapshot,
            "n1",
            ConnectionError("lost"),
            quarantine_lease=False,
            reason_code="child_binding_conflict",
        )
        self.monitor._mark_blocked_unknown(
            self.snapshot, "n1", "some unrelated prose", quarantine_lease=False
        )
        node = self._persisted_node()
        self.assertNotIn("error_type", node)
        self.assertNotIn("reason_code", node)

    def test_blocked_event_carries_the_classification(self):
        self.monitor._mark_blocked_unknown(
            self.snapshot, "n1", "event arrived from an unregistered handle",
            quarantine_lease=False,
        )
        events = load_events(self.paths, self.snapshot.run_id)
        blocked = [e for e in events if e["event"] == "blocked_unknown"]
        self.assertTrue(blocked)
        data = blocked[-1]["data"]
        # The event no longer leaves only a placeholder for an auditor:
        # the failure class is a readable enum beside the redacted reason.
        self.assertIn("reason_class", data)
        self.assertIsInstance(data["reason_class"], str)


class StatusErrorClassRenderTests(unittest.TestCase):
    def test_render_names_each_blocked_node_class(self):
        from vibe_guide.cli import _render_node_error_classes

        snapshot = RunSnapshot(
            run_id="run-1",
            plan_id="plan-1",
            plan_version=1,
            status="blocked_unknown",
            nodes={
                "n1": {
                    "status": "blocked_unknown",
                    "reason": _REDACTED_PROVIDER_TEXT,
                    "reason_class": "binding_unknown",
                    "reason_code": "worktree_missing",
                    "error_type": "FileNotFoundError",
                },
                "n2": {"status": "running"},
            },
            handles={},
        )
        text = _render_node_error_classes(snapshot)
        self.assertIn("n1", text)
        self.assertIn("binding_unknown", text)
        self.assertIn("worktree_missing", text)
        self.assertIn("FileNotFoundError", text)
        self.assertNotIn("n2", text)

    def test_render_is_empty_without_structured_stamps(self):
        from vibe_guide.cli import _render_node_error_classes

        snapshot = RunSnapshot(
            run_id="run-1",
            plan_id="plan-1",
            plan_version=1,
            status="blocked_unknown",
            nodes={"n1": {"status": "blocked_unknown"}},
            handles={},
        )
        self.assertEqual(_render_node_error_classes(snapshot), "")


if __name__ == "__main__":
    unittest.main()
