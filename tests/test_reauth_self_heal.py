"""Authorization must survive bookkeeping drift and still die on real change.

Replays the two run-ee4575fa situations: engine evidence that aged out or was
re-stamped with every contract fingerprint unchanged, and a snapshot whose
monitor-entry bookkeeping disagrees with the event stream.  The counter-cases
prove a genuinely changed contract still invalidates, and says which item.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from vibe_guide import cli as cli_module
from vibe_guide import monitor as monitor_module
from vibe_guide.authorization import authorize
from vibe_guide.cli import _load_plan, _persist_invalidation, run_cli
from vibe_guide.engine_attestation import needs_renewal
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import load_events, load_snapshot

from tests.support_v45_authorize import publish_complex_probe


class _RunFixture(unittest.TestCase):
    def setUp(self):
        self.root = publish_complex_probe(self)
        self.paths = ProjectPaths(self.root)
        authorized = run_cli(
            ["authorize", "--plan", "probe-plan", "--authorize", "AUTHORIZE", "--json"],
            self.root,
        )
        assert authorized.payload.get("status") == "ok", authorized.payload
        self.directory, plan, nodes, card = _load_plan(self.paths, "probe-plan")
        self.runner = FakeRunner()
        record = authorize(card, "AUTHORIZE")
        self.snapshot = Monitor(self.paths, plan, nodes).start(record, self.runner)
        (self.directory / "authorization.json").write_text(
            json.dumps(record.to_dict()), encoding="utf-8"
        )
        (self.directory / "current-run.json").write_text(
            json.dumps({"run_id": self.snapshot.run_id}), encoding="utf-8"
        )
        self.attestation_path = self.directory / "engine-attestation.json"
        self.published = json.loads(self.attestation_path.read_text(encoding="utf-8"))

    def _resume(self):
        return run_cli(
            ["resume", "--plan", "probe-plan", "--run-id", self.snapshot.run_id, "--json"],
            self.root,
            runner=self.runner,
        )

    def _events(self, name):
        return [e for e in load_events(self.paths, self.snapshot.run_id) if e["event"] == name]

    def _invalidation(self):
        return self.directory / "authorization-invalidated.json"

    def _aged(self):
        """Make the published evidence a day past its 72h window, nothing else."""
        real_validate = monitor_module.validate_engine_attestation
        real_needs = needs_renewal
        beyond = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat().replace("+00:00", "Z")

        def validate(candidate, plan_id, plan_revision, **kwargs):
            if candidate.get("digest") == self.published["digest"]:
                kwargs["now"] = beyond
            return real_validate(candidate, plan_id, plan_revision, **kwargs)

        def needs(attestation, plan_id, revision, ref, now=None):
            return real_needs(attestation, plan_id, revision, ref, now=beyond)

        return (
            patch.object(monitor_module, "validate_engine_attestation", validate),
            patch.object(cli_module, "needs_renewal", needs),
        )


class EngineEvidenceRenewalTests(_RunFixture):
    def test_expired_evidence_with_unchanged_contract_renews_and_resumes(self):
        monitor_patch, cli_patch = self._aged()
        with monitor_patch, cli_patch:
            result = self._resume()
        self.assertNotEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertFalse(self._invalidation().exists())
        renewed = [
            e for e in self._events("authorization_reauthorized")
            if e["data"].get("change_reason") == "engine_attestation_renewed"
        ]
        self.assertEqual(len(renewed), 1)
        current = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        self.assertNotEqual(current["evidence_ref"], self.published["evidence_ref"])
        record = json.loads((self.directory / "authorization.json").read_text(encoding="utf-8"))
        self.assertEqual(record["engine_evidence_ref"], current["evidence_ref"])

    def test_node_contract_change_still_invalidates_and_names_the_item(self):
        nodes_path = self.directory / "nodes.json"
        payload = json.loads(nodes_path.read_text(encoding="utf-8"))
        target = payload["nodes"][0] if isinstance(payload, dict) else payload[0]
        target["contract"]["acceptance_example"] = "verified_fact: README 含两行文本"
        nodes_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        monitor_patch, cli_patch = self._aged()
        with monitor_patch, cli_patch:
            result = self._resume()
        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertTrue(self._invalidation().exists())
        marker = json.loads(self._invalidation().read_text(encoding="utf-8"))
        self.assertNotEqual(marker["changed_item"], "unclassified")
        self.assertEqual(self._events("authorization_reauthorized"), [])
        # The stale evidence was not quietly replaced.
        current = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        self.assertEqual(current["digest"], self.published["digest"])

    def test_tampered_evidence_is_not_a_renewal_case(self):
        tampered = dict(self.published, provenance="forged")
        self.attestation_path.write_text(json.dumps(tampered), encoding="utf-8")
        result = self._resume()
        self.assertEqual(self._events("authorization_reauthorized"), [])
        self.assertFalse(self._invalidation().exists(), result.payload)
        self.assertNotEqual(result.payload.get("status"), "ok")

    def _append(self, path):
        path.write_text(path.read_text(encoding="utf-8") + "\n额外一行改动\n", encoding="utf-8")

    def _assert_invalidated_not_renewed(self):
        monitor_patch, cli_patch = self._aged()
        with monitor_patch, cli_patch:
            result = self._resume()
        self.assertEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertTrue(self._invalidation().exists())
        self.assertEqual(self._events("authorization_reauthorized"), [])

    def test_expired_evidence_does_not_launder_an_edited_spec(self):
        self._append(sorted((self.directory / "specs").glob("*.md"))[0])
        self._assert_invalidated_not_renewed()

    def test_expired_evidence_does_not_launder_an_edited_issue(self):
        self._append(sorted((self.directory / "issues").glob("*.md"))[0])
        self._assert_invalidated_not_renewed()

    def test_expired_evidence_judges_an_allowlist_edit_like_fresh_evidence(self):
        nodes_path = self.directory / "nodes.json"
        payload = json.loads(nodes_path.read_text(encoding="utf-8"))
        target = payload["nodes"][0] if isinstance(payload, dict) else payload[0]
        target["allowlist"] = list(target.get("allowlist", [])) + ["extra/x.py"]
        nodes_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        fresh = self._resume()
        fresh_invalidated = self._invalidation().exists()
        self.assertTrue(fresh_invalidated, fresh.payload)  # non-vacuous baseline
        self._invalidation().unlink()
        monitor_patch, cli_patch = self._aged()
        with monitor_patch, cli_patch:
            aged = self._resume()
        self.assertTrue(self._invalidation().exists(), aged.payload)
        self.assertEqual(self._events("authorization_reauthorized"), [])

    def test_fresh_evidence_is_left_alone(self):
        result = self._resume()
        self.assertNotEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertEqual(self._events("authorization_reauthorized"), [])
        current = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        self.assertEqual(current["digest"], self.published["digest"])


class EntryEvidenceSelfHealTests(_RunFixture):
    def _misrecord_entry(self, value="monitor.reconcile_adopted_dispatch"):
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        stream = [e for e in self._events("execution_topology_observed")][-1]["data"]
        self.assertNotEqual(stream["monitor_entry_evidence"], value)
        snapshot.monitor_entry_evidence = value
        _, plan, nodes, _card = _load_plan(self.paths, "probe-plan")
        Monitor(self.paths, plan, nodes)._save_snapshot(snapshot)
        return stream["monitor_entry_evidence"]

    def test_resume_trusts_the_event_stream_and_continues(self):
        stream_value = self._misrecord_entry()
        result = self._resume()
        self.assertNotEqual(result.payload.get("status"), "blocked_design", result.payload)
        self.assertFalse(self._invalidation().exists())
        healed = self._events("monitor_entry_evidence_reconciled")
        self.assertEqual(len(healed), 1)
        self.assertEqual(
            healed[0]["data"]["previous_monitor_entry_evidence"],
            "monitor.reconcile_adopted_dispatch",
        )
        self.assertEqual(healed[0]["data"]["monitor_entry_evidence"], stream_value)

    def test_unhealed_drift_reports_both_actual_values(self):
        self._misrecord_entry("monitor.tick")
        snapshot = load_snapshot(self.paths, self.snapshot.run_id)
        _, plan, nodes, _card = _load_plan(self.paths, "probe-plan")
        with self.assertRaises(PermissionError) as raised:
            Monitor(self.paths, plan, nodes)._validate_execution_topology(snapshot)
        self.assertIn("snapshot='monitor.tick'", str(raised.exception))
        self.assertIn("events='monitor.start'", str(raised.exception))

    def test_invalidation_marker_names_the_changed_item_and_both_values(self):
        _persist_invalidation(
            self.directory,
            "authorization invalidated: blocked_by_execution_topology_mismatch: "
            "monitor entry evidence drift (snapshot='a', events='b')",
        )
        marker = json.loads(self._invalidation().read_text(encoding="utf-8"))
        self.assertEqual(marker["changed_item"], "monitor_entry_evidence")
        self.assertIn("snapshot='a'", marker["reason"])
        self.assertIn("events='b'", marker["reason"])


if __name__ == "__main__":
    unittest.main()
