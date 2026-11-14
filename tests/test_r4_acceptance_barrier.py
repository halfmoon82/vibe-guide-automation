"""Durable business invalidation must precede downstream side effects."""
import unittest
import threading
import copy
import hashlib
from unittest.mock import patch

from tests.test_monitor import MonitorTests, node
from tests import test_monitor as monitor_fixture
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events, load_snapshot
from vibe_guide.contracts import RunEvent
from vibe_guide.state import append_event


class AcceptanceBarrierTests(unittest.TestCase):
    def setUp(self):
        self.fixture = MonitorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.paths = self.fixture.paths
        self.rules = patch("vibe_guide.protocol_adoption.effective_responsibility",
                           return_value={"status": "effective"})
        self.rules.start()
        self.addCleanup(self.rules.stop)

    def test_revoke_persists_barrier_and_preserves_active_worker(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up"), node("down", ["up"])])
        runner = FakeRunner()
        snapshot = monitor.start(authorization, runner)
        original = dict(snapshot.handles)
        result = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="missing real input", evidence_ref="finding.md")
        self.assertEqual(result.nodes["down"]["business_barrier"]["status"], "invalidated")
        self.assertEqual(result.handles, original)
        self.assertEqual(len(runner.start_calls), 1)
        self.assertEqual(runner.stop_calls, [])
        events = load_events(self.paths, snapshot.run_id)
        self.assertEqual(events[-1]["event"], "business_acceptance_revoked")

    def test_barrier_survives_crash_before_snapshot(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up"), node("down", ["up"])])
        runner = FakeRunner()
        snapshot = monitor.start(authorization, runner)
        with patch.object(monitor, "_save_snapshot", side_effect=RuntimeError("snapshot crash")):
            with self.assertRaisesRegex(RuntimeError, "snapshot crash"):
                monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="input contradicted", evidence_ref="finding.md")
        monitor.resume(snapshot.run_id, runner)
        restored = load_snapshot(self.paths, snapshot.run_id)
        self.assertEqual(restored.nodes["down"]["business_barrier"]["status"], "invalidated")
        self.assertEqual(len(runner.start_calls), 1)

    def test_unknown_impact_holds_all_nodes_without_second_writer(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up"), node("independent")])
        runner = FakeRunner()
        snapshot = monitor.start(authorization, runner)
        result = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="impact unknown",
                                                    evidence_ref="finding.md", impact_known=False)
        self.assertEqual(set(result.nodes), {"up", "independent"})
        self.assertTrue(all(current["business_barrier"]["impact_known"] is False for current in result.nodes.values()))
        self.assertEqual(len(runner.start_calls), 2)

    def test_late_delivery_is_retained_without_starting_reviewer(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up"), node("down", ["up"])])
        runner = FakeRunner({"up": [RunEvent("delivered", {"evidence": "old input delivery"})]})
        snapshot = monitor.start(authorization, runner)
        monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="input invalid", evidence_ref="finding.md")
        result = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(len(runner.start_calls), 1)
        self.assertIsNone(result.nodes["up"]["acceptance"])
        self.assertTrue(result.nodes["up"].get("quarantined_business_results"))

    def test_revocation_requires_effective_r4_rules(self):
        self.rules.stop()
        monitor, authorization = self.fixture.authorized_monitor([node("up")])
        snapshot = monitor.start(authorization, FakeRunner())
        with self.assertRaisesRegex(PermissionError, "rules"):
            monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="contradiction", evidence_ref="finding.md")

    def test_old_accepted_replay_cannot_cross_barrier(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up")])
        runner = FakeRunner()
        snapshot = monitor.start(authorization, runner)
        snapshot = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="invalid input", evidence_ref="finding.md")
        active = snapshot.nodes["up"]["active_task"]
        append_event(self.paths, RunEvent("accepted", {"run_id": snapshot.run_id, "node_id": "up",
            "evidence": "late old clearance", "contract_digest": snapshot.nodes["up"]["contract_digest"],
            "authorization_epoch": snapshot.authorization_digest}),
            {"role": "reviewer", "task_id": active["task_id"], "handle_id": active["handle_id"],
             "generation": active["generation"], "authorization_digest": snapshot.authorization_digest,
             "node_contract_digest": snapshot.node_contract_digest})
        # A purported reviewer callback on the developer identity must still
        # fail provenance checks; the barrier is not a reason to trust it.
        with self.assertRaisesRegex(ValueError, "provenance"):
            monitor.resume(snapshot.run_id, runner)
        result = load_snapshot(self.paths, snapshot.run_id)
        self.assertNotEqual(result.nodes["up"]["status"], "accepted")
        self.assertIsNone(result.nodes["up"]["acceptance"])

    def check_serialized_entry(self, entry):
        monitor, authorization = self.fixture.authorized_monitor([node("up")])
        entered = threading.Event()
        release = threading.Event()
        revoked = threading.Event()
        failures = []
        class PausedRunner(FakeRunner):
            def poll(self, handle):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test did not release poll")
                return []
        runner = PausedRunner()
        snapshot = monitor.start(authorization, runner)
        def tick():
            try:
                monitor.tick(snapshot.run_id, runner)
            except Exception as error:
                failures.append(error)
        def revoke():
            try:
                entry(monitor, snapshot.run_id)
                revoked.set()
            except Exception as error:
                failures.append(error)
        first = threading.Thread(target=tick)
        second = threading.Thread(target=revoke)
        first.start()
        try:
            self.assertTrue(entered.wait(5))
            second.start()
            self.assertFalse(revoked.wait(.1), "revocation crossed an active tick transaction")
        finally:
            release.set()
            first.join(5)
            if second.ident is not None:
                second.join(5)
        self.assertEqual(failures, [])
        self.assertTrue(revoked.is_set())

    def test_tick_and_revocation_share_the_run_transaction(self):
        self.check_serialized_entry(lambda monitor, run_id: monitor.revoke_business_acceptance(
            run_id, "up", reason="contradiction", evidence_ref="finding.md"))

    def test_integration_entry_cannot_cross_active_tick_transaction(self):
        def entry(monitor, run_id):
            try:
                monitor.record_integration_review(run_id, {})
            except ValueError:
                pass  # Shape rejection must occur inside the run transaction.
        self.check_serialized_entry(entry)

    def test_evidence_reconciliation_cannot_cross_active_tick_transaction(self):
        def entry(monitor, run_id):
            try:
                monitor.reconcile_evidence(run_id, {})
            except ValueError:
                pass
        self.check_serialized_entry(entry)

    def test_reauthorization_cannot_cross_active_tick_transaction(self):
        def entry(monitor, run_id):
            from vibe_guide.authorization import AuthorizationRecord
            record = AuthorizationRecord.from_dict(load_snapshot(self.paths, run_id).authorization)
            monitor.reauthorize(run_id, record, FakeRunner(), "same contract reauthorization")
        self.check_serialized_entry(entry)

    def test_technical_clearance_without_business_items_cannot_unlock_downstream(self):
        upstream = node("up")
        upstream.contract["acceptance_examples"] = ["real material supplied"]
        monitor, authorization = self.fixture.authorized_monitor([upstream, node("down", ["up"])])
        runner = FakeRunner({
            ("up", "developer"): [("delivered", {"evidence": "technical tests pass"})],
            ("up", "reviewer"): [("accepted", {"evidence": "P0-P2 clear; needs_input"})],
        })
        snapshot = monitor.start(authorization, runner)
        monitor.tick(snapshot.run_id, runner)
        result = monitor.tick(snapshot.run_id, runner)
        self.assertNotEqual(result.nodes["up"]["status"], "accepted")
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["up", "up"])

    def test_satisfied_required_business_item_unlocks_downstream(self):
        upstream = node("up")
        upstream.contract["acceptance_examples"] = ["real material supplied"]
        monitor, authorization = self.fixture.authorized_monitor([upstream, node("down", ["up"])])
        material = self.paths.root / "material.json"
        material.write_text('{"source": "verified fixture material"}', encoding="utf-8")
        proof = {"round": 1, "inputs": {}, "items": {"real material supplied": {
            "status": "satisfied", "evidence_ref": "material.json",
            "evidence_sha256": hashlib.sha256(material.read_bytes()).hexdigest()}}}
        runner = FakeRunner({
            ("up", "developer"): [("delivered", {"evidence": "delivered"})],
            ("up", "reviewer"): [("accepted", {"evidence": "review clear", "business_acceptance": proof})],
        })
        snapshot = monitor.start(authorization, runner)
        monitor.tick(snapshot.run_id, runner)
        result = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(result.nodes["up"]["status"], "accepted")
        self.assertEqual(result.nodes["up"]["business_acceptance"], proof)
        self.assertEqual(runner.start_calls[-1]["node_id"], "down")

    def test_missing_or_tampered_business_material_never_unlocks_downstream(self):
        upstream = node("up")
        upstream.contract["acceptance_examples"] = ["real material supplied"]
        monitor, authorization = self.fixture.authorized_monitor([upstream, node("down", ["up"])])
        proof = {"round": 1, "inputs": {}, "items": {"real material supplied": {
            "status": "satisfied", "evidence_ref": "material.json", "evidence_sha256": "a" * 64}}}
        runner = FakeRunner({("up", "developer"): [("delivered", {"evidence": "done"})],
            ("up", "reviewer"): [("accepted", {"evidence": "reviewed", "business_acceptance": proof}),
                                     ("accepted", {"evidence": "reviewed", "business_acceptance": proof})]})
        snapshot = monitor.start(authorization, runner)
        monitor.tick(snapshot.run_id, runner)
        missing = monitor.tick(snapshot.run_id, runner)
        self.assertNotEqual(missing.nodes["up"]["status"], "accepted")
        (self.paths.root / "material.json").write_text("different bytes", encoding="utf-8")
        changed = monitor.tick(snapshot.run_id, runner)
        self.assertNotEqual(changed.nodes["up"]["status"], "accepted")
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["up", "up"])

    def test_new_inputs_require_new_round_on_original_reviewer(self):
        upstream = node("up")
        upstream.contract["acceptance_examples"] = ["real material supplied"]
        monitor, authorization = self.fixture.authorized_monitor([upstream, node("down", ["up"])])
        runner = FakeRunner({("up", "developer"): [("delivered", {"evidence": "done"})]})
        snapshot = monitor.start(authorization, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        identity = dict(snapshot.nodes["up"]["active_task"])
        snapshot = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="source stale", evidence_ref="finding.md")
        material = self.paths.root / "material.json"
        material.write_text("new verified material", encoding="utf-8")
        digest = hashlib.sha256(material.read_bytes()).hexdigest()
        inputs = {"material.json": digest}
        resume = getattr(monitor, "revalidate_business_inputs", None)
        self.assertTrue(callable(resume), "business barriers need a protected input-version revalidation entry")
        snapshot = resume(snapshot.run_id, "up", barrier_id=snapshot.nodes["up"]["business_barrier"]["id"],
                          inputs_by_node={"up": inputs, "down": inputs}, evidence_ref="material.json")
        self.assertEqual(snapshot.nodes["up"]["active_task"], identity)
        self.assertEqual(snapshot.nodes["up"]["business_acceptance_round"], 2)
        item = {"status": "satisfied", "evidence_ref": "material.json", "evidence_sha256": digest}
        runner.events[("up", "reviewer")] = [
            ("accepted", {"business_acceptance": {"round": 1, "inputs": inputs, "items": {"real material supplied": item}}}),
            ("accepted", {"evidence": "new round verified", "business_acceptance": {"round": 2, "inputs": inputs, "items": {"real material supplied": item}}})]
        stale = monitor.tick(snapshot.run_id, runner)
        self.assertNotEqual(stale.nodes["up"]["status"], "accepted")
        result = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(result.nodes["up"]["status"], "accepted")
        self.assertEqual(result.nodes["up"]["reviewer_identity"], identity["task_id"])
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["up", "up", "down"])

    def test_revalidation_rejects_overlapping_newer_barrier_before_writing_event(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up"), node("down", ["up"])])
        snapshot = monitor.start(authorization, FakeRunner())
        snapshot = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="source stale", evidence_ref="finding.md")
        old_barrier = snapshot.nodes["up"]["business_barrier"]["id"]
        monitor.revoke_business_acceptance(snapshot.run_id, "down", reason="independent contradiction", evidence_ref="finding.md")
        material = self.paths.root / "material.json"
        material.write_text("replacement input", encoding="utf-8")
        inputs = {"material.json": hashlib.sha256(material.read_bytes()).hexdigest()}
        before = load_events(self.paths, snapshot.run_id)
        with self.assertRaisesRegex(ValueError, "barrier"):
            monitor.revalidate_business_inputs(snapshot.run_id, "up", barrier_id=old_barrier,
                inputs_by_node={"up": inputs, "down": inputs}, evidence_ref="material.json")
        self.assertEqual(load_events(self.paths, snapshot.run_id), before)

    def test_old_inputs_cannot_be_resubmitted_as_a_new_round(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up")])
        snapshot = monitor.start(authorization, FakeRunner())
        material = self.paths.root / "material.json"
        material.write_text("same disputed material", encoding="utf-8")
        inputs = {"material.json": hashlib.sha256(material.read_bytes()).hexdigest()}
        snapshot.nodes["up"]["business_inputs"] = inputs
        monitor._save_snapshot(snapshot)
        snapshot = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="source contradicted", evidence_ref="finding.md")
        before = load_events(self.paths, snapshot.run_id)
        with self.assertRaisesRegex(ValueError, "unchanged"):
            monitor.revalidate_business_inputs(snapshot.run_id, "up", barrier_id=snapshot.nodes["up"]["business_barrier"]["id"],
                inputs_by_node={"up": inputs}, evidence_ref="material.json")
        self.assertEqual(load_events(self.paths, snapshot.run_id), before)

    def test_old_acceptance_recovered_audit_cannot_resurrect_revoked_result(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up"), node("down", ["up"])])
        runner = FakeRunner()
        snapshot = monitor.start(authorization, runner)
        snapshot = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="source contradicted", evidence_ref="finding.md")
        append_event(self.paths, RunEvent("acceptance_recovered", {"run_id": snapshot.run_id, "node_id": "up"}),
            {"role": "system", "task_id": None, "handle_id": None, "generation": 0,
             "authorization_digest": snapshot.authorization_digest,
             "node_contract_digest": snapshot.node_contract_digest})
        result = monitor.resume(snapshot.run_id, runner)
        self.assertNotEqual(result.nodes["up"]["status"], "accepted")
        self.assertEqual(len(runner.start_calls), 1)

    def test_real_reviewer_acceptance_replay_cannot_cross_revocation(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up"), node("down", ["up"])])
        runner = FakeRunner({("up", "developer"): [("delivered", {"evidence": "done"})]})
        snapshot = monitor.start(authorization, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        snapshot = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="invalid source", evidence_ref="finding.md")
        active = dict(snapshot.nodes["up"]["active_task"])
        append_event(self.paths, RunEvent("accepted", {"run_id": snapshot.run_id, "node_id": "up",
            "evidence": "old reviewer clearance", "contract_digest": snapshot.nodes["up"]["contract_digest"],
            "authorization_epoch": snapshot.authorization_digest}),
            {**active, "authorization_digest": snapshot.authorization_digest,
             "node_contract_digest": snapshot.node_contract_digest})
        result = monitor.resume(snapshot.run_id, runner)
        self.assertNotEqual(result.nodes["up"]["status"], "accepted")
        self.assertIsNone(result.nodes["up"]["acceptance"])
        self.assertEqual(result.nodes["up"]["active_task"], active)
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["up", "up"])

    def test_required_business_items_also_gate_replayed_acceptance(self):
        upstream = node("up")
        upstream.contract["acceptance_examples"] = ["real material supplied"]
        monitor, authorization = self.fixture.authorized_monitor([upstream, node("down", ["up"])])
        runner = FakeRunner({("up", "developer"): [("delivered", {"evidence": "done"})]})
        snapshot = monitor.start(authorization, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        active = dict(snapshot.nodes["up"]["active_task"])
        append_event(self.paths, RunEvent("accepted", {"run_id": snapshot.run_id, "node_id": "up",
            "evidence": "technical tests only", "contract_digest": snapshot.nodes["up"]["contract_digest"],
            "authorization_epoch": snapshot.authorization_digest}),
            {**active, "authorization_digest": snapshot.authorization_digest,
             "node_contract_digest": snapshot.node_contract_digest})
        result = monitor.resume(snapshot.run_id, runner)
        self.assertNotEqual(result.nodes["up"]["status"], "accepted")
        self.assertEqual([call["node_id"] for call in runner.start_calls], ["up", "up"])

    def test_visible_sdd_replayed_acceptance_requires_business_items(self):
        payload, nodes = monitor_fixture.load_v46_dispatch_fixture()
        nodes[0].contract["acceptance_examples"] = ["real input available"]
        fixture = monitor_fixture.VisibleDispatchTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        monitor, record = fixture.authorized_monitor(nodes, topology_rulings=payload["topology_rulings"])
        runner = monitor_fixture.VisibleSddRunner(events={("sdd-a", "developer"): [("complete", {
            "evidence": "delivery", "in_session_review": {
                "protocol": monitor_fixture.VISIBLE_SDD_PROTOCOL_REF,
                "evidence_ref": "delivery#review", "clearance": {"p0": 0, "p1": 0, "p2": 0},
                "coverage": {"mode": "ocr", "listed": 3, "reviewed": 3, "skipped": []},
                "environment_facts_ref": "none"}})]})
        snapshot = monitor.start(record, runner)
        preserved = copy.deepcopy(snapshot)
        # Produce a historical acceptance under the old rules; replaying it
        # with r4 effective must not manufacture missing business evidence.
        with patch("vibe_guide.protocol_adoption.effective_responsibility", return_value={"status": "unknown"}):
            monitor.tick(snapshot.run_id, runner)
        monitor_fixture.save_snapshot(fixture.paths, preserved)
        recovered = monitor.tick(snapshot.run_id, runner)
        self.assertNotEqual(recovered.nodes["sdd-a"]["status"], "accepted")
        self.assertIsNone(recovered.nodes["sdd-a"]["acceptance"])

    def test_integration_review_cannot_close_an_invalidated_run(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up")])
        snapshot = monitor.start(authorization, FakeRunner())
        monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="input revoked", evidence_ref="finding.md")
        with self.assertRaises((PermissionError, ValueError)) as caught:
            monitor.record_integration_review(snapshot.run_id, {})
        self.assertIsInstance(caught.exception, PermissionError, "invalid business state must be rejected before integration evidence")
        self.assertIn("business", str(caught.exception))

    def test_revalidation_replays_after_event_before_snapshot_crash(self):
        monitor, authorization = self.fixture.authorized_monitor([node("up"), node("down", ["up"])])
        runner = FakeRunner()
        snapshot = monitor.start(authorization, runner)
        snapshot = monitor.revoke_business_acceptance(snapshot.run_id, "up", reason="input stale", evidence_ref="finding.md")
        material = self.paths.root / "material.json"
        material.write_text("new source", encoding="utf-8")
        inputs = {"material.json": hashlib.sha256(material.read_bytes()).hexdigest()}
        with patch.object(monitor, "_save_snapshot", side_effect=RuntimeError("snapshot crash")):
            with self.assertRaisesRegex(RuntimeError, "snapshot crash"):
                monitor.revalidate_business_inputs(snapshot.run_id, "up",
                    barrier_id=snapshot.nodes["up"]["business_barrier"]["id"],
                    inputs_by_node={"up": inputs, "down": inputs}, evidence_ref="material.json")
        restored = monitor.resume(snapshot.run_id, runner)
        self.assertEqual(restored.nodes["up"].get("business_acceptance_round"), 2)
        self.assertEqual(restored.nodes["up"]["business_inputs"], inputs)
        self.assertEqual(restored.nodes["down"]["business_barrier"]["status"], "revalidated")
        self.assertEqual(len(runner.start_calls), 1)

    def test_valid_business_acceptance_survives_snapshot_loss(self):
        upstream = node("up")
        upstream.contract["acceptance_examples"] = ["real material supplied"]
        monitor, authorization = self.fixture.authorized_monitor([upstream])
        material = self.paths.root / "material.json"
        material.write_text("verified material", encoding="utf-8")
        proof = {"round": 1, "inputs": {}, "items": {"real material supplied": {
            "status": "satisfied", "evidence_ref": "material.json",
            "evidence_sha256": hashlib.sha256(material.read_bytes()).hexdigest()}}}
        runner = FakeRunner({("up", "developer"): [("delivered", {"evidence": "done"})],
                             ("up", "reviewer"): [("accepted", {"evidence": "reviewed", "business_acceptance": proof})]})
        snapshot = monitor.start(authorization, runner)
        snapshot = monitor.tick(snapshot.run_id, runner)
        preserved = copy.deepcopy(snapshot)
        result = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(result.nodes["up"]["status"], "accepted")
        monitor_fixture.save_snapshot(self.paths, preserved)
        recovered = monitor.tick(snapshot.run_id, runner)
        self.assertEqual(recovered.nodes["up"]["status"], "accepted")
        self.assertEqual(recovered.nodes["up"].get("business_acceptance"), proof)
