"""Protected production appender origins survive replay, not inbound claims."""
import unittest
from unittest.mock import patch

from tests import test_monitor as fixture
from tests import support_vibe_home as hermetic
from vibe_guide import state
from vibe_guide.contracts import RunEvent
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.supervisor import SupervisorLease
from vibe_guide.telemetry import build_telemetry_data

setUpModule, tearDownModule = hermetic.module_hooks()


class ProductionTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture.MonitorTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        self.paths = self.fixture.paths
        self.monitor, authorization = self.fixture.authorized_monitor([fixture.node("n1")])
        self.snapshot = self.monitor.start(authorization, FakeRunner())
        self.lease = SupervisorLease(self.paths)
        self.epoch = self.lease.takeover(self.snapshot.run_id, 0,
            owner={"session_id": "observer", "provider": "codex"})
        rules = patch("vibe_guide.protocol_adoption.effective_responsibility",
                      return_value={"status": "effective", "adoption_digest": "a" * 64})
        rules.start()
        self.rules = rules
        self.addCleanup(rules.stop)

    def append(self, name="new-observation"):
        return state.append_system_observation(self.paths, self.snapshot.run_id, name,
            owner_epoch=self.epoch, owner_session_id="observer")

    def test_public_append_load_replay_preserves_envelope_and_original_bytes(self):
        sequence = self.append()
        records = state.load_events(self.paths, self.snapshot.run_id)
        self.assertIn("telemetry", records[-1]["data"])
        raw = (state.run_dir(self.paths, self.snapshot.run_id) / "events.jsonl").read_bytes()
        restored = self.monitor.tick(self.snapshot.run_id, FakeRunner())
        self.assertGreaterEqual(restored.event_sequence, sequence)
        self.monitor.tick(self.snapshot.run_id, FakeRunner())
        self.assertTrue((state.run_dir(self.paths, self.snapshot.run_id) / "events.jsonl").read_bytes().startswith(raw))
        self.assertEqual(sum(r["event"] == "new-observation" for r in
                             state.load_events(self.paths, self.snapshot.run_id)), 1)

    def test_missing_independent_origin_cannot_be_recreated_from_record(self):
        self.append()
        directory = state.run_dir(self.paths, self.snapshot.run_id) / "observation-origins"
        next(directory.glob("*.json")).unlink()
        previous = self.snapshot.event_sequence
        with self.assertRaisesRegex(ValueError, "telemetry"):
            self.monitor.tick(self.snapshot.run_id, FakeRunner())
        self.assertEqual(self.snapshot.event_sequence, previous)

    def test_owner_change_and_dedicated_event_cannot_be_pure_observations(self):
        with self.assertRaisesRegex(ValueError, "dedicated"):
            self.append("business_acceptance_revoked")
        self.append()
        self.lease.takeover(self.snapshot.run_id, self.epoch,
            owner={"session_id": "new-owner", "provider": "codex"})
        with self.assertRaisesRegex(ValueError, "telemetry"):
            self.monitor.tick(self.snapshot.run_id, FakeRunner())

    def test_inbound_append_cannot_acquire_internal_origin(self):
        data = build_telemetry_data("untrusted", run_id=self.snapshot.run_id,
            authorization_digest=self.snapshot.authorization_digest,
            node_contract_digest=self.snapshot.node_contract_digest)
        state.append_event(self.paths, RunEvent("untrusted", data))
        self.assertNotIn("telemetry", state.load_events(self.paths, self.snapshot.run_id)[-1]["data"])

    def test_legacy_rejected_beat_uses_known_production_semantics(self):
        provenance = {"role": "system", "task_id": None, "handle_id": None, "generation": 0,
            "authorization_digest": self.snapshot.authorization_digest,
            "node_contract_digest": self.snapshot.node_contract_digest}
        sequence = state.append_event(self.paths, RunEvent("supervisor_beat_rejected",
            {"run_id": self.snapshot.run_id, "reason_code": "record_not_registered"}), provenance)
        restored = self.monitor.tick(self.snapshot.run_id, FakeRunner())
        self.assertGreaterEqual(restored.event_sequence, sequence)

    def test_explicit_monitor_event_routes_cannot_be_observer_events(self):
        import ast
        import inspect
        from vibe_guide.monitor import Monitor
        from vibe_guide.telemetry import LEGACY_TELEMETRY_EVENTS
        tree = ast.parse(inspect.getsource(inspect.getmodule(Monitor)))
        names = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare) or len(node.comparators) != 1:
                continue
            left = ast.unparse(node.left)
            if left not in ("record['event']", "record.get('event')", 'event.event'):
                continue
            value = node.comparators[0]
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                names.add(value.value)
            elif isinstance(value, (ast.Set, ast.Tuple, ast.List)):
                names.update(item.value for item in value.elts
                             if isinstance(item, ast.Constant) and isinstance(item.value, str))
        self.assertGreater(len(names), 30)
        for name in sorted(names - LEGACY_TELEMETRY_EVENTS):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "dedicated"):
                    self.append(name)

    def test_imported_monitor_event_sets_cannot_be_observer_events(self):
        from vibe_guide.monitor import REMOTE_DELIVERY_AUDIT_EVENTS, WORKER_QUESTION_REPLAY_EVENTS
        for name in sorted(REMOTE_DELIVERY_AUDIT_EVENTS | WORKER_QUESTION_REPLAY_EVENTS):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "dedicated"):
                    self.append(name)

    def test_stale_owner_and_symlink_origin_cannot_be_consumed(self):
        from pathlib import Path
        before = state.load_events(self.paths, self.snapshot.run_id)
        with self.assertRaises(PermissionError):
            state.append_system_observation(self.paths, self.snapshot.run_id, "stale-observation",
                owner_epoch=self.epoch, owner_session_id="not-owner")
        self.assertEqual(state.load_events(self.paths, self.snapshot.run_id), before)
        self.append("symlink-observation")
        directory = state.run_dir(self.paths, self.snapshot.run_id) / "observation-origins"
        path = next(directory.glob("*.json"))
        target = self.paths.root / "origin-copy.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, "telemetry"):
            self.monitor.tick(self.snapshot.run_id, FakeRunner())

    def test_origin_write_crash_leaves_unknown_and_never_reconstructs_source(self):
        original = state._atomic_bytes
        def interrupted(path, content):
            if path.parent.name == "observation-origins":
                raise OSError("origin persistence interrupted")
            return original(path, content)
        with patch.object(state, "_atomic_bytes", side_effect=interrupted):
            with self.assertRaisesRegex(OSError, "origin persistence"):
                self.append("origin-crash-observation")
        records = state.load_events(self.paths, self.snapshot.run_id)
        self.assertEqual(records[-1]["event"], "origin-crash-observation")
        frozen = (state.run_dir(self.paths, self.snapshot.run_id) / "events.jsonl").read_bytes()
        with self.assertRaisesRegex(ValueError, "telemetry"):
            self.monitor.tick(self.snapshot.run_id, FakeRunner())
        self.assertEqual((state.run_dir(self.paths, self.snapshot.run_id) / "events.jsonl").read_bytes(), frozen)
        self.assertEqual(list((state.run_dir(self.paths, self.snapshot.run_id) / "observation-origins").glob("*.json")), [])

    def test_modified_origin_digest_is_not_accepted_on_replay(self):
        import json
        self.append()
        directory = state.run_dir(self.paths, self.snapshot.run_id) / "observation-origins"
        path = next(directory.glob("*.json"))
        origin = json.loads(path.read_text())
        origin["record_digest"] = "0" * 64
        path.write_text(json.dumps(origin))
        with self.assertRaisesRegex(ValueError, "telemetry"):
            self.monitor.tick(self.snapshot.run_id, FakeRunner())

    def test_legacy_heartbeat_fast_path_does_not_skip_current_lineage(self):
        provenance = {"role": "system", "task_id": None, "handle_id": None, "generation": 0,
                      "authorization_digest": "f" * 64,
                      "node_contract_digest": self.snapshot.node_contract_digest}
        state.append_event(self.paths, RunEvent("supervisor_heartbeat",
                           {"run_id": self.snapshot.run_id}), provenance)
        with self.assertRaisesRegex(ValueError, "telemetry|lineage|provenance"):
            self.monitor.tick(self.snapshot.run_id, FakeRunner())

    def test_signed_same_run_adoption_enables_real_observation_replay(self):
        import hashlib
        from vibe_guide import protocol_adoption
        from vibe_guide.authorization import authorize, build_authorization_card
        self.rules.stop()
        evidence = self.paths.root / "approval.md"
        evidence.write_text("approved complete r4 responsibility contract")
        manifest = protocol_adoption.responsibility_manifest()
        receipt = protocol_adoption.record_adoption(self.paths,
            plan_id=self.snapshot.plan_id, run_id=self.snapshot.run_id,
            manifest=manifest, accepted_protocols=manifest["protocols"],
            evidence_ref="approval.md", evidence_sha256=hashlib.sha256(evidence.read_bytes()).hexdigest(),
            authorization_digest=self.snapshot.authorization_digest,
            contract_digest=self.snapshot.node_contract_digest)
        card = build_authorization_card(self.monitor.plan, list(self.monitor.nodes.values()),
            self.fixture.capabilities, ruleset_digest=manifest["digest"],
            adoption_digest=receipt["adoption_digest"])
        self.snapshot = self.monitor.reauthorize(self.snapshot.run_id,
            authorize(card, "AUTHORIZE"), FakeRunner(), "executable_contract_changed")
        self.assertEqual(protocol_adoption.effective_responsibility(self.paths, self.snapshot)["status"], "effective")
        sequence = self.append("signed-observation")
        fresh = self.monitor.tick(self.snapshot.run_id, FakeRunner())
        self.assertGreaterEqual(fresh.event_sequence, sequence)
