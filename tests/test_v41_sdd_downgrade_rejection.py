import unittest
import json
import tempfile
from pathlib import Path

from vibe_guide.authorization import build_authorization_card
from tests.test_v41_execution_engine_binding import _plan
from vibe_guide.dag import append_integration_review_node
from vibe_guide.models import AgentCapabilities
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.paths import ProjectPaths
from vibe_guide.workflow_gate import require_entry


class SddDowngradeRejectionTests(unittest.TestCase):
    def test_legacy_v2_entry_remains_compatible_until_complex_monitor_gate(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            paths.vibe.mkdir()
            (paths.vibe / "state.json").write_text(
                json.dumps({"workflow_version": 2, "session_gate": "s0_required"})
            )
            save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))

            # The shared entry gate still supports legacy V2 sessions for
            # backward-compatible diagnostics; the complex Monitor dispatcher
            # performs the stronger V4.2 SDD-first gate after plan loading.
            require_entry(paths, "entry", "monitor")

    def test_sdd_serial_override_is_rejected_for_complex_dag(self):
        plan = append_integration_review_node(_plan())
        with self.assertRaises(ValueError):
            build_authorization_card(plan, plan.nodes, AgentCapabilities("codex", False, False, False, False, False, "guide"), execution_engine="sdd", engine_mode="serial", engine_evidence_ref="override")


if __name__ == "__main__":
    unittest.main()
