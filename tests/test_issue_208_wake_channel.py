"""ISSUE-208: dispatch names a concrete wake channel and signal format.

Acceptance anchors: a Codex supervisor address carries the push tool, the
supervisor thread and a filled-in signal; Claude Code and unknown hosts carry
``channel: null`` with a reason; dispatch text points at those fields instead
of an unnamed "authorized channel".
"""
import tempfile
import unittest
from pathlib import Path

from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths
from vibe_guide.supervisor import (
    current_supervisor_address,
    register_supervisor_address,
    wake_signal,
)


class WakeInstructionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.paths = ProjectPaths(Path(self.tmp.name))

    def _register(self, provider):
        register_supervisor_address(
            self.paths, "run-1", {"provider": provider, "session_id": "S", "host": "mac"}
        )

    def test_codex_address_carries_channel_target_and_signal(self):
        for provider in ("codex", "codex-app-visible"):
            with self.subTest(provider=provider):
                self._register(provider)
                wake = current_supervisor_address(
                    self.paths, "run-1", node_id="n1", role="developer", generation=2
                )["wake"]
                self.assertEqual(wake["channel"], "send_message_to_thread")
                self.assertEqual(wake["target"], "S")
                self.assertEqual(
                    wake["signal"], "vibe-wake run=run-1 node=n1 role=developer generation=2"
                )

    def test_claude_code_wakes_through_send_message(self):
        for provider in ("claude-code", "claude-code-visible"):
            with self.subTest(provider=provider):
                self._register(provider)
                wake = current_supervisor_address(
                    self.paths, "run-1", node_id="n1", role="developer", generation=2
                )["wake"]
                self.assertEqual(wake["channel"], "SendMessage")
                self.assertEqual(wake["target"], "S")
                self.assertEqual(
                    wake["signal"], "vibe-wake run=run-1 node=n1 role=developer generation=2"
                )

    def test_unknown_provider_has_no_channel(self):
        self._register("someday-host")
        wake = current_supervisor_address(self.paths, "run-1")["wake"]
        self.assertIsNone(wake["channel"])
        self.assertIn("someday-host", wake["reason"])

    def test_signal_without_node_keeps_placeholders(self):
        self.assertEqual(
            wake_signal("run-1"),
            "vibe-wake run=run-1 node=<node> role=<role> generation=<generation>",
        )

    def test_unregistered_address_has_no_wake(self):
        result = current_supervisor_address(self.paths, "run-1")
        self.assertEqual(result["status"], "unknown")
        self.assertNotIn("wake", result)


class WakeCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_address_query_fills_signal_from_node_args(self):
        run_cli(["supervisor-register", "--run-id", "run-1", "--provider", "codex",
                 "--session-id", "T", "--host", "mac", "--json"], self.root)
        result = run_cli(["supervisor-address", "--run-id", "run-1", "--node", "n1",
                          "--role", "reviewer", "--generation", "3", "--json"], self.root)
        self.assertEqual(result.exit_code, 0, result.payload)
        self.assertEqual(
            result.payload["wake"]["signal"],
            "vibe-wake run=run-1 node=n1 role=reviewer generation=3",
        )


class DispatchTextTests(unittest.TestCase):
    def _create_prompt(self, role):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        with tempfile.TemporaryDirectory() as d:
            runner = ProviderActionRunner(ProjectPaths(Path(d)), "codex", "codex-app-visible")
            seen = []

            def fake(contract, run_id, operation, request):
                seen.append((operation, request))
                return {"create": {"binding": {"task_id": "t", "host": "mac"}},
                        "locate": {"located": True},
                        "visibility": {"visible": True, "direct_enter": True}}[operation]

            runner._require_result = fake
            runner.task_binding({"node_id": "n1", "role": role, "generation": 5,
                                 "project_id": "p", "topology": "dual-visible",
                                 "worktree": "w", "branch": "b", "files": []},
                                Path(d), "run-1", "running")
            return dict(seen)["create"]["prompt"]

    def test_create_and_continuation_name_the_wake_fields(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner

        prompts = {
            "create": self._create_prompt("developer"),
            "continuation": ProviderActionRunner._continuation_prompt(
                {"node_id": "n1", "role": "developer", "generation": 5, "run_id": "run-1"}),
        }
        for name, prompt in prompts.items():
            with self.subTest(prompt=name):
                for required in (
                    "supervisor-address --run-id run-1 --node n1 --role developer --generation 5 --json",
                    "wake.channel", "wake.target", "wake.signal", "心跳",
                ):
                    self.assertIn(required, prompt)
                self.assertNotIn("已授权通道", prompt)
                self.assertNotIn("固定格式唤醒信号", prompt)


if __name__ == "__main__":
    unittest.main()
