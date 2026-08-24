import json
import unittest
from pathlib import Path

from vibe_guide.models import AgentCapabilities as SharedAgentCapabilities
from vibe_guide.adapters.base import Environment, ManifestError
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.adapters.task_provider import (
    BackgroundTaskProvider,
    CodexAppBridge,
    ProviderUnavailable,
    ProviderPending,
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
            commands={"codex.agent": True},
            facts={
                "codex.shell": True,
                "codex.subprocess": True,
                "codex.worktree": True,
                "codex.visible_task.create": True,
                "codex.visible_task.enter": True,
                "codex.visible_task.resume": True,
                "codex.visible_task.wait": True,
            },
        )
        result = AdapterRegistry().get("codex").detect(env)
        self.assertTrue(result.detected)
        self.assertEqual(result.capabilities.level, "full")
        self.assertEqual(result.capabilities.mode, "visible")
        self.assertIsNotNone(result.capabilities.provider)
        self.assertIsInstance(result.capabilities, SharedAgentCapabilities)

    def test_no_visible_bridge_is_explicit_background_downgrade(self):
        env = Environment(
            commands={"cursor.agent": True},
            facts={"cursor.shell": True, "cursor.subprocess": True, "cursor.worktree": True},
        )
        result = AdapterRegistry().get("cursor").detect(env)
        self.assertTrue(result.detected)
        self.assertEqual(result.capabilities.level, "background")
        self.assertEqual(result.capabilities.mode, "background")
        self.assertFalse(result.capabilities.visible_automation)
        self.assertIn("不可见", result.capabilities.limitations)
        self.assertIn("不可直接进入", result.capabilities.limitations)

    def test_no_subprocess_is_guide(self):
        result = AdapterRegistry().get("grok").detect(
            Environment(commands={"grok.agent": True}, facts={"grok.shell": True})
        )
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
        provider = BackgroundTaskProvider(
            "cursor-background",
            launcher=lambda role, issue_id, contract: {"run_id": "bg-1", "host": "local"},
        )
        binding = provider.create("developer", "ISSUE-1", Path("contract.md"))
        self.assertIsInstance(binding, TaskBinding)
        self.assertEqual(binding.mode, "background")
        self.assertFalse(binding.visible)
        self.assertEqual(binding.task_id, "bg-1")
        self.assertIn("不可见", binding.limitations)
        self.assertIn("不可直接进入", binding.limitations)

    def test_visible_provider_requires_verified_bridge(self):
        provider = AdapterRegistry().get("codex").task_provider
        with self.assertRaises(ProviderUnavailable):
            provider.create("developer", "ISSUE-1", Path("contract.md"))

    def test_codex_provider_maps_verified_thread_bridge_fields(self):
        class Bridge:
            def create_thread(self, request):
                self.request = request
                return {"threadId": "thread-1", "hostId": "host-1", "cursor": "c0"}

        bridge = Bridge()
        provider = AdapterRegistry().get("codex").task_provider
        provider.bridge = CodexAppBridge(
            create_thread=bridge.create_thread,
            navigate_to_codex_page=lambda request: {"ok": True},
            send_message_to_thread=lambda request: {"ok": True},
            wait_threads=lambda request: {"status": "running", "nextCursor": "c1"},
            list_threads=lambda request: {"threads": [{"threadId": "thread-1", "hostId": "host-1"}]},
        )
        binding = provider.create("reviewer", "ISSUE-2", Path("contract.md"))
        self.assertEqual(binding.provider, "codex-app-visible")
        self.assertEqual(binding.task_id, "thread-1")
        self.assertEqual(binding.host, "host-1")
        self.assertEqual(binding.thread_id, "thread-1")
        self.assertEqual(binding.host_id, "host-1")
        self.assertTrue(binding.visible)
        self.assertEqual(bridge.request["prompt"], "请执行 reviewer 任务，计划 ISSUE-2。")
        self.assertIn("target", bridge.request)

    def test_codex_public_tool_shapes_cover_enter_resume_wait_visibility(self):
        calls = []
        bridge = CodexAppBridge(
            create_thread=lambda request: {"threadId": "thread-1", "hostId": "host-1"},
            navigate_to_codex_page=lambda request: calls.append(("navigate", request)),
            send_message_to_thread=lambda request: calls.append(("send", request)),
            wait_threads=lambda request: calls.append(("wait", request)) or {"status": "complete", "nextCursor": "c2"},
            list_threads=lambda request: calls.append(("list", request)) or {"pinnedThreads": [{"threadId": "thread-1", "hostId": "host-1"}], "threads": []},
        )
        provider = AdapterRegistry().get("codex").task_provider
        provider.bridge = bridge
        binding = provider.create("developer", "ISSUE-1", Path("contract.md"))
        provider.enter_or_locate(binding)
        provider.resume(binding, Path("rework.md"))
        update = provider.wait(binding, "c1")
        visibility = provider.visibility(binding)
        self.assertEqual(calls[0], ("navigate", {"threadId": "thread-1"}))
        self.assertEqual(calls[1][0], "send")
        self.assertEqual(calls[1][1]["threadId"], "thread-1")
        self.assertEqual(calls[2], ("wait", {"targets": [{"threadId": "thread-1", "hostId": "host-1", "afterCursor": "c1"}], "timeoutMs": 120000}))
        self.assertEqual(calls[3], ("list", {"limit": 100}))
        self.assertEqual(update.cursor, "c2")
        self.assertTrue(visibility.visible)

    def test_codex_pending_client_thread_id_is_not_thread_id(self):
        provider = AdapterRegistry().get("codex").task_provider
        provider.bridge = CodexAppBridge(
            create_thread=lambda request: {"clientThreadId": "client-1"},
            navigate_to_codex_page=lambda request: None,
            send_message_to_thread=lambda request: None,
            wait_threads=lambda request: None,
            list_threads=lambda request: {"threads": []},
        )
        binding = provider.create("developer", "ISSUE-1", Path("contract.md"))
        self.assertIsNone(binding.task_id)
        self.assertEqual(binding.client_thread_id, "client-1")
        self.assertNotIn("threadId", binding.to_dict())
        self.assertEqual(binding.to_dict()["client_thread_id"], "client-1")
        with self.assertRaises(ProviderPending):
            provider.enter_or_locate(binding)

    def test_codex_pending_binding_can_be_resolved_from_visible_list(self):
        bridge = CodexAppBridge(
            create_thread=lambda request: {"clientThreadId": "client-1"},
            navigate_to_codex_page=lambda request: None,
            send_message_to_thread=lambda request: None,
            wait_threads=lambda request: None,
            list_threads=lambda request: {"threads": [{"clientThreadId": "client-1", "threadId": "thread-2", "hostId": "host-2"}]},
        )
        provider = AdapterRegistry().get("codex").task_provider
        provider.bridge = bridge
        binding = provider.create("developer", "ISSUE-1", Path("contract.md"))
        resolved = provider.resolve_pending(binding)
        self.assertEqual(resolved.task_id, "thread-2")
        self.assertEqual(resolved.host, "host-2")
        self.assertIsNone(resolved.client_thread_id)

    def test_visibility_returns_platform_neutral_result(self):
        provider = BackgroundTaskProvider(
            "cursor-background", launcher=lambda *args: {"run_id": "bg-3"}
        )
        result = provider.visibility(
            provider.create("developer", "ISSUE-3", Path("contract.md"))
        )
        self.assertIsInstance(result, VisibilityResult)
        self.assertFalse(result.visible)
        self.assertFalse(result.direct_enter)

    def test_capability_report_has_stable_bridge_fields(self):
        env = Environment(
            commands={"codex.agent": True},
            facts={"codex.shell": True, "codex.subprocess": True, "codex.worktree": True},
        )
        report = AdapterRegistry().get("codex").capability_report(
            env, "plan-1", Path(".vibe/authorization.md")
        )
        self.assertEqual(report["capability_level"], "background")
        self.assertEqual(report["provider"], report["capabilities"]["provider"])
        self.assertEqual(report["mode"], report["capabilities"]["mode"])
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

    def test_non_boolean_or_unscoped_facts_cannot_promote(self):
        with self.assertRaises(ValueError):
            AdapterRegistry().get("codex").detect(
                Environment(
                    commands={"codex.agent": True},
                    facts={
                        "codex.shell": "false",
                        "codex.subprocess": "false",
                        "codex.worktree": "false",
                        "codex.visible_task.create": "false",
                        "codex.visible_task.enter": "false",
                        "codex.visible_task.resume": "false",
                        "codex.visible_task.wait": "false",
                    },
                )
            )
        result = AdapterRegistry().get("cursor").detect(
            Environment(
                commands={"cursor.agent": True, "codex.agent": True},
                facts={
                    "codex.shell": True,
                    "codex.subprocess": True,
                    "codex.worktree": True,
                    "codex.visible_task.create": True,
                    "codex.visible_task.enter": True,
                    "codex.visible_task.resume": True,
                    "codex.visible_task.wait": True,
                },
            )
        )
        self.assertNotEqual(result.capabilities.level, "full")

    def test_manifest_schema_rejects_missing_invalid_and_duplicate(self):
        root = Path(__file__).parent.parent / "vibe_guide" / "adapters" / "manifests"
        with self.assertRaises(ManifestError):
            AdapterRegistry(root / "missing-manifest-dir")
        with self.assertRaises(ManifestError):
            AdapterRegistry.from_manifests([{"id": "only-id"}])
        with self.assertRaises(ManifestError):
            AdapterRegistry.from_manifests([
                AdapterRegistry().get("codex").manifest,
                dict(AdapterRegistry().get("codex").manifest),
            ])

    def test_background_launcher_failure_is_fail_closed_and_guide_has_no_provider(self):
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("cursor-background", launcher=lambda *args: None).create(
                "developer", "ISSUE-1", Path("contract.md")
            )
        adapter = AdapterRegistry().get("grok")
        result = adapter.detect(Environment(commands={"grok.agent": True}))
        self.assertEqual(result.capabilities.mode, "guide")
        self.assertIsNone(adapter.provider_for(result.capabilities))

    def test_background_launcher_object_is_called_and_requires_handle(self):
        class Launcher:
            def launch(self, role, issue_id, contract_path):
                self.args = role, issue_id, contract_path
                return {"handle": "bg-4"}

        launcher = Launcher()
        binding = BackgroundTaskProvider("grok-background", launcher=launcher).create(
            "reviewer", "ISSUE-4", Path("contract.md")
        )
        self.assertEqual(binding.task_id, "bg-4")
        self.assertEqual(launcher.args[1], "ISSUE-4")

    def test_provider_rejects_launcher_binding_with_wrong_identity(self):
        provider = BackgroundTaskProvider(
            "grok-background",
            launcher=lambda *args: TaskBinding("other", "visible", "developer", "ISSUE-5", task_id="x"),
        )
        with self.assertRaises(ProviderUnavailable):
            provider.create("developer", "ISSUE-5", Path("contract.md"))

    def test_manifest_id_and_prompt_template_types_are_strict(self):
        manifest = dict(AdapterRegistry().get("codex").manifest)
        manifest["id"] = "Not Valid"
        with self.assertRaises(ManifestError):
            AdapterRegistry.from_manifests([manifest])
        manifest = dict(AdapterRegistry().get("codex").manifest)
        manifest["session_prompt"] = "{unknown}"
        with self.assertRaises(ManifestError):
            AdapterRegistry.from_manifests([manifest])

    def test_manifest_prompt_template_is_rendered(self):
        adapter = AdapterRegistry().get("codex")
        self.assertEqual(adapter.session_prompt("启动监工", "plan-7"), "请启动监工，计划 plan-7。")


if __name__ == "__main__":
    unittest.main()
