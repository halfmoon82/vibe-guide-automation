"""Answered decisions move their blocked_design node on the next tick/resume."""
import unittest
from unittest.mock import patch

from vibe_guide.authorization import authorize
from vibe_guide.decisions import DecisionMailbox
from vibe_guide.models import INTEGRATION_REVIEW_NODE_ID
from vibe_guide.state import load_events, load_snapshot, save_snapshot
from tests import test_auto_scope_files as fx
from tests.test_integration_fix_monitor_routing import _Base

IR = INTEGRATION_REVIEW_NODE_ID
EXTRA = "vibe_guide/protocols/__init__.py"


class AnsweredDecisionResumeTests(_Base):
    def blocked_integration(self):
        plan, nodes, card, monitor, delivered = self._build_complex_plan([fx._node("n1")])
        runner = fx._BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (IR, "developer"): [delivered],
            (IR, "reviewer"): [("review_finding", {
                "finding": "x", "severity": "P1", "target_node_id": "n1",
                "scope_expansion": fx._scope_claim([EXTRA]),
            })],
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(4):
            snapshot = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(snapshot.nodes[IR]["status"], "blocked_design")
        return monitor, runner, snapshot

    def answered_events(self, run_id):
        return [e for e in load_events(self.paths, run_id) if e["event"] == "decision_answer_applied"]

    def test_approve_expansion_reworks_target_and_logs_answer(self):
        monitor, runner, snapshot = self.blocked_integration()
        run_id = snapshot.run_id
        self.assertEqual(snapshot.nodes[IR]["blocked_design_files"], [EXTRA])
        self.answer_pending(snapshot.nodes[IR]["decision_ids"][0], "approve_scope_expansion")
        live = load_snapshot(self.paths, run_id)
        monitor._consume_answered_decisions(live)
        self.assertIn(EXTRA, live.nodes["n1"]["scope_expansions"])
        self.assertEqual(live.nodes["n1"]["status"], "rework")
        self.assertEqual(live.nodes[IR]["status"], "rework")
        self.assertNotIn("decision_ids", live.nodes[IR])
        events = self.answered_events(run_id)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["data"]["decision_answers"][0]["choice"], "approve_scope_expansion")
        # restart: replay of the event alone reproduces the state
        stale = load_snapshot(self.paths, run_id)
        replay = load_snapshot(self.paths, run_id)
        replay.event_sequence = stale.event_sequence
        replay.nodes[IR].update(snapshot.nodes[IR])
        monitor._reconcile_unapplied_events(replay)
        for node in ("n1", IR):
            for key in ("status", "scope_expansions", "decision_ids"):
                self.assertEqual(replay.nodes[node].get(key), live.nodes[node].get(key), (node, key))

    def test_tick_consumes_and_does_not_regress(self):
        monitor, runner, snapshot = self.blocked_integration()
        self.answer_pending(snapshot.nodes[IR]["decision_ids"][0], "approve_scope_expansion")
        after = monitor.tick(snapshot.run_id, runner)
        self.assertNotEqual(after.nodes[IR]["status"], "blocked_design")
        self.assertIn(EXTRA, after.nodes["n1"]["scope_expansions"])
        again = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(len(self.answered_events(snapshot.run_id)), 1)
        self.assertIn(EXTRA, again.nodes["n1"]["scope_expansions"])

    def test_keep_scope_reworks_integration_review_without_expansion(self):
        monitor, runner, snapshot = self.blocked_integration()
        self.answer_pending(snapshot.nodes[IR]["decision_ids"][0], "keep_scope")
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._consume_answered_decisions(live)
        self.assertEqual(live.nodes[IR]["status"], "rework")
        self.assertEqual(live.nodes[IR]["retryable_action"]["role"], "reviewer")
        self.assertFalse(live.nodes["n1"].get("scope_expansions"))
        self.assertEqual(live.nodes["n1"]["status"], "accepted")

    def test_approve_expansion_never_admits_credential_or_git_paths(self):
        for forbidden in (".env", ".git/config", "deploy/id_rsa"):
            with self.subTest(path=forbidden):
                self.setUp()
                monitor, runner, snapshot = self.blocked_integration()
                live = load_snapshot(self.paths, snapshot.run_id)
                live.nodes[IR]["blocked_design_files"] = [forbidden]
                save_snapshot(self.paths, live)
                self.answer_pending(live.nodes[IR]["decision_ids"][0], "approve_scope_expansion")
                live = load_snapshot(self.paths, snapshot.run_id)
                monitor._consume_answered_decisions(live)
                self.assertEqual(live.nodes[IR]["status"], "blocked_unknown")
                self.assertNotIn(forbidden, live.nodes["n1"].get("scope_expansions") or [])
                self.assertEqual(self.answered_events(snapshot.run_id), [])

    def test_scope_decision_shows_the_pm_which_files(self):
        monitor, runner, snapshot = self.blocked_integration()
        record = DecisionMailbox(self.paths)._load_request(snapshot.nodes[IR]["decision_ids"][0])
        self.assertEqual(record["evidence"]["files"], [EXTRA])

    def test_keep_scope_answer_reaches_the_next_reviewer(self):
        monitor, runner, snapshot = self.blocked_integration()
        self.answer_pending(snapshot.nodes[IR]["decision_ids"][0], "keep_scope")
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._consume_answered_decisions(live)
        answers = live.nodes[IR]["contract_overrides"]["decision_answers"]
        self.assertEqual([item["choice"] for item in answers], ["keep_scope"])

    def undecided_with(self, cause):
        monitor, runner, snapshot = self.undecided(blocked_design_cause=cause)
        live = monitor.tick(snapshot.run_id, runner)
        return monitor, runner, live

    def test_out_of_contract_options(self):
        for choice, marker in (("accept_as_contract_change", "contract_change_accepted"),
                               ("defer_finding", None)):
            with self.subTest(choice=choice):
                self.setUp()
                monitor, runner, live = self.undecided_with("out_of_contract_finding")
                self.answer_pending(live.nodes["n1"]["decision_ids"][0], choice)
                live = load_snapshot(self.paths, live.run_id)
                monitor._consume_answered_decisions(live)
                node = live.nodes["n1"]
                self.assertNotEqual(node["status"], "blocked_design")
                # Whichever way it went, the next worker sees the answer itself.
                self.assertEqual(node["contract_overrides"]["decision_answers"][0]["choice"], choice)
                if marker:
                    self.assertTrue(node["contract_overrides"][marker])
                    self.assertEqual(node["status"], "rework")
                else:
                    self.assertTrue(node["deferred_findings"])
                    self.assertEqual(node["retryable_action"]["role"], "reviewer")

    def test_partial_answers_keep_blocked_design(self):
        monitor, runner, snapshot = self.undecided()
        mailbox = DecisionMailbox(self.paths)
        ids = []
        for question in ("q1?", "q2?"):
            ids.append(mailbox.raise_decision(
                run_id=snapshot.run_id, node_id="n1", question=question,
                options=["a", "b"])["decision_id"])
        live = load_snapshot(self.paths, snapshot.run_id)
        live.nodes["n1"]["decision_ids"] = ids
        save_snapshot(self.paths, live)
        self.answer_pending(ids[0], "a")
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._consume_answered_decisions(live)
        self.assertEqual(live.nodes["n1"]["status"], "blocked_design")
        self.assertEqual(self.answered_events(snapshot.run_id), [])
        self.answer_pending(ids[1], "b")
        monitor._consume_answered_decisions(live)
        self.assertEqual(live.nodes["n1"]["status"], "rework")
        self.assertEqual(len(live.nodes["n1"]["decision_answers"]), 2)

    def test_voided_answer_counts_as_pending_and_read_error_does_not_fail_round(self):
        monitor, runner, live = self.undecided_with("out_of_contract_finding")
        decision_id = live.nodes["n1"]["decision_ids"][0]
        self.answer_pending(decision_id, "defer_finding")
        with patch.object(DecisionMailbox, "result", side_effect=ValueError("digest mismatch")):
            snap = load_snapshot(self.paths, live.run_id)
            monitor._consume_answered_decisions(snap)
        self.assertEqual(snap.nodes["n1"]["status"], "blocked_design")

    def test_other_status_is_not_touched(self):
        monitor, runner, live = self.undecided_with("out_of_contract_finding")
        self.answer_pending(live.nodes["n1"]["decision_ids"][0], "defer_finding")
        snap = load_snapshot(self.paths, live.run_id)
        snap.nodes["n1"]["status"] = "blocked_unknown"
        monitor._consume_answered_decisions(snap)
        self.assertEqual(snap.nodes["n1"]["status"], "blocked_unknown")
        self.assertEqual(self.answered_events(live.run_id), [])

    def test_unmappable_answer_is_blocked_unknown_with_reason(self):
        monitor, runner, live = self.undecided_with("out_of_contract_finding")
        self.answer_pending(live.nodes["n1"]["decision_ids"][0], "defer_finding")
        snap = load_snapshot(self.paths, live.run_id)
        snap.nodes["n1"]["blocked_design_cause"] = "integration_scope_review"
        snap.nodes["n1"]["blocked_design_target"] = "n1"
        monitor._consume_answered_decisions(snap)
        self.assertEqual(snap.nodes["n1"]["status"], "blocked_unknown")
        self.assertTrue(snap.nodes["n1"]["reason"])

    def test_developer_raised_decision_resumes_original_developer(self):
        monitor, runner, snapshot = self.undecided()
        mailbox = DecisionMailbox(self.paths)
        record = mailbox.raise_decision(run_id=snapshot.run_id, node_id="n1",
                                        question="which?", options=["a", "b"])
        live = load_snapshot(self.paths, snapshot.run_id)
        live.nodes["n1"]["decision_ids"] = [record["decision_id"]]
        save_snapshot(self.paths, live)
        self.answer_pending(record["decision_id"], "b")
        live = load_snapshot(self.paths, snapshot.run_id)
        monitor._consume_answered_decisions(live)
        node = live.nodes["n1"]
        self.assertEqual(node["status"], "rework")
        self.assertEqual(node["retryable_action"]["role"], "developer")
        self.assertTrue(node["retryable_action"]["continuation"])
        self.assertEqual(node["contract_overrides"]["decision_answers"][0]["choice"], "b")


if __name__ == "__main__":
    unittest.main()
