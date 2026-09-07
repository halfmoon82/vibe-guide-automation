import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.adapters.task_provider import TaskProviderAdapter
from vibe_guide.cli import run_cli
from vibe_guide.models import DAGNode, Plan
from vibe_guide.runners.local import LocalRunner


class V42NoBypassTests(unittest.TestCase):
    def test_cli_complex_monitor_is_the_only_dispatch_path(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / ".vibe" / "plans" / "p"
            directory.mkdir(parents=True)
            (Path(root) / ".vibe" / "state.json").write_text(
                '{"workflow_version": 4, "execution_mode": "sdd_first", "session_gate": "s0_required", "capability_contract_required": true}'
            )
            plan = Plan("p", 1, "prd.md", [], "authorized", complexity_band="complex")
            card = SimpleNamespace()
            snapshot = SimpleNamespace(run_id="run-1", status="initialized", nodes={}, event_sequence=0)
            with patch("vibe_guide.cli._load_plan", return_value=(directory, plan, [], card)), \
                 patch("vibe_guide.cli.authorize", return_value=SimpleNamespace(digest="a" * 64, to_dict=lambda: {})), \
                 patch("vibe_guide.cli.Monitor.start", return_value=snapshot), \
                 patch("vibe_guide.cli.screen_session", return_value=None), \
                 patch("vibe_guide.cli._snapshot_result", wraps=None) as snapshot_result:
                # Avoid provider setup; the assertion is on the public payload.
                snapshot_result.side_effect = lambda command, snap, as_json, continuation="manual": SimpleNamespace(
                    exit_code=0, payload={"dispatcher": "monitor"}, text="", as_json=as_json
                )
                result = run_cli(["monitor", "--plan", "p", "--authorize", "AUTHORIZE", "--json"], Path(root), runner=object())
            self.assertEqual(result.payload["dispatcher"], "monitor")

    def test_direct_complex_runner_is_rejected_before_provider_call(self):
        runner = LocalRunner({"fixture": ["echo", "ok"]})
        request = {
            "complexity_band": "complex",
            "adapter_id": "fixture",
            "command": ["echo", "ok"],
        }
        with self.assertRaisesRegex(PermissionError, "complex_monitor_required"):
            runner.start(request, Path(tempfile.gettempdir()))
        self.assertEqual(runner.start_count, 0)

    def test_sdd_only_override_cannot_start_complex_dag(self):
        adapter = TaskProviderAdapter("codex-app-visible", lambda request: {"ok": True})
        request = {"route": "complex", "execution_mode": "sdd", "provider": "codex-app-visible"}
        with self.assertRaisesRegex(PermissionError, "complex_monitor_required"):
            adapter.start(request)


if __name__ == "__main__":
    unittest.main()
