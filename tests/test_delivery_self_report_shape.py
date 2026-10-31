"""Delivery shape gate: a self-report that says "blocked" is not a delivery.

Recorded failure (run-89693f36): a developer self-report whose top level said
``status=blocked`` / ``report_kind=blocked_preflight_only`` /
``implementation_complete=false`` was archived with ``event=delivered`` and
handed to the pending wait; the monitor treated it as a delivery and the node
left its blocked state.  #335: ``worker-deliver`` must refuse a blocked
self-report paired with a delivery event -- naming only the field names, never
echoing the worker's free text -- and the monitor must park such a report as a
``blocked_engineering`` event: the node stays blocked, no reviewer is
dispatched, and nothing presents the block as a self-healing retry.
"""
import json as json_module
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import load_events

from tests import test_claude_bg_delivery_gate as _gate
from tests.test_claude_bg_reconnect import GOOD, FakeLauncher

EVIDENCE = GOOD["delivery_evidence"]


def blocked_payload(**overrides):
    value = {
        "event": "delivered",
        "status": "blocked",
        "report_kind": "blocked_preflight_only",
        "implementation_complete": False,
        "delivery_evidence": dict(EVIDENCE),
    }
    value.update(overrides)
    return value


class SelfReportShapeGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = ProjectPaths(Path(temporary.name))
        self.store = ProviderActionStore(self.paths)
        patcher = mock.patch.object(ProviderActionStore, "_latest_generation", return_value=1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def deliver(self, payload, node="n1"):
        return self.store.record_worker_delivery("run-1", node, "developer", payload, 1)

    def archived(self):
        root = self.paths.vibe / "provider-actions" / "deliveries"
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_file())

    def test_blocked_self_report_with_delivered_event_is_rejected_naming_fields(self):
        with self.assertRaises(ValueError) as caught:
            self.deliver(blocked_payload())
        message = str(caught.exception)
        for field in ("status", "report_kind", "implementation_complete"):
            self.assertIn(field, message)
        # Field names only: the worker's own values never come back.
        self.assertNotIn("blocked_preflight_only", message)
        self.assertEqual(self.archived(), [])

    def test_blocked_self_report_with_complete_event_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.deliver(blocked_payload(event="complete"))
        self.assertIn("report_kind", str(caught.exception))
        self.assertEqual(self.archived(), [])

    def test_blocked_engineering_event_is_recorded(self):
        outcome = self.deliver(blocked_payload(event="blocked_engineering"))
        self.assertTrue(outcome["recorded"])
        self.assertEqual(len(self.archived()), 1)

    def test_issue_complete_false_is_rejected_too(self):
        payload = blocked_payload()
        del payload["implementation_complete"]
        payload["issue_complete"] = False
        with self.assertRaises(ValueError) as caught:
            self.deliver(payload)
        self.assertIn("issue_complete", str(caught.exception))
        self.assertEqual(self.archived(), [])

    def test_normal_delivery_without_blocked_fields_is_unchanged(self):
        self.assertTrue(self.deliver({"delivery_evidence": dict(EVIDENCE)})["recorded"])
        self.assertTrue(self.deliver({"event": "complete", "delivery_evidence": dict(EVIDENCE)})["recorded"])

    def test_corrupt_archive_reads_as_none(self):
        root = self.paths.vibe / "provider-actions" / "deliveries"
        root.mkdir(parents=True)
        (root / "run-1-n1-developer-g1.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.store.archived_payload("run-1", "n1", "developer", 1))

    def test_oversized_archive_reads_as_none(self):
        root = self.paths.vibe / "provider-actions" / "deliveries"
        root.mkdir(parents=True)
        (root / "run-1-n1-developer-g1.json").write_text(
            json_module.dumps({"payload": {"x": "y" * 100000}}), encoding="utf-8")
        self.assertIsNone(self.store.archived_payload("run-1", "n1", "developer", 1))

    def test_values_that_are_not_explicitly_false_do_not_trigger_the_gate(self):
        payload = {
            "event": "delivered",
            "status": "done",
            "report_kind": "final_report",
            "implementation_complete": 0,
            "delivery_evidence": dict(EVIDENCE),
        }
        self.assertTrue(self.deliver(payload)["recorded"])


class BlockedEngineeringMonitorTests(unittest.TestCase):
    tearDown = _gate.BackgroundDeliveryGateTests.tearDown
    _engine_plan = _gate.BackgroundDeliveryGateTests._engine_plan
    monitor = _gate.BackgroundDeliveryGateTests.monitor
    generation = _gate.BackgroundDeliveryGateTests.generation

    def setUp(self):
        _gate.BackgroundDeliveryGateTests.setUp(self)

    def runner(self):
        from vibe_guide.runners.claude_bg import ClaudeBgRunner
        return ClaudeBgRunner(lambda c, w: FakeLauncher("working"), paths=self.paths)

    def start(self):
        return self.monitor().start(self.record, self.runner())

    def events(self, run_id, name):
        return [record for record in load_events(self.paths, run_id)
                if record["event"] == name]

    def test_blocked_engineering_report_parks_the_node_without_reviewer(self):
        snapshot = self.start()
        outcome = ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg", "developer",
            blocked_payload(event="blocked_engineering"), self.generation(snapshot))
        self.assertTrue(outcome["recorded"])

        snapshot = self.monitor().resume(snapshot.run_id, self.runner())

        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "blocked_unknown", node.get("reason"))
        self.assertEqual(node.get("reason_class"), "engineering_blocked")
        # The block must not masquerade as an auto-retry: no retry marker, no
        # live session, no reviewer was ever dispatched for this report.
        self.assertFalse(node.get("retryable_action"))
        self.assertIsNone(node.get("active_task"))
        self.assertEqual(self.events(snapshot.run_id, "blocked_engineering") and
                         len(self.events(snapshot.run_id, "blocked_engineering")), 1)
        self.assertEqual(self.events(snapshot.run_id, "delivered"), [])
        self.assertNotIn(node["status"], {"review", "delivered", "accepted"})

    def test_blocked_fields_only_in_the_archive_are_parked_not_delivered(self):
        # Runner projections (RESULT_EVENT_KEYS) hide the blocked fields from
        # event data; the monitor must read the archived payload, where the
        # worker's full report lives, before judging the delivery.
        from vibe_guide.contracts import RunEvent

        snapshot = self.start()
        handle_id = snapshot.handles["bg"]
        active = snapshot.nodes["bg"]["active_task"]
        generation = active.get("generation")
        archive = (self.paths.vibe / "provider-actions" / "deliveries" /
                   "{}-bg-developer-g{}.json".format(snapshot.run_id, generation))
        archive.parent.mkdir(parents=True, exist_ok=True)
        report_payload = blocked_payload()
        report_payload["event"] = "delivered"
        archive.write_text(__import__("json").dumps({
            "run_id": snapshot.run_id, "issue_id": "bg", "role": "developer",
            "generation": generation, "payload": report_payload,
            "reported_at": 0, "consumed_by": None}), encoding="utf-8")
        # The projected event data carries only the delivery vocabulary.
        data = {"delivery_evidence": report_payload["delivery_evidence"]}
        data.update({key: active.get(key) for key in
                     ("role", "task_id", "handle_id", "generation")})
        data["node_id"] = "bg"

        self.monitor()._apply_event(snapshot, "bg", handle_id, RunEvent("delivered", data),
                                    self.runner())

        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "blocked_unknown", node.get("reason"))
        self.assertEqual(node.get("reason_class"), "engineering_blocked")

    def test_runner_vocabulary_gap_still_parks_the_blocked_report(self):
        # #335 visible-path defense: the provider runner does not know the
        # blocked_engineering event name, so the consumed archive surfaces as
        # visibility_unknown("provider event is unsupported"); the monitor
        # must recover the report from the archive, not lose it to a generic
        # block.
        from vibe_guide.contracts import RunEvent

        snapshot = self.start()
        handle_id = snapshot.handles["bg"]
        active = snapshot.nodes["bg"]["active_task"]
        generation = active.get("generation")
        archive = (self.paths.vibe / "provider-actions" / "deliveries" /
                   "{}-bg-developer-g{}.json".format(snapshot.run_id, generation))
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_text(__import__("json").dumps({
            "run_id": snapshot.run_id, "issue_id": "bg", "role": "developer",
            "generation": generation,
            "payload": blocked_payload(event="blocked_engineering"),
            "reported_at": 0, "consumed_by": "action-1"}), encoding="utf-8")
        data = {"reason": "provider event is unsupported"}
        data.update({key: active.get(key) for key in
                     ("role", "task_id", "handle_id", "generation")})
        data["node_id"] = "bg"

        self.monitor()._apply_event(
            snapshot, "bg", handle_id, RunEvent("visibility_unknown", data), self.runner())

        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "blocked_unknown", node.get("reason"))
        self.assertEqual(node.get("reason_class"), "engineering_blocked")
        self.assertFalse(node.get("retryable_action"))

    def test_blocked_engineering_replays_after_crash_before_save(self):
        # Crash window: the event is appended but the snapshot save misses
        # it; the replay twin must rebuild the same park instead of
        # degrading to "unapplied event needs manual reconciliation".
        from vibe_guide.state import load_snapshot

        snapshot = self.start()
        crashed = load_snapshot(self.paths, snapshot.run_id)
        self.monitor()._record(
            crashed, "blocked_engineering",
            {"run_id": crashed.run_id, "node_id": "bg", "role": "developer",
             "generation": 1, "reason_class": "engineering_blocked",
             "reason_code": "blocked_self_report"},
            crashed.nodes["bg"]["active_task"])

        snapshot = self.monitor().resume(snapshot.run_id, self.runner())
        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "blocked_unknown", node.get("reason"))
        self.assertEqual(node.get("reason_class"), "engineering_blocked")
        self.assertEqual(node.get("reason_code"), "blocked_self_report")

    def test_blocked_fields_on_a_delivered_event_are_parked_not_delivered(self):
        # Defense in depth: a payload that predates the shape gate (or reached
        # the monitor past worker-deliver validation) with blocked fields on a
        # delivery event must be parked, never judged as a delivery.  The
        # unit boundary is the monitor itself: real runners project payload
        # fields into event data, so drive _apply_event with the full payload
        # shape a runner would forward.
        from vibe_guide.contracts import RunEvent

        snapshot = self.start()
        handle_id = snapshot.handles["bg"]
        active = snapshot.nodes["bg"]["active_task"]
        data = blocked_payload()
        # The runner's claims always win over payload fields; mirror that.
        data.update({key: active.get(key) for key in
                     ("role", "task_id", "handle_id", "generation")})
        data["node_id"] = "bg"
        event = RunEvent("delivered", data)

        self.monitor()._apply_event(snapshot, "bg", handle_id, event, self.runner())

        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "blocked_unknown", node.get("reason"))
        self.assertEqual(node.get("reason_class"), "engineering_blocked")
        self.assertIsNone(node.get("active_task"))
        self.assertFalse(node.get("retryable_action"))
