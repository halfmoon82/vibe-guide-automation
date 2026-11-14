"""Public adoption receipts must not silently enable a new run contract."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide import protocol_adoption
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import RunSnapshot
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.models import AgentCapabilities, DAGNode, Plan


class AdoptionCLITests(unittest.TestCase):
    def setUp(self):
        from tests.test_reauthorization_gates import publish_complex_probe
        from vibe_guide.cli import run_cli, _load_plan
        from vibe_guide.monitor import Monitor
        from vibe_guide.runners.fake import FakeRunner
        self.root = publish_complex_probe(self, dispatch_mode="visible")
        self.paths = ProjectPaths(self.root)
        result = run_cli(["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"], self.root)
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.directory, plan, nodes, card = _load_plan(self.paths, "probe-plan")
        self.snapshot = Monitor(self.paths, plan, nodes).start(authorize(card, "AUTHORIZE"), FakeRunner())
        (self.directory / "current-run.json").write_text(json.dumps({"run_id": self.snapshot.run_id}))
        self.evidence = self.root / "approval.md"
        self.evidence.write_text("approved complete r4 responsibility rules")

    def receipt(self, *, partial=False, previous=None):
        manifest = protocol_adoption.responsibility_manifest()
        return protocol_adoption.record_adoption(self.paths,
            plan_id=self.snapshot.plan_id, run_id=self.snapshot.run_id, manifest=manifest,
            accepted_protocols={} if partial else manifest["protocols"],
            evidence_ref="approval.md", evidence_sha256=hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
            authorization_digest=previous or self.snapshot.authorization_digest,
            contract_digest=self.snapshot.node_contract_digest)

    def preview(self, digest=None):
        from vibe_guide.cli import run_cli
        args = ["monitor", "--plan", "probe-plan", "--preview-card", "--json"]
        if digest is not None:
            args += ["--adoption-digest", digest]
        return run_cli(args, self.root)

    def fingerprint(self):
        return {str(path.relative_to(self.root)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in self.root.rglob("*") if path.is_file()}

    def test_preview_selects_exact_receipt_without_writing(self):
        receipt = self.receipt()
        before = self.fingerprint()
        result = self.preview(receipt["adoption_digest"])
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertEqual(result.payload["card"]["adoption_digest"], receipt["adoption_digest"])
        self.assertEqual(result.payload["card"]["ruleset_digest"], receipt["manifest"]["digest"])
        self.assertEqual(self.fingerprint(), before)

    def test_public_init_records_explicit_selection_without_enabling_it(self):
        from vibe_guide.cli import run_cli
        manifest = protocol_adoption.responsibility_manifest()
        selection = {"plan_id": self.snapshot.plan_id, "run_id": self.snapshot.run_id,
                     "accepted_protocols": manifest["protocols"], "evidence_ref": "approval.md",
                     "evidence_sha256": hashlib.sha256(self.evidence.read_bytes()).hexdigest(),
                     "authorization_digest": self.snapshot.authorization_digest,
                     "contract_digest": self.snapshot.node_contract_digest}
        source = self.root / "selection.json"
        source.write_text(json.dumps(selection))
        result = run_cli(["init", "--confirm", "--responsibility-adoption", str(source), "--json"], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        receipt = result.payload["responsibility"]["adoption"]
        self.assertEqual(receipt["status"], "pending_reauthorization")
        self.assertNotEqual(protocol_adoption.effective_responsibility(self.paths, self.snapshot)["status"], "effective")

    def test_pending_receipt_is_not_implicitly_selected(self):
        self.receipt()
        result = self.preview()
        self.assertEqual(result.payload.get("status"), "ok", result.payload)
        self.assertFalse(result.payload["card"].get("adoption_digest"))

    def test_public_monitor_publishes_selected_pair_on_the_original_run(self):
        from vibe_guide.cli import run_cli
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.state import load_snapshot
        receipt = self.receipt()
        preview = self.preview(receipt["adoption_digest"])
        self.assertEqual(preview.payload.get("status"), "ok", preview.payload)
        result = run_cli(["monitor", "--plan", "probe-plan", "--authorize", "AUTHORIZE",
                          "--adoption-digest", receipt["adoption_digest"], "--json"],
                         self.root, runner=FakeRunner())
        # This fixture's visible dispatcher can remain blocked_unknown; the
        # assertion here is the committed authorization, not task execution.
        self.assertIn(result.payload.get("status"), ("ok", "running", "blocked_unknown"), result.payload)
        restored = load_snapshot(self.paths, self.snapshot.run_id)
        self.assertEqual(protocol_adoption.effective_responsibility(self.paths, restored)["status"], "effective")
        published = json.loads((self.directory / "authorization-card.json").read_text())
        for field in ("adoption_digest", "ruleset_digest"):
            self.assertEqual(published[field], preview.payload["card"][field])
        self.assertEqual(restored.run_id, self.snapshot.run_id)
        for key, original in self.snapshot.tasks.items():
            for field in ("task_id", "cursor", "worktree", "branch"):
                self.assertEqual(restored.tasks[key].get(field), original.get(field))

    def test_invalid_selection_is_rejected_without_writing(self):
        for kind in ("partial", "evidence_changed", "previous_authorization", "missing"):
            with self.subTest(kind=kind):
                self.evidence.write_text("approved complete r4 responsibility rules")
                receipt = self.receipt(partial=kind == "partial",
                                       previous="f" * 64 if kind == "previous_authorization" else None)
                if kind == "evidence_changed":
                    self.evidence.write_text("changed after approval")
                before = self.fingerprint()
                result = self.preview("e" * 64 if kind == "missing" else receipt["adoption_digest"])
                self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
                self.assertEqual(self.fingerprint(), before)


    def test_reauthorization_revalidates_receipt_before_writing_run_evidence(self):
        from vibe_guide.cli import _load_plan
        from vibe_guide.authorization import refresh_authorization_card
        from vibe_guide.monitor import Monitor
        from vibe_guide.runners.fake import FakeRunner
        for kind in ("partial", "evidence_changed", "previous_authorization"):
            with self.subTest(kind=kind):
                self.evidence.write_text("approved complete r4 responsibility rules")
                receipt = self.receipt(partial=kind == "partial",
                                       previous="f" * 64 if kind == "previous_authorization" else None)
                if kind == "evidence_changed":
                    self.evidence.write_text("changed after approval")
                _, plan, nodes, card = _load_plan(self.paths, "probe-plan")
                card = refresh_authorization_card(plan, nodes, card,
                    workflow=self.snapshot.workflow,
                    engine_attestation=json.loads((self.directory / "engine-attestation.json").read_text()),
                    ruleset_digest=receipt["manifest"]["digest"],
                    adoption_digest=receipt["adoption_digest"])
                before = self.fingerprint()
                with self.assertRaisesRegex(PermissionError, "adoption"):
                    Monitor(self.paths, plan, nodes).reauthorize(
                        self.snapshot.run_id, authorize(card, "AUTHORIZE"), FakeRunner(),
                        "executable_contract_changed")
                # Taking the monitor lock records lock ownership even on
                # rejection; authorization/events/tasks must remain intact.
                after = self.fingerprint()
                self.assertEqual({k: v for k, v in after.items() if not k.endswith(".lock")},
                                 {k: v for k, v in before.items() if not k.endswith(".lock")})


class ResponsibilityAdoptionTests(unittest.TestCase):
    def test_signed_full_adoption_becomes_effective_only_after_same_run_transition(self):
        from tests import test_monitor as fixture
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.state import load_snapshot
        source = fixture.MonitorTests()
        source.setUp()
        self.addCleanup(source.tearDown)
        nodes = [fixture.node("n1")]
        monitor, old = source.authorized_monitor(nodes)
        runner = FakeRunner()
        snapshot = monitor.start(old, runner)
        approval = source.paths.root / "approval.md"
        approval.write_text("approved full r4 rules")
        manifest = protocol_adoption.responsibility_manifest()
        receipt = protocol_adoption.record_adoption(source.paths,
            plan_id=snapshot.plan_id, run_id=snapshot.run_id, manifest=manifest,
            accepted_protocols=manifest["protocols"], evidence_ref="approval.md",
            evidence_sha256=hashlib.sha256(approval.read_bytes()).hexdigest(),
            authorization_digest=old.digest, contract_digest=old.node_contract_digest)
        card = build_authorization_card(monitor.plan, nodes, source.capabilities,
            ruleset_digest=manifest["digest"], adoption_digest=receipt["adoption_digest"])
        updated = monitor.reauthorize(snapshot.run_id, authorize(card, "AUTHORIZE"),
                                      runner, "executable_contract_changed")
        self.assertEqual(protocol_adoption.effective_responsibility(source.paths, updated)["status"], "effective")
        restored = load_snapshot(source.paths, snapshot.run_id)
        self.assertEqual(protocol_adoption.effective_responsibility(source.paths, restored)["status"], "effective")
        for key, original in snapshot.tasks.items():
            for field in ("task_id", "cursor", "worktree", "branch"):
                self.assertEqual(restored.tasks[key].get(field), original.get(field))

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.paths = ProjectPaths(Path(self.temporary.name))
        evidence = self.paths.root / "approval.md"
        evidence.write_text("approved responsibility rules r4", encoding="utf-8")
        self.args = dict(plan_id="plan-one", run_id="run-one",
                         evidence_ref="approval.md",
                         evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
                         authorization_digest="a" * 64, contract_digest="c" * 64)

    def test_full_adoption_records_pending_without_mutating_run(self):
        manifest = protocol_adoption.responsibility_manifest()
        result = protocol_adoption.record_adoption(
            self.paths, manifest=manifest, accepted_protocols=manifest["protocols"], **self.args)
        self.assertEqual(result["status"], "pending_reauthorization")
        self.assertEqual(len(result["adoption_digest"]), 64)
        self.assertFalse((self.paths.vibe_dir / "runs" / "run-one" / "state.json").exists())

    def test_partial_or_rejected_adoption_never_enables_rules(self):
        manifest = protocol_adoption.responsibility_manifest()
        for selection, expected in (({}, "rejected"),
                                    ({next(iter(manifest["protocols"])): next(iter(manifest["protocols"].values()))}, "partial")):
            with self.subTest(selection=selection):
                result = protocol_adoption.record_adoption(
                    self.paths, manifest=manifest, accepted_protocols=selection, **self.args)
                self.assertEqual(result["status"], expected)
                snapshot = RunSnapshot("run-one", "plan-one", 1, "running", {}, {})
                self.assertNotEqual(protocol_adoption.effective_responsibility(self.paths, snapshot)["status"], "effective")

    def test_changed_approval_evidence_is_rejected_before_writing(self):
        manifest = protocol_adoption.responsibility_manifest()
        self.args["evidence_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "evidence"):
            protocol_adoption.record_adoption(self.paths, manifest=manifest,
                                             accepted_protocols=manifest["protocols"], **self.args)

    def test_missing_adoption_is_unknown_for_real_snapshot(self):
        snapshot = RunSnapshot("run-one", "plan-one", 1, "running", {}, {})
        self.assertEqual(protocol_adoption.effective_responsibility(self.paths, snapshot)["status"], "unknown")

    def test_identical_selection_is_idempotent_and_preserves_partial_receipt(self):
        manifest = protocol_adoption.responsibility_manifest()
        partial = protocol_adoption.record_adoption(self.paths, manifest=manifest, accepted_protocols={}, **self.args)
        first = protocol_adoption.record_adoption(self.paths, manifest=manifest, accepted_protocols=manifest["protocols"], **self.args)
        second = protocol_adoption.record_adoption(self.paths, manifest=manifest, accepted_protocols=manifest["protocols"], **self.args)
        self.assertEqual(first, second)
        self.assertNotEqual(partial["adoption_digest"], first["adoption_digest"])
        self.assertEqual(len(list((self.paths.vibe / "protocol-adoptions").glob("*.json"))), 2)

    def test_symlinked_receipt_store_cannot_write_outside_project(self):
        manifest = protocol_adoption.responsibility_manifest()
        with tempfile.TemporaryDirectory() as outside:
            self.paths.vibe.mkdir(parents=True, exist_ok=True)
            (self.paths.vibe / "protocol-adoptions").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(ValueError):
                protocol_adoption.record_adoption(self.paths, manifest=manifest, accepted_protocols=manifest["protocols"], **self.args)
            self.assertEqual(list(Path(outside).iterdir()), [])

    def test_same_project_receipt_remains_pending_before_reauthorization(self):
        manifest = protocol_adoption.responsibility_manifest()
        receipt = protocol_adoption.record_adoption(self.paths, manifest=manifest, accepted_protocols=manifest["protocols"], **self.args)
        snapshot = RunSnapshot("run-one", "plan-one", 1, "running", {}, {},
                               authorization_digest=self.args["authorization_digest"],
                               node_contract_digest=self.args["contract_digest"])
        self.assertEqual(protocol_adoption.effective_responsibility(self.paths, snapshot)["status"], "pending_reauthorization")

    def test_unsigned_reauthorization_metadata_cannot_enable_rules(self):
        manifest = protocol_adoption.responsibility_manifest()
        receipt = protocol_adoption.record_adoption(self.paths, manifest=manifest, accepted_protocols=manifest["protocols"], **self.args)
        snapshot = {"protocol_adoption": receipt, "reauthorization": {
            "adoption_digest": receipt["adoption_digest"],
            "ruleset_digest": manifest["digest"],
            "plan_id": "plan-one", "run_id": "run-one",
            "authorization_digest": self.args["authorization_digest"],
            "contract_digest": self.args["contract_digest"],
        }}
        self.assertEqual(protocol_adoption.effective_responsibility(self.paths, snapshot)["status"], "unknown")

    def test_mismatched_reauthorization_stays_unknown(self):
        manifest = protocol_adoption.responsibility_manifest()
        receipt = protocol_adoption.record_adoption(self.paths, manifest=manifest, accepted_protocols=manifest["protocols"], **self.args)
        snapshot = {"protocol_adoption": receipt, "reauthorization": {
            "adoption_digest": "f" * 64, "ruleset_digest": manifest["digest"]}}
        self.assertEqual(protocol_adoption.effective_responsibility(self.paths, snapshot)["status"], "unknown")

    def test_unsigned_digest_pair_cannot_be_injected_into_old_authorization(self):
        node = DAGNode("n1", "node", [], [], "g",
                       {"files": ["a.py"], "worker": "worker", "worktree": ".worktrees/n1"}, "ready")
        plan = Plan("plan-one", 1, "prd.md", ["n1"], "draft")
        capabilities = AgentCapabilities("codex", True, True, True, True, True, "full")
        old = authorize(build_authorization_card(plan, [node], capabilities), "AUTHORIZE")
        manifest = protocol_adoption.responsibility_manifest()
        args = dict(self.args, authorization_digest=old.digest, contract_digest=old.node_contract_digest)
        receipt = protocol_adoption.record_adoption(self.paths, manifest=manifest, accepted_protocols=manifest["protocols"], **args)
        injected = dict(old.to_dict(), ruleset_digest=manifest["digest"], adoption_digest=receipt["adoption_digest"])
        snapshot = RunSnapshot("run-one", "plan-one", 1, "running", {}, {},
                               authorization=injected, authorization_digest=old.digest,
                               node_contract_digest=old.node_contract_digest)
        self.assertEqual(protocol_adoption.effective_responsibility(self.paths, snapshot)["status"], "unknown")
