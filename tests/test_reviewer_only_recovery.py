"""Cluster D: reviewer-only nodes (integration review) are first-class in recovery.

Acceptance examples 1, 3, 4, 5, 6 of the reviewer-only-recovery contract, plus
the nine recovery sites that used to load the developer binding
unconditionally (each one changed, or pinned here as not applicable).
Example 2 (authorization refresh with an in-flight successor) lives in
tests/test_authorization_proof_stable_fields.py.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_auto_scope_files as fx
from vibe_guide import monitor as monitor_module
from vibe_guide.authorization import authorize
from vibe_guide.models import INTEGRATION_REVIEW_NODE_ID
from vibe_guide.monitor import NO_NEW_FILES_NOTE, SCOPE_REVIEW_REASON, ScopeDecisionRequired
from vibe_guide.prompts import INTEGRATION_REVIEW_ROUTING_NOTE, rework_findings_section
from vibe_guide.state import load_events, load_snapshot, save_snapshot
from vibe_guide.task_registry import load_task_binding, save_task_binding

IR = INTEGRATION_REVIEW_NODE_ID


class _Base(unittest.TestCase):
    setUp = fx.MonitorAutoScopeTests.setUp
    tearDown = fx.MonitorAutoScopeTests.tearDown
    _build_complex_plan = fx.MonitorAutoScopeTests._build_complex_plan

    def run_to_review(self, review_events):
        """n1 accepted, integration reviewer running (reviewer-only: no developer)."""
        plan, nodes, card, monitor, delivered = self._build_complex_plan([fx._node("n1")])
        runner = fx._BoundBindingRunner(events={
            ("n1", "developer"): [delivered],
            ("n1", "reviewer"): [("accepted", {"evidence": "review"})],
            (IR, "reviewer"): review_events,
        })
        snapshot = monitor.start(authorize(card, "AUTHORIZE"), runner)
        for _ in range(2):
            snapshot = monitor.tick(snapshot.run_id, runner)
        return monitor, runner, snapshot

    def events(self, run_id, name):
        return [e for e in load_events(self.paths, run_id) if e["event"] == name]


class ReviewerOnlyRoleTests(_Base):
    def test_primary_role_follows_node_type(self):
        monitor, _runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        self.assertEqual(monitor._primary_role(IR, snapshot.nodes[IR]), "reviewer")
        self.assertEqual(monitor._primary_role("n1", snapshot.nodes["n1"]), "developer")
        # a non-integration node whose only generations are reviews
        self.assertEqual(
            monitor._primary_role("n1", {"developer_generation": 0, "review_generation": 2}),
            "reviewer",
        )

    def _stop_reviewer(self, snapshot, status="stopped"):
        binding = load_task_binding(self.paths, IR, "reviewer", run_id=snapshot.run_id)
        binding.status = status
        save_task_binding(self.paths, binding)
        return binding

    def _strand(self, snapshot, status="blocked_unknown"):
        current = snapshot.nodes[IR]
        current.update({"status": status, "active_role": None, "active_task": None,
                        "retryable_action": None, "start_intent": None,
                        "reviewer_started": True})
        snapshot.handles.pop(IR, None)
        save_snapshot(self.paths, snapshot)

    def test_example_1_repair_cleans_up_and_queues_reviewer_successor(self):
        monitor, _runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        self.assertIsNotNone(snapshot.nodes[IR].get("active_task"))
        binding = self._stop_reviewer(snapshot)
        self._strand(snapshot)

        result = monitor.repair_node(IR, snapshot.run_id)

        self.assertEqual(result["kind"], "stuck_session")
        self.assertTrue(result["steps"]["binding_failed"])
        persisted = load_snapshot(self.paths, snapshot.run_id)
        retry = persisted.nodes[IR]["retryable_action"]
        self.assertEqual(retry["role"], "reviewer")
        self.assertTrue(retry["successor"])
        self.assertTrue(retry["pending_schedule"])
        self.assertEqual(retry["predecessor_task_id"], binding.task_id)
        self.assertEqual(persisted.tasks[IR + ":reviewer"]["status"], "failed")
        # a second repair never stacks another successor request
        monitor.repair_node(IR, snapshot.run_id)
        again = load_snapshot(self.paths, snapshot.run_id).nodes[IR]["retryable_action"]
        self.assertEqual((again["role"], again["predecessor_task_id"]),
                         ("reviewer", binding.task_id))

    def test_example_1_tick_recovers_a_missing_reviewer_without_developer(self):
        monitor, _runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        for status in ("stopped", "failed", "archived"):
            binding = self._stop_reviewer(snapshot, status)
            self._strand(snapshot)
            snapshot = load_snapshot(self.paths, snapshot.run_id)
            monitor._reset_binding_cache()
            with self.assertRaises(FileNotFoundError):
                load_task_binding(self.paths, IR, "developer", run_id=snapshot.run_id)
            monitor._recover_missing_reviewer_successor(snapshot, IR)
            retry = snapshot.nodes[IR]["retryable_action"]
            self.assertIsInstance(retry, dict, status)
            self.assertEqual((retry["role"], retry["successor"], retry["predecessor_task_id"]),
                             ("reviewer", True, binding.task_id), status)
            snapshot.nodes[IR]["retryable_action"] = None

    def test_live_reviewer_binding_is_never_replaced(self):
        monitor, _runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        self._strand(snapshot)
        snapshot = load_snapshot(self.paths, snapshot.run_id)
        monitor._reset_binding_cache()
        monitor._recover_missing_reviewer_successor(snapshot, IR)
        self.assertIsNone(snapshot.nodes[IR]["retryable_action"])

    def test_dual_node_still_needs_a_delivered_developer_and_absent_reviewer(self):
        monitor, _runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        current = snapshot.nodes["n1"]
        current.update({"status": "blocked_unknown", "active_task": None, "active_role": None,
                        "retryable_action": None, "reviewer_started": True})
        snapshot.handles.pop("n1", None)
        monitor._reset_binding_cache()
        monitor._recover_missing_reviewer_successor(snapshot, "n1")
        # n1's reviewer binding exists and is accepted: not replaceable
        self.assertIsNone(current["retryable_action"])

    # -- the nine sites: changed above, or not applicable and pinned here ----

    def test_sites_not_applicable_to_reviewer_only_fail_closed(self):
        monitor, _runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        current = snapshot.nodes[IR]
        self.assertFalse(monitor._recover_reverse_blocked_acceptance(snapshot, IR))
        self.assertFalse(monitor._recover_quarantined_delivered_developer(snapshot, IR))
        with self.assertRaisesRegex(ValueError, "reviewer-only"):
            monitor.reconcile_scope(snapshot.run_id, {
                "kind": "scope_correction", "node_id": IR, "task_binding": {},
                "consistency": {}})
        with self.assertRaisesRegex(ValueError, "reviewer-only"):
            monitor._apply_scope_correction(snapshot, IR, {})
        with self.assertRaisesRegex(ValueError, "reviewer-only"):
            self._reject_reconcile(monitor, snapshot)
        self.assertEqual(current["status"], snapshot.nodes[IR]["status"])

    def _reject_reconcile(self, monitor, snapshot):
        # The evidence package covers every unaccepted node; a reviewer-only
        # node has no developer to promote, so the validator names it.
        for node in snapshot.nodes.values():
            node["status"] = "review" if node is snapshot.nodes[IR] else "rework"
        package = {
            "schema_version": 1, "run_id": snapshot.run_id, "plan_id": snapshot.plan_id,
            "plan_revision": snapshot.plan_version,
            "authorization_digest": snapshot.authorization_digest,
            "node_contract_digest": snapshot.node_contract_digest,
            "nodes": [{"node_id": name, "developer": {}, "reviewer": {}}
                      for name in snapshot.nodes],
        }
        monitor._validate_reconciliation_package(snapshot, package)

    def test_schedule_retry_defaults_to_the_reviewer_role(self):
        monitor, _runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        self._strand(snapshot)
        monitor.schedule_retry(IR, "provider timeout", run_id=snapshot.run_id)
        retry = load_snapshot(self.paths, snapshot.run_id).nodes[IR]["retryable_action"]
        self.assertEqual((retry["role"], retry["phase"]), ("reviewer", "review"))


class FindingRoutingTests(_Base):
    """Acceptance examples 5 and 6."""

    def finding(self, **extra):
        data = {"finding": "fixture missing", "severity": "P1"}
        data.update(extra)
        return [("review_finding", data)]

    def drive(self, events, ticks=4):
        monitor, runner, snapshot = self.run_to_review(events)
        for _ in range(ticks - 2):
            snapshot = monitor.tick(snapshot.run_id, runner)
        return monitor, runner, snapshot

    def test_example_5_files_inside_original_scope_queue_rework_without_review(self):
        _m, _r, snapshot = self.drive(self.finding(
            target_node_id="n1", scope_expansion=fx._scope_claim(["n1.py"])))
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(snapshot.nodes["n1"].get("scope_expansions", []), [])
        self.assertNotEqual(snapshot.nodes[IR]["status"], "blocked_design")
        self.assertEqual(snapshot.nodes[IR]["status"], "rework")
        self.assertTrue(snapshot.nodes[IR]["retryable_action"]["pending_schedule"])

    def test_example_5_target_without_scope_request_is_a_plain_rework(self):
        _m, _r, snapshot = self.drive(self.finding(target_node_id="n1"))
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertNotEqual(snapshot.nodes[IR]["status"], "blocked_design")

    def test_example_5_defect_files_inside_one_upstream_scope_map_to_it(self):
        _m, runner, snapshot = self.drive(self.finding(files=["n1.py"]), ticks=3)
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        starts = [c for c in runner.start_calls
                  if c["node_id"] == "n1" and c["role"] == "developer" and c["phase"] == "rework"]
        self.assertEqual(starts[-1]["integration_finding_ref"]["node_id"], IR)
        # once dispatched the marker is spent, so a later rework does not repeat it
        self.assertNotIn("integration_finding_ref", snapshot.nodes["n1"])

    def test_files_outside_every_scope_keep_the_old_self_rework(self):
        _m, _r, snapshot = self.drive(self.finding(files=["elsewhere.py"]))
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        self.assertEqual(snapshot.nodes[IR]["review_loop_rounds"], 1)
        self.assertTrue(snapshot.nodes[IR]["integration_findings"])

    def test_example_6_real_review_still_reports_review_with_other_wording(self):
        _m, _r, snapshot = self.drive(self.finding(
            target_node_id="n1",
            scope_expansion=fx._scope_claim(["vibe_guide/protocols/__init__.py"])))
        self.assertEqual(snapshot.nodes[IR]["status"], "blocked_design")
        self.assertEqual(snapshot.nodes[IR]["blocked_design_cause"], "integration_scope_review")
        self.assertNotEqual(NO_NEW_FILES_NOTE, SCOPE_REVIEW_REASON)
        self.assertIn("product-manager review", SCOPE_REVIEW_REASON)
        self.assertNotIn("product-manager review", NO_NEW_FILES_NOTE)

    def test_scope_expand_with_no_new_files_goes_straight_to_rework(self):
        monitor, _runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        for _ in range(2):
            snapshot = monitor.tick(snapshot.run_id, _runner)
        self.assertEqual(snapshot.nodes["n1"]["status"], "accepted")
        snapshot = monitor.request_scope_expansion(
            snapshot.run_id, "n1", fx._scope_claim(["n1.py"]))
        self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
        self.assertEqual(snapshot.nodes["n1"]["reason"], NO_NEW_FILES_NOTE)

    def test_scope_expand_that_needs_review_still_raises_with_review_wording(self):
        monitor, runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        for _ in range(2):
            snapshot = monitor.tick(snapshot.run_id, runner)
        with self.assertRaises(ScopeDecisionRequired) as caught:
            monitor.request_scope_expansion(
                snapshot.run_id, "n1", fx._scope_claim(["vibe_guide/protocols/__init__.py"]))
        self.assertEqual(str(caught.exception), SCOPE_REVIEW_REASON)

    def test_rework_prompt_carries_the_integration_findings(self):
        root = Path(self.temporary.name)
        deliveries = root / ".vibe" / "provider-actions" / "deliveries"
        deliveries.mkdir(parents=True)
        (deliveries / "run-1-{}-reviewer-g2.json".format(IR)).write_text(json.dumps({
            "payload": {"event": "review_finding",
                        "finding": "SENTINEL integration gap", "severity": "P1"}}))
        contract = {"role": "developer", "phase": "rework", "run_id": "run-1",
                    "node_id": "n1", "project_root": str(root),
                    "integration_finding_ref": {"node_id": IR, "generation": 2}}
        self.assertIn("SENTINEL integration gap", rework_findings_section(contract))
        contract.pop("integration_finding_ref")
        self.assertEqual(rework_findings_section(contract), "")

    def test_integration_dispatch_prompt_explains_target_and_scope_request(self):
        text = rework_findings_section({"role": "reviewer", "node_id": IR, "phase": "review"})
        self.assertEqual(text, INTEGRATION_REVIEW_ROUTING_NOTE)
        for needle in ("target_node_id", "scope_expansion", "review_finding"):
            self.assertIn(needle, text)
        # the example is copyable: it parses once the placeholders are filled
        start = text.index("{\"event\"")
        end = text.rindex("}") + 1
        example = json.loads(text[start:end])
        self.assertEqual(example["event"], "review_finding")
        self.assertEqual(rework_findings_section({"role": "reviewer", "node_id": "n1"}), "")


class UnchangedCommitSetTests(_Base):
    """Acceptance example 4."""

    def test_example_4_same_heads_do_not_open_another_reviewer(self):
        monitor, runner, snapshot = self.run_to_review(
            [("review_finding", {"finding": "x", "severity": "P1", "files": ["elsewhere.py"]})])
        heads = {"n1": "a" * 40}
        with patch.object(monitor, "_reviewed_commit_set", return_value=heads):
            current = snapshot.nodes[IR]
            current["integration_findings"] = ["earlier finding"]
            current["reviewed_commit_set"] = dict(heads)
            before = len(runner.start_calls)
            started = monitor._start_task(snapshot, IR, "reviewer", "review", runner, True)
        self.assertFalse(started)
        self.assertEqual(len(runner.start_calls), before)
        self.assertEqual(current["status"], "rework")
        self.assertIn("等待返工", current["reason"])
        self.assertTrue(current["waiting_for_rework"])
        # one event, not one per tick
        with patch.object(monitor, "_reviewed_commit_set", return_value=heads):
            monitor._start_task(snapshot, IR, "reviewer", "review", runner, True)
        self.assertEqual(len(self.events(snapshot.run_id, "review_skipped_unchanged_commits")), 1)

    def test_a_moved_head_reviews_again_and_records_the_new_set(self):
        monitor, runner, snapshot = self.run_to_review(
            [("review_finding", {"finding": "x", "severity": "P1", "files": ["elsewhere.py"]})])
        current = snapshot.nodes[IR]
        current["integration_findings"] = ["earlier finding"]
        current["reviewed_commit_set"] = {"n1": "a" * 40}
        moved = {"n1": "b" * 40}
        with patch.object(monitor, "_reviewed_commit_set", return_value=moved), \
                patch.object(monitor, "_start_task_dispatch", return_value=True) as dispatch:
            self.assertTrue(monitor._start_task(snapshot, IR, "reviewer", "review", runner, True))
        dispatch.assert_called_once()
        self.assertEqual(current["reviewed_commit_set"], moved)

    def test_an_unreadable_head_never_skips_the_review(self):
        monitor, runner, snapshot = self.run_to_review(
            [("review_finding", {"finding": "x", "severity": "P1", "files": ["elsewhere.py"]})])
        current = snapshot.nodes[IR]
        current["integration_findings"] = ["earlier finding"]
        current["reviewed_commit_set"] = {"n1": "a" * 40}
        with patch.object(monitor, "_reviewed_commit_set", return_value=None), \
                patch.object(monitor, "_start_task_dispatch", return_value=True) as dispatch:
            self.assertTrue(monitor._start_task(snapshot, IR, "reviewer", "review", runner, True))
        dispatch.assert_called_once()

    def test_first_review_has_no_previous_round_to_match(self):
        monitor, runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        heads = {"n1": "a" * 40}
        with patch.object(monitor, "_reviewed_commit_set", return_value=heads), \
                patch.object(monitor, "_start_task_dispatch", return_value=True):
            snapshot.nodes[IR]["reviewed_commit_set"] = dict(heads)
            snapshot.nodes[IR].pop("integration_findings", None)
            self.assertTrue(monitor._start_task(snapshot, IR, "reviewer", "review", runner, False))


class ParkedSelfReportTests(_Base):
    """Acceptance example 3 at the unit level (no live provider)."""

    def test_nothing_to_consume_is_a_no_op(self):
        from vibe_guide.self_report_recovery import consume_parked_delivery
        monitor, runner, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        current = snapshot.nodes[IR]
        # a running node, and a parked node with no archive, are both left alone
        self.assertFalse(consume_parked_delivery(monitor, snapshot, IR, runner))
        binding = load_task_binding(self.paths, IR, "reviewer", run_id=snapshot.run_id)
        binding.status = "stopped"
        save_task_binding(self.paths, binding)
        current.update({"status": "blocked_unknown", "active_task": None, "active_role": None,
                        "retryable_action": None, "reviewer_replacement_required": True})
        snapshot.handles.pop(IR, None)
        monitor._reset_binding_cache()
        self.assertFalse(consume_parked_delivery(monitor, snapshot, IR, runner))
        self.assertEqual(current["status"], "blocked_unknown")

    def _parked_with_archive(self, payload):
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.runners.provider_action import ProviderActionRunner
        monitor, _fake, snapshot = self.run_to_review([("accepted", {"evidence": "x"})])
        current = snapshot.nodes[IR]
        task_id, generation = current["reviewer_identity"], current["review_generation"]
        handle_id = current["active_task"]["handle_id"]
        binding = load_task_binding(self.paths, IR, "reviewer", run_id=snapshot.run_id)
        binding.status = "stopped"
        save_task_binding(self.paths, binding)
        current.update({"status": "blocked_unknown", "active_task": None, "active_role": None,
                        "retryable_action": None, "reviewer_replacement_required": True,
                        "recovery_action": "change_reviewer_or_split_node"})
        snapshot.handles.pop(IR, None)
        monitor._reset_binding_cache()
        runner = ProviderActionRunner(self.paths, "codex-app-visible", "codex-app-visible")
        store = runner.store
        store._atomic(runner._handle_path(handle_id), {
            "run_id": snapshot.run_id, "node_id": IR, "role": "reviewer", "task_id": task_id,
            "generation": generation, "host": "host-1", "wait_sequence": 0,
            "pending_action": None, "pending_operation": None})
        store._directory("deliveries")
        store._atomic(store._delivery_path(snapshot.run_id, IR, "reviewer", generation), {
            "run_id": snapshot.run_id, "issue_id": IR, "role": "reviewer",
            "generation": generation, "payload": payload})
        return monitor, runner, store, snapshot, handle_id, generation

    def test_example_3_parked_reviewers_report_is_consumed_once_and_advances_the_node(self):
        from vibe_guide.self_report_recovery import consume_parked_delivery
        payload = {"event": "review_finding", "finding": "gap", "severity": "P1",
                   "target_node_id": "n1"}
        monitor, runner, store, snapshot, _handle, generation = self._parked_with_archive(payload)
        with patch("vibe_guide.adapters.task_provider.require_entry"), \
                patch("vibe_guide.adapters.task_provider.require_complex_monitor_dispatch"):
            self.assertTrue(consume_parked_delivery(monitor, snapshot, IR, runner))
            # the conclusion advanced the node: the finding routed n1 back to rework
            self.assertEqual(snapshot.nodes["n1"]["status"], "rework")
            self.assertFalse(snapshot.nodes[IR]["reviewer_replacement_required"])
            archive = store._read(store._delivery_path(snapshot.run_id, IR, "reviewer", generation))
            self.assertTrue(archive["consumed_by"])
            self.assertEqual(len(self.events(snapshot.run_id, "self_report_delivery_reconciled")), 1)
            # once only: the archive is consumed, a second resume has nothing to do
            snapshot.nodes[IR].update({"status": "blocked_unknown", "active_task": None,
                                       "active_role": None, "retryable_action": None})
            snapshot.handles.pop(IR, None)
            self.assertFalse(consume_parked_delivery(monitor, snapshot, IR, runner))
        self.assertEqual(len(self.events(snapshot.run_id, "self_report_delivery_reconciled")), 1)

    def test_a_live_reviewer_binding_is_left_to_the_normal_wait_path(self):
        from vibe_guide.self_report_recovery import consume_parked_delivery
        monitor, runner, store, snapshot, _h, generation = self._parked_with_archive(
            {"event": "review_finding", "finding": "gap", "target_node_id": "n1"})
        binding = load_task_binding(self.paths, IR, "reviewer", run_id=snapshot.run_id)
        binding.status = "review"
        save_task_binding(self.paths, binding)
        monitor._reset_binding_cache()
        self.assertFalse(consume_parked_delivery(monitor, snapshot, IR, runner))
        archive = store._read(store._delivery_path(snapshot.run_id, IR, "reviewer", generation))
        self.assertFalse(archive.get("consumed_by"))

    def test_resume_runs_the_parked_consumption_hook(self):
        import inspect
        source = inspect.getsource(monitor_module.Monitor.resume)
        self.assertIn("consume_parked_delivery(self, snapshot, node_id, runner)", source)

    def test_role_is_parameterised_not_hardcoded(self):
        import vibe_guide.self_report_recovery as module
        self.assertFalse(hasattr(module, "_ROLE"))
        self.assertEqual(module._REPLAY_EVENTS["reviewer"] - module._REPLAY_EVENTS["developer"],
                         {"accepted", "review_finding"})


if __name__ == "__main__":
    unittest.main()


class RunnerForwardsRoutingKeysTests(unittest.TestCase):
    def test_wait_result_projection_carries_the_integration_routing_keys(self):
        from vibe_guide.runners.provider_action import RESULT_EVENT_KEYS
        for key in ("target_node_id", "scope_expansion", "files"):
            self.assertIn(key, RESULT_EVENT_KEYS)
