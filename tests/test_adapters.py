import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.adapters.base import Environment
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.adapters.task_provider import (
    BackgroundTaskProvider,
    ProviderUnavailable,
    TaskBinding,
    VisibilityResult,
)


class AdapterTests(unittest.TestCase):
    def test_registry_exposes_all_seven_manifest_adapters(self):
        registry = AdapterRegistry()
        self.assertEqual(
            set(registry.ids),
            {
                "codex",
                "claude-code",
                "cursor",
                "grok",
                "workbuddy",
                "kimi-code",
                "deepseek-harness",
            },
        )

    def test_verified_visible_probes_report_full(self):
        env = Environment(
            commands={"codex": True},
            facts={
                "shell": True,
                "subprocess": True,
                "worktree": True,
                "visible_task.create": True,
                "visible_task.enter": True,
                "visible_task.resume": True,
                "visible_task.wait": True,
            },
        )
        result = AdapterRegistry().get("codex").detect(env)
        self.assertTrue(result.detected)
        self.assertEqual(result.capabilities.level, "full")
        self.assertEqual(result.capabilities.mode, "visible")
        self.assertIsNotNone(result.capabilities.provider)

    def test_no_visible_bridge_is_explicit_background_downgrade(self):
        env = Environment(
            commands={"cursor": True},
            facts={"shell": True, "subprocess": True, "worktree": True},
        )
        result = AdapterRegistry().get("cursor").detect(env)
        self.assertTrue(result.detected)
        self.assertEqual(result.capabilities.level, "background")
        self.assertEqual(result.capabilities.mode, "background")
        self.assertFalse(result.capabilities.visible_automation)
        self.assertIn("不可见", result.capabilities.limitations)
        self.assertIn("不可直接进入", result.capabilities.limitations)

    def test_no_subprocess_is_guide(self):
        result = AdapterRegistry().get("grok").detect(Environment(commands={"grok": True}))
        self.assertTrue(result.detected)
        self.assertEqual(result.capabilities.level, "guide")
        self.assertEqual(result.capabilities.mode, "guide")

    def test_prompts_are_short_and_product_manager_readable(self):
        adapter = AdapterRegistry().get("codex")
        prompt = adapter.session_prompt("启动监工", "plan-7")
        self.assertLessEqual(len(prompt), 120)
        self.assertIn("启动监工", prompt)
        self.assertIn("plan-7", prompt)
        self.assertNotIn("create_thread", prompt)

    def test_monitor_command_is_stable_local_cli_command(self):
        command = AdapterRegistry().get("codex").monitor_command("plan-7", True)
        self.assertEqual(command, ["vibe", "monitor", "--plan", "plan-7", "--json"])

    def test_background_provider_discloses_limits_and_never_claims_visibility(self):
        provider = BackgroundTaskProvider("cursor")
        binding = provider.create("developer", "ISSUE-1", Path("contract.md"))
        self.assertIsInstance(binding, TaskBinding)
        self.assertEqual(binding.mode, "background")
        self.assertFalse(binding.visible)
        self.assertIsNone(binding.task_id)
        self.assertIn("不可见", binding.limitations)
        self.assertIn("不可直接进入", binding.limitations)

    def test_visible_provider_requires_verified_bridge(self):
        provider = AdapterRegistry().get("codex").task_provider
        with self.assertRaises(ProviderUnavailable):
            provider.create("developer", "ISSUE-1", Path("contract.md"))

    def test_codex_provider_maps_verified_thread_bridge_fields(self):
        class Bridge:
            def create_thread(self, role, issue_id, contract_path):
                return {"threadId": "thread-1", "hostId": "host-1", "cursor": "c0"}

        provider = AdapterRegistry().get("codex").task_provider
        provider.bridge = Bridge()
        binding = provider.create("reviewer", "ISSUE-2", Path("contract.md"))
        self.assertEqual(binding.provider, "codex-thread")
        self.assertEqual(binding.task_id, "thread-1")
        self.assertEqual(binding.host, "host-1")
        self.assertEqual(binding.thread_id, "thread-1")
        self.assertEqual(binding.host_id, "host-1")
        self.assertTrue(binding.visible)

    def test_visibility_returns_platform_neutral_result(self):
        result = BackgroundTaskProvider("cursor").visibility(
            BackgroundTaskProvider("cursor").create("developer", "ISSUE-3", Path("contract.md"))
        )
        self.assertIsInstance(result, VisibilityResult)
        self.assertFalse(result.visible)
        self.assertFalse(result.direct_enter)

    def test_capability_report_has_stable_bridge_fields(self):
        env = Environment(
            commands={"codex": True},
            facts={"shell": True, "subprocess": True, "worktree": True},
        )
        report = AdapterRegistry().get("codex").capability_report(
            env, "plan-1", Path(".vibe/authorization.md")
        )
        self.assertEqual(report["capability_level"], "background")
        self.assertEqual(report["monitor_command"], ["vibe", "monitor", "--plan", "plan-1", "--json"])
        self.assertEqual(report["authorization_card"], ".vibe/authorization.md")

    def test_manifests_are_machine_readable_and_have_probe_contract(self):
        root = Path(__file__).parent.parent / "vibe_guide" / "adapters" / "manifests"
        for path in sorted(root.glob("*.yaml")):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertRegex(data["id"], r"^[a-z0-9-]+$")
            self.assertTrue(data["probes"])
            self.assertIn("session_prompt", data)
            self.assertIn("provider", data)
            self.assertIn("background_fallback", data)


if __name__ == "__main__":
    unittest.main()
