"""F3 (integration review): a decision-less blocked_design registers a decision.

The producers (integration scope expansion needing PM review, reviewer
out-of-contract finding) record a fixed enum cause; the monitor registers a
vibe-authored decision in the mailbox so the PM sees a question instead of a
silent lifecycle stall.  Provider finding text never reaches durable state.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.decision_boundary import NotificationBridge
from vibe_guide.decisions import DecisionMailbox
from vibe_guide.models import INTEGRATION_REVIEW_NODE_ID
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events, load_snapshot, save_snapshot
from vibe_guide.supervisor import _mailbox_state
from tests import test_auto_scope_files as fx

SENTINEL = "SENTINEL-PROVIDER-FINDING-TEXT-9137"


class _Base(unittest.TestCase):
    setUp = fx.MonitorAutoScopeTests.setUp
    tearDown = fx.MonitorAutoScopeTests.tearDown
    _build_complex_plan = fx.MonitorAutoScopeTests._build_complex_plan
    _drive = fx.MonitorAutoScopeTests._drive
    _run_finding = fx.MonitorAutoScopeTests._run_finding

    def pending_for(self, run_id, node_id):
        return [
            item for item in DecisionMailbox(self.paths).list_pending(run_id)
            if item.get("node_id") == node_id
        ]

    def undecided(self, **extra):
        node = fx._node("n1")
        plan = fx.Plan("plan-1", 1, "docs/prd.md", ["n1"], "draft")
        card = build_authorization_card(plan, [node], self.capabilities)
        monitor = fx.Monitor(self.paths, plan, [node])
        runner = FakeRunner()
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        current = snapshot.nodes["n1"]
        current.update({"status": "blocked_design", "reason": "design decision required",
                        "active_role": None, "active_task": None, "retryable_action": None})
        current.update(extra)
        snapshot.handles.pop("n1", None)
        save_snapshot(self.paths, snapshot)
        return monitor, runner, snapshot

    def decision_events(self, run_id):
        return [
            e for e in load_events(self.paths, run_id)
            if e["event"] in {"blocked_unknown", "decision_raised", "blocked_design"}
        ]


class ProducerRegistersDecisionTests(_Base):
    def test_integration_scope_review_registers_decision_and_supervisor_reads_it(self):
        plan, nodes, card, monitor, delivered = self._build_complex_plan([fx._node("n1")])
        runner = fx._BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (INTEGRATION_REVIEW_NODE_ID, "developer"): [delivered],
            (INTEGRATION_REVIEW_NODE_ID, "reviewer"): [(
                "review_finding",
                {
                    "finding": SENTINEL,
                    "severity": "P1",
                    "target_node_id": "n1",
                    "scope_expansion": fx._scope_claim(["vibe_guide/protocols/__init__.py"]),
                },
            )],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)
        current = snapshot.nodes[INTEGRATION_REVIEW_NODE_ID]
        self.assertEqual(current["status"], "blocked_design")
        self.assertEqual(current["blocked_design_cause"], "integration_scope_review")
        self.assertTrue(current["decision_ids"])
        self.assertEqual(current["owner_layer"], "product_acceptance")
        self.assertTrue(current["user_interrupt"])
        pending = self.pending_for(snapshot.run_id, INTEGRATION_REVIEW_NODE_ID)
        self.assertEqual([p["decision_id"] for p in pending], current["decision_ids"])
        self.assertIn("整合审查要求把文件扩进已验收节点的范围", pending[0]["question"])
        self.assertEqual(pending[0]["evidence"]["target_node_id"], "n1")
        raw = b"".join(
            path.read_bytes() for path in self.paths.root.rglob("*") if path.is_file()
            and ".vibe" in path.parts
        )
        self.assertNotIn(SENTINEL.encode(), raw)
        mailbox = _mailbox_state(self.paths, snapshot.run_id, {})
        listed = [d["decision_id"] for d in mailbox["decisions"]]
        self.assertIn(current["decision_ids"][0], listed)
        self.assertEqual(mailbox["state"], "decision")

    def test_out_of_contract_finding_registers_decision_without_finding_text(self):
        _monitor, _runner, snapshot = self._out_of_contract()
        current = snapshot.nodes["n1"]
        self.assertEqual(current["status"], "blocked_design")
        self.assertEqual(current["blocked_design_cause"], "out_of_contract_finding")
        pending = self.pending_for(snapshot.run_id, "n1")
        self.assertEqual([p["decision_id"] for p in pending], current["decision_ids"])
        self.assertIn("审查发现节点合同之外的问题", pending[0]["question"])
        self.assertEqual(pending[0]["evidence"]["role"], "reviewer")
        for path in (self.paths.vibe).rglob("*"):
            if path.is_file():
                self.assertNotIn(SENTINEL.encode(), path.read_bytes(), str(path))
        mailbox = _mailbox_state(self.paths, snapshot.run_id, {})
        self.assertIn(current["decision_ids"][0], [d["decision_id"] for d in mailbox["decisions"]])

    def _out_of_contract(self):
        """Unlisted-file consistency finding: no unique answer -> blocked_design."""
        node = fx._node("n1")
        plan = fx.Plan(
            "plan-1", 1, "docs/prd.md", ["n1"], "draft",
            decisions=[dict(fx.DECISION, question="canonical name")],
        )
        card = build_authorization_card(plan, [node], self.capabilities)
        monitor = fx.Monitor(self.paths, plan, [node])
        self.monitor = monitor
        return self._drive(
            monitor, card, [node], ["n1.py", "vibe_guide/protocols/__init__.py"],
            finding_overrides={"finding": SENTINEL},
        )


class LifecycleFallbackTests(_Base):
    def assert_lifecycle(self, snapshot):
        current = snapshot.nodes["n1"]
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertEqual(current["recovery_action"], "raise_missing_decision")
        self.assertEqual(current["owner_layer"], "supervisor_lifecycle")
        self.assertIs(current["user_interrupt"], False)
        self.assertEqual(self.pending_for(snapshot.run_id, "n1"), [])
        self.assertFalse(current.get("decision_ids"))

    def test_legacy_without_cause_goes_lifecycle_on_tick_without_decision(self):
        monitor, runner, snapshot = self.undecided()
        self.assert_lifecycle(monitor.tick(snapshot.run_id, runner))
        self.assert_lifecycle(monitor.tick(snapshot.run_id, runner))

    def test_legacy_without_cause_goes_lifecycle_on_resume_without_decision(self):
        monitor, runner, snapshot = self.undecided()
        with patch("vibe_guide.monitor.require_entry", return_value=None):
            self.assert_lifecycle(monitor.resume(snapshot.run_id, runner))

    def test_unknown_cause_value_also_goes_lifecycle(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="something_else")
        self.assert_lifecycle(monitor.tick(snapshot.run_id, runner))

    def test_ensure_returns_false_for_missing_cause_and_writes_nothing(self):
        monitor, runner, snapshot = self.undecided()
        live = load_snapshot(self.paths, snapshot.run_id)
        before = len(load_events(self.paths, snapshot.run_id))
        self.assertIs(monitor._ensure_blocked_design_decision(live, "n1"), False)
        self.assertEqual(len(load_events(self.paths, snapshot.run_id)), before)
        self.assertEqual(self.pending_for(snapshot.run_id, "n1"), [])

    def test_option_impacts_do_not_promise_automatic_execution(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        monitor.tick(snapshot.run_id, runner)
        options = {o["id"]: o["impact"] for o in self.pending_for(snapshot.run_id, "n1")[0]["options"]}
        self.assertEqual(options["accept_as_contract_change"], "选此项后由监工变更合同并安排该节点返工")
        for impact in options.values():
            self.assertTrue(impact.startswith("选此项后"), impact)

    def test_reblock_for_same_cause_after_answer_gets_a_new_pending_decision(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="integration_scope_review")
        first = monitor.tick(snapshot.run_id, runner).nodes["n1"]["decision_ids"][0]
        DecisionMailbox(self.paths).answer(first, "keep_scope")
        self.assertEqual(self.pending_for(snapshot.run_id, "n1"), [])
        self.reblock(snapshot)
        second = monitor.tick(snapshot.run_id, runner).nodes["n1"]
        self.assertEqual(second["status"], "blocked_design")
        self.assertNotEqual(second["decision_ids"], [first])
        pending = self.pending_for(snapshot.run_id, "n1")
        self.assertEqual([p["decision_id"] for p in pending], second["decision_ids"])
        self.assertIn("（第 2 次）", pending[0]["question"])
        state = _mailbox_state(self.paths, snapshot.run_id, {})
        self.assertEqual(state["state"], "decision")
        self.assertEqual([d["decision_id"] for d in state["decisions"]], second["decision_ids"])

    def reblock(self, snapshot):
        again = load_snapshot(self.paths, snapshot.run_id)
        again.nodes["n1"].update({"status": "blocked_design", "decision_ids": [],
                                  "owner_layer": None, "recovery_action": None})
        save_snapshot(self.paths, again)

    def test_pending_request_is_idempotent_across_ensure_calls(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        live = load_snapshot(self.paths, snapshot.run_id)
        self.assertTrue(monitor._ensure_blocked_design_decision(live, "n1"))
        ids = list(live.nodes["n1"]["decision_ids"])
        self.assertTrue(monitor._ensure_blocked_design_decision(live, "n1"))
        self.assertEqual(live.nodes["n1"]["decision_ids"], ids)
        self.assertEqual(len(self.pending_for(snapshot.run_id, "n1")), 1)

    def test_answered_suffix_bound_falls_back_to_lifecycle(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        mailbox = DecisionMailbox(self.paths)
        for _ in range(20):
            live = load_snapshot(self.paths, snapshot.run_id)
            self.assertTrue(monitor._ensure_blocked_design_decision(live, "n1"))
            mailbox.answer(live.nodes["n1"]["decision_ids"][0], "defer_finding")
        live = load_snapshot(self.paths, snapshot.run_id)
        self.assertIs(monitor._ensure_blocked_design_decision(live, "n1"), False)
        self.assertEqual(self.pending_for(snapshot.run_id, "n1"), [])

    def test_result_read_error_counts_as_failure(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        live = load_snapshot(self.paths, snapshot.run_id)
        with patch("vibe_guide.monitor.DecisionMailbox.result", side_effect=ValueError("bad")):
            self.assertIs(monitor._ensure_blocked_design_decision(live, "n1"), False)

    def spy_notify(self, side_effect=None):
        return patch.object(
            NotificationBridge, "notify", autospec=True,
            side_effect=side_effect or (lambda bridge, record: {"status": "accepted"}),
        )

    def test_one_notify_per_new_decision_not_per_suffix_attempt(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="integration_scope_review")
        with self.spy_notify() as spy:
            first = monitor.tick(snapshot.run_id, runner).nodes["n1"]["decision_ids"][0]
            self.assertEqual([c.args[1]["decision_id"] for c in spy.call_args_list], [first])
            DecisionMailbox(self.paths).answer(first, "keep_scope")
            self.reblock(snapshot)
            second = monitor.tick(snapshot.run_id, runner).nodes["n1"]["decision_ids"][0]
        self.assertNotEqual(first, second)
        self.assertEqual([c.args[1]["decision_id"] for c in spy.call_args_list], [first, second])

    def test_exhausted_cap_never_notifies_and_adds_no_events(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        mailbox = DecisionMailbox(self.paths)
        for _ in range(20):
            live = load_snapshot(self.paths, snapshot.run_id)
            self.assertTrue(monitor._ensure_blocked_design_decision(live, "n1"))
            save_snapshot(self.paths, live)
            mailbox.answer(live.nodes["n1"]["decision_ids"][0], "defer_finding")
        self.reblock(snapshot)
        with self.spy_notify() as spy:
            monitor.tick(snapshot.run_id, runner)
            count = len(self.decision_events(snapshot.run_id))
            monitor.tick(snapshot.run_id, runner)
            monitor.tick(snapshot.run_id, runner)
        self.assertEqual(spy.call_count, 0)
        self.assertEqual(len(self.decision_events(snapshot.run_id)), count)

    def test_notify_exception_still_adopts_decision_with_unknown_status(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        with self.spy_notify(side_effect=RuntimeError("bridge hung")):
            current = monitor.tick(snapshot.run_id, runner).nodes["n1"]
        self.assertEqual(current["status"], "blocked_design")
        self.assertEqual(len(current["decision_ids"]), 1)
        events = [e for e in load_events(self.paths, snapshot.run_id)
                  if e["event"] == "decision_raised"
                  and e["data"].get("source") == "monitor_blocked_design"]
        self.assertEqual(events[0]["data"]["notification_status"], "unknown")

    def test_decision_raised_event_carries_notification_status_and_reconciled_flag(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        monitor.tick(snapshot.run_id, runner)
        events = [e for e in load_events(self.paths, snapshot.run_id)
                  if e["event"] == "decision_raised"
                  and e["data"].get("source") == "monitor_blocked_design"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["notification_status"], "disabled")
        self.assertIs(events[0]["data"]["old_task_reconciled"], True)

    def test_mailbox_failure_falls_back_then_recovers_without_event_spam(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="out_of_contract_finding")
        with patch("vibe_guide.monitor.DecisionMailbox.raise_decision", side_effect=OSError("disk")):
            first = monitor.tick(snapshot.run_id, runner)
            count = len(self.decision_events(snapshot.run_id))
            second = monitor.tick(snapshot.run_id, runner)
            third = monitor.tick(snapshot.run_id, runner)
        for item in (first, second, third):
            self.assertEqual(item.nodes["n1"]["status"], "blocked_unknown")
            self.assertEqual(item.nodes["n1"]["recovery_action"], "raise_missing_decision")
        self.assertEqual(len(self.decision_events(snapshot.run_id)), count)
        healed = monitor.tick(snapshot.run_id, runner)
        current = healed.nodes["n1"]
        self.assertEqual(current["status"], "blocked_design")
        self.assertEqual(current["owner_layer"], "product_acceptance")
        self.assertIsNone(current["recovery_action"])
        self.assertEqual(len(current["decision_ids"]), 1)
        self.assertEqual(len(self.pending_for(snapshot.run_id, "n1")), 1)
        self.assertEqual(
            len(self.decision_events(snapshot.run_id)), count + 1
        )


class ReplayTests(_Base):
    def test_replay_matches_live(self):
        monitor, runner, snapshot = self.undecided(blocked_design_cause="integration_scope_review", old_task_reconciled=True)
        stale = load_snapshot(self.paths, snapshot.run_id)
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._route_undecided_blocked_design(live)
        replay = load_snapshot(self.paths, snapshot.run_id)
        replay.event_sequence = stale.event_sequence
        monitor._reconcile_unapplied_events(replay)
        for key in ("status", "decision_ids", "owner_layer", "recovery_action",
                    "user_interrupt", "quarantine", "old_task_reconciled"):
            self.assertEqual(replay.nodes["n1"].get(key), live.nodes["n1"].get(key), key)
        self.assertEqual(live.nodes["n1"]["status"], "blocked_design")

    def test_replay_restores_cause_from_producer_event(self):
        node = fx._node("n1")
        plan = fx.Plan(
            "plan-1", 1, "docs/prd.md", ["n1"], "draft",
            decisions=[dict(fx.DECISION, question="canonical name")],
        )
        card = build_authorization_card(plan, [node], self.capabilities)
        monitor = fx.Monitor(self.paths, plan, [node])
        runner = FakeRunner(events={("n1", "developer"): [("complete", {"evidence": "delivery"})]})
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        binding = runner.start_calls[-1]["consistency_binding"]
        runner.events[("n1", "reviewer")] = [("review_finding", {
            "finding": SENTINEL, "in_contract": False,
            "consistency": fx._inconsistency(["n1.py", "vibe_guide/protocols/__init__.py"], binding),
        })]
        with patch("vibe_guide.monitor.save_snapshot", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                monitor.tick(snapshot.run_id, runner)
        replay = load_snapshot(self.paths, snapshot.run_id)
        monitor._reconcile_unapplied_events(replay)
        self.assertEqual(replay.nodes["n1"]["blocked_design_cause"], "out_of_contract_finding")
        self.assertEqual(replay.nodes["n1"]["status"], "blocked_design")
        self.assertEqual(replay.nodes["n1"]["decision_ids"], [
            p["decision_id"] for p in self.pending_for(snapshot.run_id, "n1")])
        self.assertEqual(replay.nodes["n1"]["owner_layer"], "product_acceptance")


if __name__ == "__main__":
    unittest.main()
