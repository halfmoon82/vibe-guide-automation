"""A half-filled delivery_evidence is refused at write time and by the gate."""
import json
import unittest

from vibe_guide.evidence import evaluate_delivery_evidence
from vibe_guide.runners.provider_action import TEMPLATE_PLACEHOLDER, validate_result_shape


def result(**evidence):
    base = {"completion_marker": "done-1", "delivery_path": "d/x.json", "thread_status": "complete"}
    base.update(evidence)
    return {"status": "completed", "event": "complete", "delivery_evidence": base}


class PartialTemplateTests(unittest.TestCase):
    def test_each_unfilled_key_is_refused_by_name(self):
        for key in ("completion_marker", "delivery_path", "thread_status"):
            for value in (TEMPLATE_PLACEHOLDER, " <填写> ", "＜填写＞", "x-<填写>"):
                with self.subTest(key=key, value=value):
                    with self.assertRaisesRegex(ValueError, "delivery_evidence\\." + key):
                        validate_result_shape("wait", result(**{key: value}), role="developer")

    def test_delivered_event_is_checked_too(self):
        payload = result(completion_marker=TEMPLATE_PLACEHOLDER)
        payload["event"] = "delivered"
        with self.assertRaisesRegex(ValueError, "delivery_evidence.completion_marker"):
            validate_result_shape("wait", payload, role="developer")

    def test_filled_result_passes(self):
        validate_result_shape("wait", result(), role="developer")

    def test_gate_uses_the_same_judgement(self):
        node = {"status": "DELIVERED"}
        binding = {"task_id": "t", "host": "h", "worktree": "w", "branch": "b", "cursor": "c"}
        ok = evaluate_delivery_evidence(node, binding, result()["delivery_evidence"])
        self.assertTrue(ok.complete)
        for key, reason in (("completion_marker", "completion marker is missing"),
                            ("delivery_path", "delivery path is missing")):
            bad = evaluate_delivery_evidence(
                node, binding, result(**{key: TEMPLATE_PLACEHOLDER})["delivery_evidence"])
            self.assertFalse(bad.complete)
            self.assertIn(reason, bad.reasons)
            self.assertEqual(bad.classification, "acceptance_rejected")


if __name__ == "__main__":
    unittest.main()
