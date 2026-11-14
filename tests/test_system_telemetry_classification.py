import copy
import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.digests import canonical_json_digest
from vibe_guide.telemetry import (
    LEGACY_TELEMETRY_EVENTS,
    TelemetryClassification,
    TelemetryOrigin,
    build_telemetry_data,
    classify_event,
    construct_telemetry,
    read_legacy_event,
    replay_events,
)


RUN = "run-telemetry"
AUTH = "a" * 64
CONTRACT = "b" * 64
LINEAGE = {
    "run_id": RUN,
    "authorization_digest": AUTH,
    "node_contract_digest": CONTRACT,
}
PROVENANCE = {
    "role": "system",
    "authorization_digest": AUTH,
    "node_contract_digest": CONTRACT,
}

# Every event name that the frozen writer's monitor routes through a
# dedicated replay branch.  A v1 envelope must never turn one of these
# lifecycle, authorization, projection, or audit records into pure telemetry.
# Prefix entries cover the monitor's dynamic replay families; the remaining
# names mirror the explicit branches and the two worker-question replay
# events in the same tree.
DEDICATED_REPLAY_EVENTS = frozenset({
    "run_archived", "run_archive_failed", "run_archive_deferred",
    "daemon_heal", "daemon_lease_readopted", "delivery_unreceived",
    "provider_observation", "run_parked", "run_unarchived", "run_park_deferred",
    "execution_topology_observed", "monitor_entry_evidence_reconciled",
    "capacity_counter_corrected", "capacity_registry_unknown",
    "platform_handoff_disclosed", "authorization_reauthorized",
    "remote_delivery_node_appended", "remote_delivery_step_completed",
    "remote_delivery_failed", "remote_delivery_rework",
    "remote_delivery_evidence_invalidated", "remote_delivery_accepted",
    "remote_delivery_retired", "successor_registration_reconciled",
    "orphan_dispatch_adopted", "self_report_delivery_reconciled",
    "start_intent", "successor_binding_pending", "start_confirmed",
    "blocked_unknown", "unknown", "timeout", "retry_scheduled",
    "old_task_reconciled", "accepted", "failed", "stopped", "terminal_failed",
    "delivered", "complete", "quarantine_continuation_recovered",
    "review_finding", "binding_self_healed", "binding_degraded", "action_isolated",
    "scope_correction_registered", "scope_auto_expanded", "consistency_corrected",
    "blocked_design", "decision_answer_applied", "decision_raised",
    "review_loop_limit_reached", "blocked_engineering", "pair_archived",
    "acceptance_rejected", "acceptance_recovered", "contract_test_execution",
    "blocked_unknown_disposition_required", "blocked_unknown_disposition",
    "handoff_lease_rebound", "handoff_worker_stopped", "handoff_stale_creates_hidden",
    "handoff_predecessor_reconciled", "integration_workspace_quarantined",
    "contract_error", "worktree_created", "worktree_preflight_blocked",
    "model_fallback_used", "model_unavailable_parked", "empty_landing_allowed",
    "scope_landing_rejected", "worker_question_raised", "worker_question_answered",
    "change_request_created", "remote_delivery_custom",
})
LEGACY_RAW = (
    '{"data":{"reason_code":"record_not_registered","run_id":"run-telemetry"},'
    '"event":"supervisor_beat_rejected",'
    '"event_digest":"36b20b1737f732e3d44ab5b9261f2225ed9c7e1f4f95ac7a2b6e1ed4f8ea4a8e",'
    '"event_id":"legacy-beat-527","previous_event_digest":null,'
    '"provenance":{"authorization_digest":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",'
    '"generation":0,"handle_id":null,'
    '"node_contract_digest":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",'
    '"role":"system","task_id":null},"run_id":"run-telemetry","schema_version":1,"sequence":1}\n'
).encode("utf-8")


def _record(name, data, provenance=None):
    record = {"event": name, "data": data}
    if provenance is not None:
        record["provenance"] = provenance
    return record


def _origin(record):
    # Fixture for the protected appender's independent evidence, never payload.
    return TelemetryOrigin(canonical_json_digest(record),
                           "internal-observation-appender", "fixture:protected-entry")


