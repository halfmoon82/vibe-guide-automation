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


import json
import shutil
import tempfile
from pathlib import Path

from vibe_guide.adapters.task_provider import ProviderActionStore
from vibe_guide.cli import run_cli
from vibe_guide.paths import ProjectPaths

E2E_FIXTURE = Path(__file__).parent / "fixtures" / "e2e-project"
# Tools a happy-path run necessarily exercises: create, locate, visibility,
# wait.  ``resume`` (send_message) only fires on continuation/rework and is
# pinned by the mapping test above.
CLAUDE_HAPPY_PATH_TOOLS = {
    "ccd_session__spawn_task", "ccd_window__open_session_in",
    "ccd_session_mgmt__get_session", "ccd_session_mgmt__list_events",
}


class ClaudeCodeMailboxRunTests(unittest.TestCase):
    """Public CLI + Claude Code capabilities + a fake session servicing the mailbox."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        shutil.copytree(E2E_FIXTURE, self.root)
        self.addCleanup(self.temporary.cleanup)

    def cli(self, argv):
        return run_cli(argv + ["--json"], self.root)

    def test_run_completes_with_claude_code_tools_and_neutral_binding_fields(self):
        self.assertEqual(self.cli(["init", "--confirm"]).exit_code, 0)
        store = ProviderActionStore(ProjectPaths(self.root))
        store.publish_capabilities(
            "claude-code",
            {
                "claude-code.shell": True, "claude-code.subprocess": True, "claude-code.worktree": True,
                "claude-code.visible_task.create": True, "claude-code.visible_task.enter": True,
                "claude-code.visible_task.resume": True, "claude-code.visible_task.wait": True,
            },
            provenance="claude-code desktop session: ccd tools observed",
            project_id="project-fixture",
        )
        source = json.loads((self.root / "plan-source.json").read_text(encoding="utf-8"))
        source["capabilities"]["agent_id"] = "claude-code"
        source["active_pair_limit"] = 1
        source["nodes"] = [source["nodes"][0]]
        source["nodes"][0]["contract"].update({"adapter_id": "claude-code", "project_id": "project-fixture", "host": "mac"})
        for key in ("command", "provider", "mode"):
            source["nodes"][0]["contract"].pop(key, None)
        (self.root / "claude-plan.json").write_text(json.dumps(source), encoding="utf-8")
        planned = self.cli(["plan", "--request", "设计并实现两个契约兼容的并行节点并完成独立审查", "--plan-id", "claude-plan", "--s1", "4,4,4,4,4", "--node-spec", "claude-plan.json"])
        self.assertEqual(planned.exit_code, 0, planned.text)

        started = self.cli(["monitor", "--plan", "claude-plan", "--authorize", "AUTHORIZE"])
        self.assertEqual(started.exit_code, 4, started.text)

        observed_tools = set()
        waits = {"developer": 0, "reviewer": 0}
        result = started
        for _ in range(40):
            for action in store.pending():
                observed_tools.add(action["native_tool"])
                role, operation = action["role"], action["operation"]
                session_id = "local_" + role + "_session"
                if operation == "create":
                    payload = {"binding": {"task_id": session_id, "host": "mac"}}
                elif operation == "locate":
                    payload = {"located": True}
                elif operation == "visibility":
                    payload = {"visible": True, "direct_enter": True}
                elif operation == "resume":
                    payload = {"resumed": True}
                elif operation == "wait":
                    waits[role] += 1
                    if role == "developer" and waits[role] == 1:
                        payload = {"status": "timeout", "cursor": "uuid-dev-1"}
                    elif role == "developer":
                        payload = {"status": "completed", "cursor": "uuid-dev-2", "event": "complete", "evidence": "verified-developer"}
                    else:
                        payload = {"status": "completed", "cursor": "uuid-rev-1", "event": "accepted", "evidence": "verified-reviewer"}
                else:
                    self.fail("unexpected provider action: %r" % action)
                store.complete(action["action_id"], payload)
            result = self.cli(["resume", "--plan", "claude-plan"])
            if result.payload.get("status") == "complete":
                break
        self.assertEqual((result.exit_code, result.payload.get("status")), (0, "complete"), result.text)
        self.assertTrue(CLAUDE_HAPPY_PATH_TOOLS.issubset(observed_tools), observed_tools)
        self.assertFalse(any(tool.startswith("codex_app__") for tool in observed_tools), observed_tools)
        tasks = json.loads((self.root / ".vibe" / "runs" / result.payload["run_id"] / "tasks.json").read_text(encoding="utf-8"))
        bindings = tasks if isinstance(tasks, list) else list(tasks.values())
        flat = json.dumps(bindings, ensure_ascii=False)
        self.assertIn("claude-code-visible", flat)
        self.assertIn("local_developer_session", flat)
        self.assertIn("local_reviewer_session", flat)
