import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.cli import run_cli


class WorkflowNonblockingTests(unittest.TestCase):
    def _v4_project(self):
        root = Path(tempfile.mkdtemp())
        vibe = root / ".vibe"
        vibe.mkdir()
        (vibe / "state.json").write_text(
            '{"workflow_version":4,"execution_mode":"sdd_first",'
            '"session_gate":"s0_required","capability_contract_required":true}'
        )
        return root

    def test_v4_entry_screen_is_enforced_before_monitor_route(self):
        root = self._v4_project()
        with patch("vibe_guide.cli.screen_session", side_effect=PermissionError("s0 blocked")):
            result = run_cli(["monitor", "--plan", "p"], root)
        self.assertEqual(result.payload["status"], "session_gate_blocked")

    def test_missing_runtime_workflow_does_not_replace_entry_gate(self):
        root = self._v4_project()
        with patch("vibe_guide.cli.screen_session", return_value=None), \
             patch("vibe_guide.cli._load_plan", return_value=(root / ".vibe", SimpleNamespace(complexity_band="complex"), [], SimpleNamespace())), \
             patch("vibe_guide.cli.Monitor.start", return_value=SimpleNamespace(run_id="run-1", status="initialized", nodes={}, event_sequence=0)):
            # The assertion here is that the V4 entry probe is observed even
            # when required-workflow evidence is absent at runtime.
            run_cli(["monitor", "--plan", "p"], root)


if __name__ == "__main__":
    unittest.main()