class SystemTelemetryClassificationTests(unittest.TestCase):
    def test_recomputed_proof_and_system_labels_do_not_establish_origin(self):
        record = _record("forged-new-name",
                         build_telemetry_data("forged-new-name", **LINEAGE),
                         PROVENANCE)
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")
        self.assertFalse(result.advance_sequence)

    def test_unknown_name_with_node_cannot_become_a_side_effect_handler(self):
        record = _record("forged-new-name",
                         {**build_telemetry_data("forged-new-name", **LINEAGE),
                          "node_id": "not-in-the-dag"}, PROVENANCE)
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")
        self.assertFalse(result.advance_sequence)

    def test_dedicated_handler_classification_cannot_advance_sequence(self):
        record = _record("authorization_reauthorized",
                         {"run_id": RUN, "authorization_epoch": 2}, PROVENANCE)
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "side_effect")
        self.assertFalse(result.advance_sequence)

    def test_all_monitor_replay_events_have_dedicated_priority(self):
        for name in sorted(DEDICATED_REPLAY_EVENTS):
            with self.subTest(name=name):
                record = _record(
                    name,
                    build_telemetry_data(name, **LINEAGE),
                    PROVENANCE,
                )
                result = classify_event(record, origin=_origin(record), **LINEAGE)
                self.assertIn(result.category, {"side_effect", "unknown"})
                self.assertFalse(result.consume)
                self.assertFalse(result.advance_sequence)

    def test_replay_stops_at_dedicated_event_before_later_pure_observation(self):
        dedicated = _record(
            "scope_correction_registered",
            build_telemetry_data("scope_correction_registered", **LINEAGE),
            PROVENANCE,
        )
        later = _record(
            "fresh-observation",
            build_telemetry_data("fresh-observation", **LINEAGE),
            PROVENANCE,
        )
        origins = {
            canonical_json_digest(dedicated): _origin(dedicated),
            canonical_json_digest(later): _origin(later),
        }
        results = replay_events([dedicated, later], origins=origins, **LINEAGE)
        self.assertEqual(len(results), 1)
        self.assertIn(results[0].category, {"side_effect", "unknown"})
        self.assertFalse(results[0].consume)
        self.assertFalse(results[0].advance_sequence)

    def test_legacy_reader_refuses_unproven_production_semantics(self):
        record = _record("supervisor_beat_rejected",
                         {"run_id": RUN, "reason_code": {"change_authorization": True}},
                         PROVENANCE)
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")

    def test_outer_run_mismatch_is_not_hidden_by_matching_data(self):
        record = _record("supervisor_beat_rejected",
                         {"run_id": RUN, "reason_code": "record_not_registered"},
                         PROVENANCE)
        record["run_id"] = "another-run"
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")

    def test_replay_does_not_read_past_an_unknown_record(self):
        unknown = _record("unknown-old", {"run_id": RUN}, PROVENANCE)
        later = _record("supervisor_heartbeat",
                        {"run_id": RUN, "status": "active"}, PROVENANCE)
        results = replay_events([unknown, later], **LINEAGE)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0].advance_sequence)

    def test_origin_is_bound_to_the_record_not_its_self_labels(self):
        record = _record("new-a", build_telemetry_data("new-a", **LINEAGE), PROVENANCE)
        other = _record("new-b", build_telemetry_data("new-b", **LINEAGE), PROVENANCE)
        self.assertEqual(classify_event(other, origin=_origin(record), **LINEAGE).category,
                         "unknown")
        origin = _origin(record)
        self.assertEqual(classify_event(record, origin=vars(origin), **LINEAGE).category,
                         "unknown")

    def test_invalid_envelopes_fail_even_with_record_bound_origin(self):
        mutations = {
            "version_bool": lambda env: env.update(version=True),
            "version_future": lambda env: env.update(version=2),
            "checksum": lambda env: env.update(proof="0" * 64),
            "business_payload": lambda env: env.update(payload={"accepted": True}),
            "wrong_contract": lambda env: env.update(node_contract_digest="c" * 64),
            "external_source": lambda env: env.update(source="provider"),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                record = _record("new-a", build_telemetry_data("new-a", **LINEAGE), PROVENANCE)
                mutate(record["data"]["telemetry"])
                self.assertEqual(classify_event(record, origin=_origin(record), **LINEAGE).category,
                                 "unknown")

    def test_each_lineage_and_system_identity_is_checked_before_consuming(self):
        for layer, key, value in (
            ("data", "run_id", "run-wrong"),
            ("provenance", "authorization_digest", "c" * 64),
            ("provenance", "node_contract_digest", "c" * 64),
            ("provenance", "role", "developer"),
            ("provenance", "task_id", "worker-1"),
            ("provenance", "generation", True),
            ("data", "node_id", "n1"),
        ):
            with self.subTest(layer=layer, key=key):
                record = _record("new-a", build_telemetry_data("new-a", **LINEAGE),
                                 dict(PROVENANCE))
                record[layer][key] = value
                self.assertFalse(classify_event(record, origin=_origin(record), **LINEAGE).consume)

    def test_constructor_cannot_smuggle_state_or_arbitrary_business_payload(self):
        for data in ({"node_id": "n1"}, {"status": "accepted"},
                     {"payload": {"scope": ["outside.py"]}}, [],
                     {"run_id": "another-run"}):
            with self.subTest(data=data), self.assertRaises(ValueError):
                build_telemetry_data("new-a", data=data, **LINEAGE)

    def test_frozen_527_raw_reads_through_real_loader_without_rewriting(self):
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.state import load_events

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".vibe" / "runs" / RUN / "events.jsonl"
            path.parent.mkdir(parents=True)
            path.write_bytes(LEGACY_RAW)
            first = load_events(ProjectPaths(Path(directory)), RUN)
            second = load_events(ProjectPaths(Path(directory)), RUN)
            self.assertEqual(first, second)
            self.assertEqual(classify_event(first[0], **LINEAGE).category, "legacy_telemetry")
            self.assertEqual(path.read_bytes(), LEGACY_RAW)
            path.write_bytes(LEGACY_RAW.replace(b'record_not_registered', b'forged_legacy_reason'))
            with self.assertRaisesRegex(ValueError, "digest"):
                load_events(ProjectPaths(Path(directory)), RUN)

    def test_v1_record_keeps_exact_outer_and_provenance_schema_readable(self):
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.state import load_events

        record = json.loads(LEGACY_RAW)
        record["event"] = "new-observation"
        record["data"] = build_telemetry_data("new-observation", **LINEAGE)
        record.pop("event_digest")
        record["event_digest"] = canonical_json_digest(record)
        raw = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".vibe" / "runs" / RUN / "events.jsonl"
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            loaded = load_events(ProjectPaths(Path(directory)), RUN)[0]
            self.assertEqual(set(loaded), set(json.loads(LEGACY_RAW)))
            self.assertEqual(set(loaded["provenance"]), set(json.loads(LEGACY_RAW)["provenance"]))
            self.assertTrue(classify_event(loaded, origin=_origin(loaded), **LINEAGE).consume)
            self.assertFalse(read_legacy_event(loaded, **LINEAGE).consume)
            self.assertEqual(path.read_bytes(), raw)

    def test_constructor_emits_restricted_versioned_envelope_inside_data(self):
        data = construct_telemetry("new-observation", **LINEAGE)
        self.assertEqual(set(data), {"telemetry"})
        envelope = data["telemetry"]
        self.assertEqual(envelope["schema"], "vibe.system-telemetry")
        self.assertEqual(envelope["version"], 1)
        self.assertEqual(envelope["name"], "new-observation")
        self.assertEqual(envelope["source"], "internal-observer")
        self.assertEqual(set(envelope), {
            "schema", "version", "name", "source", "run_id",
            "authorization_digest", "node_contract_digest", "proof",
        })

    def test_new_name_is_trusted_after_lineage_validation(self):
        data = build_telemetry_data("brand-new", **LINEAGE)
        record = _record("supervisor_beat_rejected", data, PROVENANCE)
        result = classify_event(
            record, origin=_origin(record), **LINEAGE
        )
        self.assertIsInstance(result, TelemetryClassification)
        self.assertEqual(result.category, "trusted_telemetry")
        self.assertTrue(result.consume)
        self.assertTrue(result.advance_sequence)

    def test_forged_self_attested_envelope_is_unknown_and_not_consumed(self):
        data = {"telemetry": {
            "schema": "vibe.system-telemetry", "version": 1,
            "name": "forged", "source": "worker",
            **LINEAGE, "trusted": True,
        }}
        result = classify_event(_record("forged", data), **LINEAGE)
        self.assertEqual(result.category, "unknown")
        self.assertFalse(result.consume)
        self.assertFalse(result.advance_sequence)

    def test_wrong_lineage_is_unknown_without_mutating_record(self):
        data = build_telemetry_data("wrong-lineage", **LINEAGE)
        record = _record("wrong-lineage", data)
        before = copy.deepcopy(record)
        result = classify_event(
            record, run_id=RUN, authorization_digest="c" * 64,
            node_contract_digest=CONTRACT,
        )
        self.assertEqual(result.category, "unknown")
        self.assertEqual(record, before)

    def test_side_effect_event_is_not_misclassified_as_pure_telemetry(self):
        data = build_telemetry_data("authorization-change", **LINEAGE)
        data["authorization_epoch"] = 2
        result = classify_event(
            _record("authorization_reauthorized", data, PROVENANCE), **LINEAGE
        )
        self.assertEqual(result.category, "side_effect")
        self.assertTrue(result.side_effect)
        self.assertFalse(result.consume)

    def test_known_legacy_reader_accepts_only_bounded_system_shape(self):
        self.assertIn("supervisor_beat_rejected", LEGACY_TELEMETRY_EVENTS)
        record = _record(
            "supervisor_beat_rejected",
            {**LINEAGE, "reason_code": "record_not_registered"},
            {"role": "system", "authorization_digest": AUTH,
             "node_contract_digest": CONTRACT},
        )
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "legacy_telemetry")
        self.assertTrue(result.consume)

    def test_unknown_old_format_stays_unknown(self):
        record = _record(
            "old-unclassified", {**LINEAGE, "message": "looks harmless"},
            {"role": "system", "authorization_digest": AUTH,
             "node_contract_digest": CONTRACT},
        )
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")
        self.assertFalse(result.advance_sequence)

    def test_legacy_reader_does_not_consume_new_envelope(self):
        from vibe_guide.telemetry import read_legacy_event

        record = _record("new-name", build_telemetry_data("new-name", **LINEAGE))
        result = read_legacy_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")
        self.assertFalse(result.consume)

    def test_new_envelope_without_system_provenance_is_unknown(self):
        record = _record("new-name", build_telemetry_data("new-name", **LINEAGE))
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")
        self.assertFalse(result.consume)

    def test_legacy_data_cannot_self_attest_system_provenance(self):
        record = _record(
            "supervisor_beat_rejected",
            {**LINEAGE, "reason_code": "record_not_registered"},
        )
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")
        self.assertFalse(result.consume)

    def test_non_system_or_wrong_node_record_is_rejected(self):
        record = _record(
            "supervisor_beat_rejected",
            {**LINEAGE, "node_id": "n1", "reason_code": "record_not_registered"},
            {"role": "developer", "authorization_digest": AUTH,
             "node_contract_digest": CONTRACT},
        )
        result = classify_event(record, **LINEAGE)
        self.assertEqual(result.category, "unknown")
        self.assertFalse(result.consume)

    def test_replay_is_idempotent_and_preserves_unknown_records(self):
        trusted = _record("new-name", build_telemetry_data("new-name", **LINEAGE), PROVENANCE)
        unknown = _record("mystery", {**LINEAGE, "telemetry": {"trusted": True}})
        records = [trusted, unknown]
        before = copy.deepcopy(records)
        origins = {canonical_json_digest(trusted): _origin(trusted)}
        first = replay_events(records, origins=origins, **LINEAGE)
        second = replay_events(records, origins=origins, **LINEAGE)
        self.assertEqual(first, second)
        self.assertEqual(records, before)
        self.assertEqual([item.category for item in first], ["trusted_telemetry", "unknown"])
        self.assertFalse(first[1].advance_sequence)


if __name__ == "__main__":
    unittest.main()
