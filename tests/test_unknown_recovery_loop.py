"""#404: a blocked_unknown must carry a structured dossier, and the same cause
parked twice with no progress in between must switch strategy, not resume again."""
import unittest

from vibe_guide import supervisor

NODE = "dev-a"


def node(**overrides):
    base = {
        "status": "blocked_unknown",
        "reason_class": "binding_unknown",
        "reason_code": "binding_not_closed",
        "error_type": "ValueError",
        "recovery_action": "reconcile_binding",
        "active_task": None,
        "evidence": [],
    }
    base.update(overrides)
    return base


def parked(sequence, node_id=NODE, reason_class="binding_unknown", reason_code="binding_not_closed",
           error_type="ValueError"):
    return {"sequence": sequence, "event": "blocked_unknown", "data": {
        "node_id": node_id, "reason_class": reason_class, "reason_code": reason_code,
        "error_type": error_type}}


def other(sequence, name, node_id=NODE):
    return {"sequence": sequence, "event": name, "data": {"node_id": node_id}}


def assess(events, nodes=None, tasks=None):
    return supervisor.unknown_loop_assessment(nodes or {NODE: node()}, tasks or {}, events)


class DossierTests(unittest.TestCase):
    def test_every_unknown_names_gap_evidence_cause_owner_action_and_exit(self):
        entry = assess([parked(7)])[NODE]
        for key in ("gap", "evidence_ref", "cause_fingerprint", "owner", "next_action", "verify"):
            self.assertTrue(entry.get(key), key)
        self.assertEqual(entry["evidence_ref"], "events.jsonl#7")
        self.assertEqual(entry["repeats"], 1)
        self.assertFalse(entry["switch_required"])

    def test_non_blocked_unknown_nodes_get_no_dossier(self):
        self.assertEqual(assess([parked(1)], {NODE: node(status="running")}), {})

    def test_no_park_event_still_yields_dossier_with_unknown_evidence(self):
        entry = assess([])[NODE]
        self.assertEqual(entry["repeats"], 0)
        self.assertEqual(entry["evidence_ref"], "state.json#nodes/" + NODE)


class RepeatTests(unittest.TestCase):
    def test_second_identical_park_requires_a_strategy_switch(self):
        entry = assess([parked(1), parked(2)])[NODE]
        self.assertEqual(entry["repeats"], 2)
        self.assertTrue(entry["switch_required"])
        self.assertNotEqual(entry["next_action"], "reconcile_binding")

    def test_resume_bookkeeping_between_parks_is_not_progress(self):
        events = [parked(1), other(2, "retry_scheduled"), other(3, "provider_observation"), parked(4)]
        self.assertTrue(assess(events)[NODE]["switch_required"])

    def test_real_progress_between_parks_resets_the_count(self):
        events = [parked(1), other(2, "acceptance_recovered"), parked(3)]
        entry = assess(events)[NODE]
        self.assertEqual(entry["repeats"], 1)
        self.assertFalse(entry["switch_required"])

    def test_different_cause_does_not_chain(self):
        events = [parked(1, reason_class="retryable"), parked(2)]
        self.assertEqual(assess(events)[NODE]["repeats"], 1)

    def test_other_nodes_events_do_not_break_or_extend_the_chain(self):
        events = [parked(1), other(2, "acceptance_recovered", node_id="other"), parked(3, node_id="other"), parked(4)]
        self.assertEqual(assess(events)[NODE]["repeats"], 2)

    def test_third_round_moves_further_down_the_ladder_not_back(self):
        second = assess([parked(1), parked(2)])[NODE]["next_action"]
        fourth = assess([parked(i) for i in range(1, 5)])[NODE]["next_action"]
        self.assertNotEqual(second, fourth)

    def test_same_cause_gives_same_fingerprint_across_rounds(self):
        one = assess([parked(1)])[NODE]["cause_fingerprint"]
        two = assess([parked(1), parked(2)])[NODE]["cause_fingerprint"]
        self.assertEqual(one, two)


class LegitimateWaitTests(unittest.TestCase):
    def test_upstream_wait_is_a_wait_not_a_loop(self):
        waiting = node(reason_class="capacity_wait", recovery_action="requeue_same_task")
        events = [parked(i, reason_class="capacity_wait") for i in (1, 2, 3)]
        entry = assess(events, {NODE: waiting})[NODE]
        self.assertFalse(entry["switch_required"])
        self.assertEqual(entry["owner"], "upstream")

    def test_external_decision_is_a_product_boundary_not_a_loop(self):
        gated = node(reason_class="external_decision", recovery_action="await_external_decision")
        events = [parked(i, reason_class="external_decision") for i in (1, 2, 3)]
        entry = assess(events, {NODE: gated})[NODE]
        self.assertFalse(entry["switch_required"])
        self.assertEqual(entry["owner"], "product_manager")


class IndependentNodesTests(unittest.TestCase):
    def test_looping_node_does_not_flag_its_healthy_neighbour(self):
        nodes = {NODE: node(), "dev-b": node(status="running")}
        result = assess([parked(1), parked(2)], nodes)
        self.assertEqual(set(result), {NODE})


class BeatTextTests(unittest.TestCase):
    def test_switch_required_is_rendered_with_the_new_action(self):
        entry = assess([parked(1), parked(2)])[NODE]
        text = supervisor.render_unknown_loop_text({NODE: entry}, {"accepted": 2, "total": 7})
        self.assertIn(NODE, text)
        self.assertIn(entry["next_action"], text)
        self.assertIn("业务验收 2/7", text)
        self.assertIn("工程恢复", text)

    def test_single_park_renders_dossier_without_switch_demand(self):
        entry = assess([parked(1)])[NODE]
        text = supervisor.render_unknown_loop_text({NODE: entry}, {"accepted": 0, "total": 1})
        self.assertNotIn("必须切换", text)

    def test_nothing_unknown_renders_nothing(self):
        self.assertEqual(supervisor.render_unknown_loop_text({}, {"accepted": 1, "total": 1}), "")


class ReportWiringTests(unittest.TestCase):
    def test_render_stall_beat_text_includes_loop_lines_from_report(self):
        entry = assess([parked(1), parked(2)])[NODE]
        report = {"status": "ok", "run_readable": True, "nodes": {}, "mailbox_waiting": {},
                  "unknown_loops": {NODE: entry}, "progress": {"accepted": 2, "total": 7}}
        text = supervisor.render_stall_beat_text(report, "run-x")
        self.assertIn("必须切换策略", text)
        self.assertIn("业务验收 2/7", text)

    def test_first_park_adds_no_beat_text(self):
        entry = assess([parked(1)])[NODE]
        report = {"status": "ok", "run_readable": True, "nodes": {}, "mailbox_waiting": {},
                  "unknown_loops": {NODE: entry}, "progress": {"accepted": 0, "total": 2}}
        self.assertEqual(supervisor.render_stall_beat_text(report, "run-x"), "")

    def test_report_without_loops_is_unchanged(self):
        report = {"status": "ok", "run_readable": True, "nodes": {}, "mailbox_waiting": {}}
        self.assertEqual(supervisor.render_stall_beat_text(report, "run-x"), "")


if __name__ == "__main__":
    unittest.main()
