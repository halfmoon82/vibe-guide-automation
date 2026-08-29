import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from vibe_guide.adapters.task_provider import (
    CapabilityObservation,
    ProviderActionStore,
    evaluate_provider_capabilities,
    validate_mailbox_evidence,
)
from vibe_guide.runners.provider_action import ProviderCapabilityGovernance


class V3CapabilityTests(unittest.TestCase):
    def _observations(self, now):
        expires = (now + timedelta(minutes=30)).isoformat()
        return {
            name: CapabilityObservation(
                name=name,
                status="verified_available",
                evidence_ref="runtime:codex:%s" % name,
                observed_at=now.isoformat(),
                expires_at=expires,
                source="codex_app",
            )
            for name in ("create", "enter", "resume", "wait", "terminal", "mailbox")
        }

    def test_structured_runtime_observations_produce_fresh_contract(self):
        now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
        result = evaluate_provider_capabilities(
            provider="codex-app-visible",
            host_id="local",
            observations=self._observations(now),
            now=now,
        )
        self.assertEqual(result.status, "verified_available")
        self.assertEqual(set(result.capabilities), {"create", "enter", "resume", "wait", "terminal", "mailbox"})
        self.assertTrue(result.evidence_refs)
        self.assertEqual(result.to_dict()["status"], "verified_available")

    def test_unknown_or_timeout_is_retry_pending_then_blocked_with_remediation(self):
        now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
        observations = self._observations(now)
        observations["wait"] = CapabilityObservation(
            name="wait", status="unknown_timeout", evidence_ref="runtime:wait:timeout",
            observed_at=now.isoformat(), expires_at=(now + timedelta(minutes=5)).isoformat(), source="codex_app"
        )
        pending = evaluate_provider_capabilities("codex-app-visible", "local", observations, now=now, attempts=1, max_attempts=2)
        self.assertEqual(pending.status, "retry_pending")
        blocked = evaluate_provider_capabilities("codex-app-visible", "local", observations, now=now, attempts=2, max_attempts=2)
        self.assertEqual(blocked.status, "blocked_unknown")
        self.assertTrue(blocked.remediation)
        self.assertIn("unknown_timeout", blocked.to_dict()["capabilities"]["wait"]["status"])

    def test_mailbox_rejects_agent_self_report_and_missing_evidence(self):
        now = datetime.now(timezone.utc)
        with self.assertRaises(ValueError):
            validate_mailbox_evidence({"create": {"status": "verified_available", "evidence_ref": "agent:self"}}, now=now)
        with self.assertRaises(ValueError):
            validate_mailbox_evidence({}, now=now)

    def test_store_persists_only_structured_v3_evidence(self):
        now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProviderActionStore(type("Paths", (), {"resolve_vibe_path": lambda self, name: root / ".vibe" / name, "vibe": root / ".vibe"})())
            evaluation = store.publish_capability_observations("codex-app-visible", "local", self._observations(now), now=now)
            self.assertEqual(store.capability_evaluation(now=now).status, evaluation.status)
            self.assertEqual(json.loads((root / ".vibe/provider-actions/capabilities-v2.json").read_text())["schema_version"], 2)

    def test_expired_evaluation_is_not_still_verified(self):
        now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
        later = now + timedelta(hours=1)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProviderActionStore(type("Paths", (), {"resolve_vibe_path": lambda self, name: root / ".vibe" / name, "vibe": root / ".vibe"})())
            store.publish_capability_observations("codex-app-visible", "local", self._observations(now), now=now)
            self.assertNotEqual(store.capability_evaluation(now=later).status, "verified_available")

    def test_legacy_capability_mailbox_survives_v3_publication(self):
        now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProviderActionStore(type("Paths", (), {"resolve_vibe_path": lambda self, name: root / ".vibe" / name, "vibe": root / ".vibe"})())
            store.publish_capabilities("codex", {"codex.shell": True}, "legacy")
            store.publish_capability_observations("codex-app-visible", "local", self._observations(now), now=now)
            self.assertEqual(store.capabilities()["schema_version"], 1)

    def test_v3_only_projection_maps_provider_to_codex_adapter(self):
        now = datetime(2026, 8, 29, 3, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProviderActionStore(type("Paths", (), {"resolve_vibe_path": lambda self, name: root / ".vibe" / name, "vibe": root / ".vibe"})())
            store.publish_capability_observations("codex-app-visible", "local", self._observations(now), now=now)
            projected = store.capabilities()
            self.assertEqual(projected["adapter_id"], "codex")
            self.assertTrue(all(key.startswith("codex.") for key in projected["facts"]))

    def test_blocked_unknown_has_failure_and_recovery_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ProviderActionStore(type("Paths", (), {"resolve_vibe_path": lambda self, name: root / ".vibe" / name, "vibe": root / ".vibe"})())
            result = ProviderCapabilityGovernance(store, "codex-app-visible", "local").refresh(lambda: (_ for _ in ()).throw(RuntimeError("wait timeout")), attempts=2, max_attempts=2)
            self.assertEqual(result.status, "blocked_unknown")
            self.assertEqual(result.last_operation, "capability_refresh")
            self.assertTrue(result.failure_observation)
            self.assertTrue(result.governance_action)
            self.assertTrue(result.next_action)
            self.assertTrue(result.recovery_entry)
            persisted = store.capability_evaluation()
            self.assertEqual(persisted.status, "blocked_unknown")
            self.assertEqual(persisted.failure_observation["reason"], "wait timeout")


if __name__ == "__main__":
    unittest.main()
