import unittest

from vibe_guide.models import Plan, V4ExecutionPolicy
from vibe_guide.planner import build_v4_execution_policy


class V4SddContractTests(unittest.TestCase):
    def _plan(self):
        return Plan("v4-plan", 1, "prd.md", ["n1"], "confirmed_pending_authorization", decisions=[])

    def test_policy_is_versioned_sdd_first_and_overrides_binding_hard_gates(self):
        policy = build_v4_execution_policy(self._plan(), [])
        self.assertEqual(policy.workflow_version, 4)
        self.assertEqual(policy.execution_mode, "sdd_first")
        for gate in ("stage_a", "stage_b", "stage_c", "stage_d", "stage_e", "provider"):
            self.assertFalse(policy.hard_gates[gate])

    def test_unknown_execution_mode_is_rejected(self):
        with self.assertRaises(ValueError):
            V4ExecutionPolicy.from_dict({"workflow_version": 4, "execution_mode": "other"})

    def test_round_trip_is_json_safe(self):
        policy = build_v4_execution_policy(self._plan(), [])
        payload = policy.to_dict()
        payload["legacy_fields"] = {"future_flag": True}
        # digest must be recomputed after changing payload
        payload.pop("digest")
        policy = V4ExecutionPolicy.from_dict(V4ExecutionPolicy(**{k: v for k, v in payload.items()}).to_dict())
        self.assertTrue(policy.to_dict()["legacy_fields"]["future_flag"])

    def test_gate_schema_and_boolean_values_are_strict(self):
        policy = build_v4_execution_policy(self._plan(), [])
        payload = policy.to_dict()
        for bad in ({"stage_a": False}, {**policy.hard_gates, "unknown": False}, {**policy.hard_gates, "stage_a": 0}):
            payload["hard_gates"] = bad
            payload["digest"] = policy.digest
            with self.assertRaises((ValueError, TypeError)):
                V4ExecutionPolicy.from_dict(payload)

    def test_digest_is_required_and_tampering_rejected(self):
        payload = build_v4_execution_policy(self._plan(), []).to_dict()
        missing = dict(payload); missing.pop("digest")
        with self.assertRaises(ValueError):
            V4ExecutionPolicy.from_dict(missing)
        payload["execution_mode"] = "sdd_first_changed"
        with self.assertRaises(ValueError):
            V4ExecutionPolicy.from_dict(payload)
