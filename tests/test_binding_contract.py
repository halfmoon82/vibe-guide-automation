import unittest

from vibe_guide.binding_contract import BindingIntent, BindingProof


class BindingContractTests(unittest.TestCase):
    def intent_data(self):
        return {
            "run_id": "run-1", "plan_id": "plan-1", "plan_revision": 2,
            "node_id": "node-1", "task_id": "task-1", "role": "developer",
            "generation": 1, "writer": "writer-1", "worktree": "/tmp/w",
            "branch": "codex/node-1", "base_sha": "a" * 40,
            "authorization_digest": "b" * 64, "node_contract_digest": "c" * 64,
        }

    def test_minimal_intent_does_not_include_lease_or_cursor(self):
        intent = BindingIntent.from_dict(self.intent_data())
        self.assertNotIn("lease_id", intent.to_dict())
        self.assertNotIn("cursor", intent.to_dict())
        self.assertEqual(len(intent.digest), 64)

    def test_intent_rejects_runtime_fields(self):
        with self.assertRaises(ValueError):
            BindingIntent.from_dict(dict(self.intent_data(), lease_id="lease-1"))

    def test_digest_is_deterministic_and_key_order_independent(self):
        first = BindingIntent.from_dict(self.intent_data())
        second = BindingIntent.from_dict({k: self.intent_data()[k] for k in reversed(self.intent_data())})
        self.assertEqual(first.digest, second.digest)

    def test_proof_refresh_and_match(self):
        intent = BindingIntent.from_dict(self.intent_data())
        proof = BindingProof.from_dict({
            "provider_task_id": "provider-1", "provider_host": "local",
            "lease_id": "lease-1", "cursor": "cursor-1",
            "observed_worktree": "/tmp/w", "observed_branch": "codex/node-1",
            "observed_writer": "writer-1", "observed_role": "developer",
            "observed_generation": 1, "observed_contract_digest": "c" * 64,
        })
        self.assertTrue(proof.matches(intent))
        refreshed = BindingProof.from_dict(dict(proof.to_dict(), cursor="cursor-2"))
        self.assertTrue(refreshed.matches(intent))

    def test_mismatch_is_rejected(self):
        intent = BindingIntent.from_dict(self.intent_data())
        proof = BindingProof.from_dict({
            "provider_task_id": "provider-1", "provider_host": "local",
            "lease_id": "lease-1", "cursor": "cursor-1",
            "observed_worktree": "/other", "observed_branch": "codex/node-1",
            "observed_writer": "writer-1", "observed_role": "developer",
            "observed_generation": 1, "observed_contract_digest": "c" * 64,
        })
        self.assertFalse(proof.matches(intent))


if __name__ == "__main__":
    unittest.main()
