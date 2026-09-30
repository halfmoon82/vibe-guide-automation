"""ISSUE-92 stage 2: review methodology section inside visible-sdd protocol.

Anchors: the severity mapping table must contain a Refutation row and the
rule that an unrefuted claim never counts into P0-P2 clearance.
"""
import unittest

from vibe_guide.protocols import load_protocol

PROTOCOL_NAME = "visible-sdd-worker"


def _text():
    return load_protocol(PROTOCOL_NAME)


class MethodologySectionTests(unittest.TestCase):
    def setUp(self):
        self.text = _text()

    def test_claim_lifecycle_order(self):
        """约定 -> 可行执行 -> 反驳后才成 finding, in stated order."""
        for token in ("约定", "可行执行", "反驳", "finding"):
            self.assertIn(token, self.text)
        claim = self.text.find("约定")
        execute = self.text.find("可行执行")
        refute = self.text.find("反驳")
        self.assertLess(claim, execute)
        self.assertLess(execute, refute)

    def test_node_contract_is_the_intent(self):
        self.assertIn("节点合同", self.text)
        self.assertIn("intent", self.text.lower())

    def test_unrefuted_never_counts_into_clearance(self):
        """The acceptance anchor line: unrefuted claims do not count P0-P2."""
        self.assertIn("未完成反驳不得计入 P0–P2", self.text)

    def test_unknown_goes_blocked_unknown(self):
        self.assertIn("blocked_unknown", self.text)

    def test_fan_out_capped_at_one_level(self):
        self.assertIn("一层", self.text)
        self.assertIn("fan-out", self.text.lower())

    def test_severity_mapping_table_has_refutation_row(self):
        """The mapping table anchors a row whose mechanism is Refutation."""
        lines = [l for l in self.text.splitlines() if "Refutation" in l]
        self.assertTrue(lines, "mapping table must name Refutation")
        self.assertTrue(
            any(l.strip().startswith("|") for l in lines),
            "Refutation must appear inside a table row",
        )

    def test_reference_skill_is_optional_and_hard_rules_continue(self):
        self.assertIn(".vibe/proposals/skills/", self.text)
        self.assertIn("读不到", self.text)


class DeliveryContractUntouchedTests(unittest.TestCase):
    """§5 delivery fields and the protocol pointer must not drift."""

    def test_delivery_fields_unchanged(self):
        text = _text()
        for token in (
            'in_session_review',
            'delivery_evidence',
            "vibe_guide/protocols/visible-sdd-worker.md",
            'clearance',
            'evidence_ref',
        ):
            self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
