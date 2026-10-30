"""worker-deliver checks the integration reviewer's claim shape on the spot."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.paths import ProjectPaths

SECRET = "REVIEWER-SECRET-VALUE"
DELIVERY = {
    "completion_marker": "DELIVERY_COMPLETE",
    "delivery_path": ".vibe/runs/run-1/review.md",
    "thread_status": "completed",
}
VERDICT = {"status": "ok", "evidence": "checked"}


def claim(**overrides):
    value = {
        "findings": [],
        "iteration_compatibility": dict(VERDICT),
        "test_runtime_delivery": dict(VERDICT),
        "out_of_scope": [],
    }
    value.update(overrides)
    return value


class IntegrationDeliveryGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = ProjectPaths(Path(temporary.name))
        self.store = ProviderActionStore(self.paths)
        patcher = mock.patch.object(ProviderActionStore, "_latest_generation", return_value=1)
        patcher.start()
        self.addCleanup(patcher.stop)

    def deliver(self, node, role, evidence, **extra):
        payload = {"delivery_evidence": dict(DELIVERY), "evidence": evidence, **extra}
        return self.store.record_worker_delivery("run-1", node, role, payload, 1)

    def archived(self):
        return sorted(
            p for p in self.paths.vibe.rglob("*")
            if p.is_file() and p.parent.name == "deliveries"
        )

    def test_missing_out_of_scope_is_rejected_without_a_file(self):
        bad = claim()
        del bad["out_of_scope"]
        with self.assertRaises(ValueError) as caught:
            self.deliver("integration-review", "reviewer", bad, event="accepted")
        self.assertIn("out_of_scope", str(caught.exception))
        self.assertEqual(self.archived(), [])

    def test_complete_claim_is_recorded(self):
        outcome = self.deliver("integration-review", "reviewer", claim(), event="accepted")
        self.assertTrue(outcome["recorded"])
        self.assertEqual(len(self.archived()), 1)

    def test_wrong_types_and_empty_evidence_are_rejected(self):
        for bad in (
            claim(findings="none"),
            claim(out_of_scope={}),
            claim(iteration_compatibility="ok"),
            claim(test_runtime_delivery={"status": "ok", "evidence": "  "}),
            claim(test_runtime_delivery={"status": "ok", "evidence": 3}),
            "all clean",
            None,
        ):
            with self.subTest(bad=repr(bad)[:40]):
                with self.assertRaises(ValueError):
                    self.deliver("integration-review", "reviewer", bad, event="accepted")
        self.assertEqual(self.archived(), [])

    def test_extra_key_is_rejected_without_echoing_its_name_or_value(self):
        with self.assertRaises(ValueError) as caught:
            self.deliver("integration-review", "reviewer", claim(**{SECRET: SECRET}), event="accepted")
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertEqual(self.archived(), [])

    def test_other_nodes_and_developers_keep_the_existing_checks(self):
        self.assertTrue(self.deliver("n1", "reviewer", "free text")["recorded"])
        self.assertTrue(self.deliver("integration-review", "developer", "free text")["recorded"])

    def test_only_the_accepted_claim_is_gated(self):
        for event in ("review_finding", "delivered", "complete"):
            with self.subTest(event=event):
                self.assertTrue(
                    self.deliver("integration-review", "reviewer", "x", event=event)["recorded"]
                )


if __name__ == "__main__":
    unittest.main()
