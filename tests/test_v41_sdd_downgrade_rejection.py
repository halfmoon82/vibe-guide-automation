import unittest

from vibe_guide.authorization import build_authorization_card
from tests.test_v41_execution_engine_binding import _plan
from vibe_guide.dag import append_integration_review_node
from vibe_guide.models import AgentCapabilities


class SddDowngradeRejectionTests(unittest.TestCase):
    def test_sdd_serial_override_is_rejected_for_complex_dag(self):
        plan = append_integration_review_node(_plan())
        with self.assertRaises(ValueError):
            build_authorization_card(plan, plan.nodes, AgentCapabilities("codex", False, False, False, False, False, "guide"), execution_engine="sdd", engine_mode="serial", engine_evidence_ref="override")


if __name__ == "__main__":
    unittest.main()
