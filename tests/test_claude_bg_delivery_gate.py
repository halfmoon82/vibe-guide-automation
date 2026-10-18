"""claude-code-background: a self-reported delivery must pass the delivery gate.

Recorded failure (run-0d53e1d1, 2026-10-17, v5.0.18): two background sessions
finished and ran ``vibe worker-deliver``; the archive under
``provider-actions/deliveries/`` was written and ``ClaudeBgRunner.poll``
returned ``delivered``, yet the monitor marked both nodes ``blocked_unknown``
at once.  The runner nested the worker's ``delivery_evidence`` under
``payload`` where the monitor never looks, and a background binding has no
host/cursor while the only cursor substitute was a consumed ``wait`` request
that background dispatch never creates.

The PR #236 tests stopped at ``runner.poll() == delivered`` and ran without
the ``vibeguide_monitor`` engine, so the delivery gate never executed.  These
tests drive the gated path end to end: start -> self-report -> a fresh
process resumes the run.
"""
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.authorization import _canonical_digest, authorize, build_authorization_card
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.dag import append_integration_review_node
from vibe_guide.engine_attestation import create_engine_attestation
from vibe_guide.evidence import evaluate_delivery_evidence
from vibe_guide.models import AgentCapabilities, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.planner import REQUIRED_COMPLEX_WORKFLOW, TaskContext
from vibe_guide.runners.claude_bg import ClaudeBgRunner
from vibe_guide.state import load_events
from vibe_guide.task_registry import load_task_binding
from vibe_guide.workflow_gate import create_task_workflow, record_workflow_node

from tests.test_claude_bg_reconnect import GOOD, FakeLauncher, background_node


class BackgroundDeliveryGateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = ProjectPaths(Path(self.temporary.name))
        self.paths.vibe.mkdir(parents=True, exist_ok=True)
        save_contract(self.paths, build_contract(self.paths.root, provider="fake", host_id="local"))
        self.capabilities = AgentCapabilities("fake", True, True, True, True, True, "full")
        self.plan, self.nodes, self.record = self._engine_plan()

    def tearDown(self):
        self.temporary.cleanup()

    def _engine_plan(self):
        """Complex plan under the vibeguide_monitor engine (as in issue 88)."""
        (self.paths.root / "docs").mkdir(parents=True, exist_ok=True)
        (self.paths.root / "docs" / "prd.md").write_text("prd\n", encoding="utf-8")
        (self.paths.root / "docs" / "spec.md").write_text("spec\n", encoding="utf-8")
        workflow = create_task_workflow("plan-bg", TaskContext(5, 5, 5, 5, 5))
        plan_dir = self.paths.vibe / "plans" / "plan-bg"
        plan_dir.mkdir(parents=True, exist_ok=True)
        evidence_file = plan_dir / "workflow-evidence.md"
        evidence_file.write_text("evidence\n", encoding="utf-8")
        artifact = {
            "ref": evidence_file.name,
            "sha256": hashlib.sha256(evidence_file.read_bytes()).hexdigest(),
        }
        # ``resume`` re-checks the two files dispatch rewrites against the
        # invariant authorization rests on (authorize_entry.LIFECYCLE_REFS).
        (plan_dir / "plan.json").write_text(json.dumps({"decisions": []}), encoding="utf-8")
        (plan_dir / "plan-confirmation.json").write_text(
            json.dumps({"authorization_digest": "card", "status": "confirmed"}), encoding="utf-8")
        for node_id in REQUIRED_COMPLEX_WORKFLOW:
            output = {"ref": node_id}
            evidence = {"ref": node_id, "artifact": artifact}
            if node_id == "plan_confirmation":
                output["authorization_digest"] = "card"
            if node_id == "product_decision":
                evidence["decision_digest"] = _canonical_digest(
                    {"decisions": [], "evidence_priority": []})
            record_workflow_node(workflow, node_id, {"ref": node_id}, output, evidence)
        (self.paths.vibe / "state.json").write_text(
            json.dumps({"workflow_version": 2, "session_gate": "s0_required",
                        "task_workflow": workflow}),
            encoding="utf-8",
        )
        plan = append_integration_review_node(Plan(
            "plan-bg", 1, "docs/prd.md", ["bg", "bg2"], "draft",
            spec_path="docs/spec.md",
            complexity_band="complex",
            nodes=[background_node("bg"), background_node("bg2")],
            integration_contract={
                "iteration_context": {"kind": "iteration", "based_on": "V5"},
                "compatibility_scope": ["V5 API"],
                "agentsmd_acceptance_refs": ["AGENTS.md#8"],
                "integration_acceptance_contract": {"checks": ["all"]},
                "unverified_or_excluded": ["provider"],
            },
        ))
        nodes = list(plan.nodes)
        attestation = create_engine_attestation(
            plan_id=plan.plan_id, plan_revision=plan.version,
            execution_engine="vibeguide_monitor", engine_mode="dag",
            provider="fake", capability_facts={"fake.worktree": True},
            provenance="live:test",
            now=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        (plan_dir / "engine-attestation.json").write_text(json.dumps(attestation), encoding="utf-8")
        card = build_authorization_card(
            plan, nodes, self.capabilities,
            execution_engine="vibeguide_monitor", engine_mode="dag",
            engine_evidence_ref=attestation["evidence_ref"],
        )
        return plan, nodes, authorize(card, "AUTHORIZE")

    def monitor(self):
        """A fresh Monitor, as a new ``vibe resume`` process builds one."""
        monitor = Monitor(self.paths, self.plan, self.nodes)
        # Topology-projection evidence is a separate gate (issue-88 fixture).
        monitor._validate_execution_topology = lambda snapshot: None
        return monitor

    def runner(self, state="working"):
        return ClaudeBgRunner(lambda c, w: FakeLauncher(state), paths=self.paths)

    def start(self):
        snapshot = self.monitor().start(self.record, self.runner())
        self.assertEqual(snapshot.execution_engine, "vibeguide_monitor")
        self.assertEqual(snapshot.nodes["bg"]["status"], "running")
        return snapshot

    def generation(self, snapshot, node_id="bg"):
        binding = load_task_binding(self.paths, node_id, "developer", run_id=snapshot.run_id)
        self.assertEqual((binding.mode, binding.host, binding.cursor), ("background", None, None))
        return binding.generation

    def deliveries(self):
        return self.paths.vibe / "provider-actions" / "deliveries"

    def archive_path(self, run_id, node_id, generation):
        return self.deliveries() / "{}-{}-developer-g{}.json".format(run_id, node_id, generation)

    def test_self_report_reaches_review_in_a_new_process(self):
        snapshot = self.start()
        outcome = ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg", "developer", dict(GOOD), self.generation(snapshot)
        )
        self.assertTrue(outcome["recorded"])

        snapshot = self.monitor().resume(snapshot.run_id, self.runner())

        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "review", node.get("reason"))
        self.assertEqual(node["delivery_path"], GOOD["delivery_evidence"]["delivery_path"])
        # The other node never reported and keeps running.
        self.assertEqual(snapshot.nodes["bg2"]["status"], "running")
        delivered = [record for record in load_events(self.paths, snapshot.run_id)
                     if record["event"] == "delivered"]
        self.assertEqual(len(delivered), 1)

    def test_delivery_is_consumed_once(self):
        snapshot = self.start()
        generation = self.generation(snapshot)
        store = ProviderActionStore(self.paths)
        store.record_worker_delivery(snapshot.run_id, "bg", "developer", dict(GOOD), generation)
        self.assertEqual(store.unconsumed_deliveries(snapshot.run_id), ["bg"])

        snapshot = self.monitor().resume(snapshot.run_id, self.runner())
        self.assertEqual(snapshot.nodes["bg"]["status"], "review")

        archive = json.loads(self.archive_path(snapshot.run_id, "bg", generation).read_text(encoding="utf-8"))
        self.assertTrue(archive["consumed_by"])
        self.assertEqual(store.unconsumed_deliveries(snapshot.run_id), [])
        # Re-reporting the identical payload is a duplicate, not a second result.
        again = store.record_worker_delivery(snapshot.run_id, "bg", "developer", dict(GOOD), generation)
        self.assertEqual((again["duplicate"], again["consumed"]), (True, False))

    def test_archive_for_another_node_does_not_vouch(self):
        snapshot = self.start()
        generation = self.generation(snapshot)
        ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg2", "developer", dict(GOOD), self.generation(snapshot, "bg2")
        )
        # bg2's archive under bg's file name: its identity fields still say bg2.
        source = self.archive_path(snapshot.run_id, "bg2", generation)
        self.archive_path(snapshot.run_id, "bg", generation).write_text(
            source.read_text(encoding="utf-8"), encoding="utf-8")

        snapshot = self.monitor().resume(snapshot.run_id, self.runner())

        self.assertNotIn(snapshot.nodes["bg"]["status"], {"review", "delivered"})

    def test_archive_of_another_generation_does_not_vouch(self):
        snapshot = self.start()
        generation = self.generation(snapshot)
        ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg", "developer", dict(GOOD), generation
        )
        path = self.archive_path(snapshot.run_id, "bg", generation)
        archive = json.loads(path.read_text(encoding="utf-8"))
        archive["generation"] = generation + 1
        path.write_text(json.dumps(archive), encoding="utf-8")

        snapshot = self.monitor().resume(snapshot.run_id, self.runner())

        self.assertNotIn(snapshot.nodes["bg"]["status"], {"review", "delivered"})

    def test_background_reviewer_acceptance_reaches_accepted(self):
        # The report's own event decides the transition, as for a visible
        # wait result; a reviewer's acceptance is never read as a delivery.
        snapshot = self.start()
        store = ProviderActionStore(self.paths)
        store.record_worker_delivery(snapshot.run_id, "bg", "developer", dict(GOOD), self.generation(snapshot))
        snapshot = self.monitor().resume(snapshot.run_id, self.runner())
        self.assertEqual(snapshot.nodes["bg"]["status"], "review")
        reviewer = load_task_binding(self.paths, "bg", "reviewer", run_id=snapshot.run_id)
        self.assertEqual(reviewer.mode, "background")

        store.record_worker_delivery(snapshot.run_id, "bg", "reviewer",
                                     dict(GOOD, event="accepted", evidence="P0-P2 clear"), reviewer.generation)
        snapshot = self.monitor().resume(snapshot.run_id, self.runner())

        self.assertEqual(snapshot.nodes["bg"]["status"], "accepted", snapshot.nodes["bg"].get("reason"))
        self.assertEqual(store.unconsumed_deliveries(snapshot.run_id), [])

    def test_visible_binding_keeps_host_and_cursor_requirements(self):
        snapshot = self.start()
        generation = self.generation(snapshot)
        store = ProviderActionStore(self.paths)
        store.record_worker_delivery(snapshot.run_id, "bg", "developer", dict(GOOD), generation)
        # The registry now says visible (a loadable binding needs a host; it
        # has no cursor): the background receipt must not stand in for it.
        registry = self.paths.vibe / "runs" / snapshot.run_id / "tasks.json"
        data = json.loads(registry.read_text(encoding="utf-8"))
        for binding in data["bindings"]:
            if binding.get("issue_id") == "bg":
                binding.update(mode="visible", topology="dual-visible", visible=True,
                               threadId="thread-1", hostId="local", limitations=[])
        registry.write_text(json.dumps(data), encoding="utf-8")
        # Same task identity as the visible binding, so only the mode refuses.
        self.assertIsNone(store.background_delivery_receipt(
            snapshot.run_id, "bg", "developer", generation, "thread-1",
            GOOD["delivery_evidence"]))

        snapshot = self.monitor().resume(snapshot.run_id, self.runner())

        node = snapshot.nodes["bg"]
        self.assertEqual(node["status"], "blocked_unknown")
        self.assertIn("current cursor is missing", node["reason"])

    def forge_archive(self, snapshot, **fields):
        """Record bg's own report, then rewrite identity fields in the archive."""
        generation = self.generation(snapshot)
        ProviderActionStore(self.paths).record_worker_delivery(
            snapshot.run_id, "bg", "developer", dict(GOOD), generation)
        path = self.archive_path(snapshot.run_id, "bg", generation)
        archive = json.loads(path.read_text(encoding="utf-8"))
        archive.update(fields)
        path.write_text(json.dumps(archive), encoding="utf-8")
        return generation

    def test_each_layer_refuses_a_foreign_archive(self):
        # The runner's reader and the store's receipt both check the archive
        # identity; neither may rely on the other.
        snapshot = self.start()
        for fields in ({"issue_id": "bg2"}, {"run_id": "run-other"}, {"role": "reviewer"},
                       {"generation": 99}):
            generation = self.forge_archive(snapshot, **fields)
            claims = {"run_id": snapshot.run_id, "node_id": "bg", "role": "developer",
                      "generation": generation}
            self.assertIsNone(self.runner()._read_delivery(claims), fields)
            self.assertIsNone(ProviderActionStore(self.paths).background_delivery_receipt(
                snapshot.run_id, "bg", "developer", generation, "developer:bg",
                GOOD["delivery_evidence"]), fields)
            self.archive_path(snapshot.run_id, "bg", generation).unlink()

    def test_receipt_refuses_another_task_or_consumer(self):
        snapshot = self.start()
        store = ProviderActionStore(self.paths)
        generation = self.forge_archive(snapshot)
        receipt = lambda task: store.background_delivery_receipt(
            snapshot.run_id, "bg", "developer", generation, task, GOOD["delivery_evidence"])
        self.assertIsNotNone(receipt("developer:bg"))
        self.assertIsNone(receipt("developer:other"))
        self.forge_archive(snapshot, consumed_by="action-123")
        self.assertIsNone(receipt("developer:bg"))
        self.assertFalse(store.consume_background_delivery(
            snapshot.run_id, "bg", "developer", generation, "developer:bg"))

    def test_session_done_without_report_stays_blocked_unknown(self):
        snapshot = self.start()
        snapshot = self.monitor().resume(snapshot.run_id, self.runner("done"))
        self.assertEqual(snapshot.nodes["bg"]["status"], "blocked_unknown")


class BackgroundReceiptGateUnitTests(unittest.TestCase):
    ARTIFACTS = dict(GOOD["delivery_evidence"])

    def binding(self, mode):
        return {"mode": mode, "task_id": "developer:bg", "host": None, "hostId": None,
                "cursor": None, "worktree": ".worktrees/bg", "branch": "branch-bg"}

    def test_background_receipt_replaces_host_and_cursor_for_background_only(self):
        node = {"status": "DELIVERED"}
        self.assertTrue(evaluate_delivery_evidence(
            node, self.binding("background"), self.ARTIFACTS, background_receipt=True).complete)
        visible = evaluate_delivery_evidence(
            node, self.binding("visible"), self.ARTIFACTS, background_receipt=True)
        self.assertEqual(visible.reasons, ["host identity is missing", "current cursor is missing"])
        self.assertEqual(visible.classification, "blocked_unknown")
        without = evaluate_delivery_evidence(node, self.binding("background"), self.ARTIFACTS)
        self.assertEqual(without.reasons, ["host identity is missing", "current cursor is missing"])


if __name__ == "__main__":
    unittest.main()