class BlockedSelfReportEvidenceTests(unittest.TestCase):
    """evaluate_delivery_evidence lets a blocked self-state dominate (#335)."""

    def node(self):
        return {"status": "DELIVERED"}

    def binding(self):
        return {"task_id": "task-1", "host": "local", "worktree": ".worktrees/n",
                "branch": "node/n", "cursor": "c"}

    def artifacts(self):
        return dict(EVIDENCE)

    def evaluate(self, **kwargs):
        from vibe_guide.evidence import evaluate_delivery_evidence
        return evaluate_delivery_evidence(self.node(), self.binding(), self.artifacts(), **kwargs)

    def test_complete_evidence_with_blocked_self_report_is_blocked_unknown(self):
        gate = self.evaluate(self_report=blocked_payload())
        self.assertFalse(gate.complete)
        self.assertEqual(gate.status, "blocked_unknown")
        self.assertEqual(gate.classification, "blocked_unknown")
        self.assertIsNone(gate.code)

    def test_blocked_self_report_needs_no_other_evidence_to_dominate(self):
        gate = self.evaluate(self_report={"status": "blocked"})
        self.assertFalse(gate.complete)
        self.assertEqual(gate.classification, "blocked_unknown")

    def test_absent_self_report_keeps_the_complete_verdict(self):
        gate = self.evaluate()
        self.assertTrue(gate.complete)

    def test_unblocked_self_report_keeps_the_complete_verdict(self):
        gate = self.evaluate(self_report={"status": "done", "report_kind": "final"})
        self.assertTrue(gate.complete)
