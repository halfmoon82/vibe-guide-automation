import unittest

from vibe_guide.adapters.base import Environment
from vibe_guide.adapters.registry import AdapterRegistry


class NativeDispatchTests(unittest.TestCase):
    def test_non_codex_never_falls_back_to_background(self):
        env = Environment(commands={"claude-code.agent": True}, facts={
            "claude-code.shell": True, "claude-code.subprocess": True,
            "claude-code.worktree": True,
        })
        adapter = AdapterRegistry(background_launchers={"claude-code": lambda: {}}).get("claude-code")
        caps = adapter.detect(env).capabilities
        self.assertEqual(caps.mode, "guide")

    def test_agent_presence_alone_cannot_verify_codex_native_dispatch(self):
        env = Environment(commands={"codex.agent": True}, facts={
            "codex.shell": True, "codex.subprocess": True, "codex.worktree": True,
        })
        caps = AdapterRegistry().get("codex").detect(env).capabilities
        self.assertEqual(caps.mode, "guide")

    def test_codex_native_missing_cannot_fall_back_to_background(self):
        env = Environment(commands={"codex.agent": True}, facts={
            "codex.shell": True, "codex.subprocess": True, "codex.worktree": True,
        })
        adapter = AdapterRegistry(background_launchers={"codex": lambda: {}}).get("codex")
        self.assertEqual(adapter.detect(env).capabilities.mode, "guide")

    def test_claude_code_visible_after_observed_native_lifecycle_facts(self):
        """Observed lifecycle facts, not the adapter id, decide visibility."""
        env = Environment(commands={"claude-code.agent": True}, facts={
            "claude-code.shell": True, "claude-code.subprocess": True,
            "claude-code.worktree": True, "claude-code.visible_task.create": True,
            "claude-code.visible_task.enter": True,
            "claude-code.visible_task.resume": True,
            "claude-code.visible_task.wait": True,
        })
        caps = AdapterRegistry().get("claude-code").detect(env).capabilities
        self.assertEqual(caps.mode, "visible")
        self.assertEqual(caps.provider, "claude-code-visible")
        self.assertTrue(caps.create_task and caps.enter_task and caps.resume_task and caps.wait_task)

    def test_claude_code_manifest_declares_native_control_plane(self):
        """The manifest, not an adapter id comparison, carries the declaration."""
        manifest = AdapterRegistry().get("claude-code").manifest
        self.assertIs(manifest["native_control_plane"], True)


if __name__ == "__main__":
    unittest.main()
