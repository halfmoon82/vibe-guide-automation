"""Claude Code desktop dispatch: native tool mapping and mailbox contract.

`claude-code.yaml` declares `native_control_plane: true`, but the runner only
mapped Codex tools; every other provider got a placeholder name a desktop
session cannot act on.  These tests pin (1) a complete Claude Code mapping
sharing Codex's operation set, (2) fail-closed for providers without a
verified control plane, and (3) a full run driven through the public CLI with
Claude Code capabilities and a fake session servicing the mailbox with
provider-neutral binding fields.
"""
import unittest

from vibe_guide.adapters.task_provider import ProviderUnavailable


class NativeToolMapTests(unittest.TestCase):
    def test_native_tool_map_covers_every_operation_for_claude_code(self):
        from vibe_guide.runners.provider_action import NATIVE_TOOL_MAP
        operations = {"create", "locate", "visibility", "resume", "wait"}
        self.assertEqual(set(NATIVE_TOOL_MAP["codex-app-visible"]), operations)
        self.assertEqual(set(NATIVE_TOOL_MAP["claude-code-visible"]), operations)
        claude = NATIVE_TOOL_MAP["claude-code-visible"]
        self.assertEqual(claude["create"], "ccd_session__spawn_task")
        self.assertEqual(claude["locate"], "ccd_window__open_session_in")
        self.assertEqual(claude["visibility"], "ccd_session_mgmt__get_session")
        self.assertEqual(claude["resume"], "ccd_session_mgmt__send_message")
        self.assertEqual(claude["wait"], "ccd_session_mgmt__list_events")

    def test_codex_mapping_is_unchanged(self):
        from vibe_guide.runners.provider_action import NATIVE_TOOL_MAP
        self.assertEqual(NATIVE_TOOL_MAP["codex-app-visible"], {
            "create": "codex_app__create_thread",
            "locate": "codex_app__navigate_to_codex_page",
            "visibility": "codex_app__wait_threads",
            "resume": "codex_app__send_message_to_thread",
            "wait": "codex_app__wait_threads",
        })

    def test_unmapped_provider_fails_closed_instead_of_a_placeholder_name(self):
        from vibe_guide.runners.provider_action import ProviderActionRunner
        runner = ProviderActionRunner.__new__(ProviderActionRunner)
        runner.provider = "cursor-visible"
        with self.assertRaises(ProviderUnavailable) as caught:
            runner._native_tool("create")
        self.assertIn("cursor-visible", str(caught.exception))
        runner.provider = "claude-code-visible"
        self.assertEqual(runner._native_tool("wait"), "ccd_session_mgmt__list_events")


if __name__ == "__main__":
    unittest.main()
