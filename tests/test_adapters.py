import json
import unittest
from pathlib import Path

from vibe_guide.models import AgentCapabilities as SharedAgentCapabilities
from vibe_guide.adapters.base import Environment, ManifestError
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.adapters.task_provider import (
    BackgroundTaskProvider,
    CodexAppBridge,
    ProviderPending,
    ProviderUnavailable,
    RepositoryTaskRouting,
    TaskBinding,
    VisibilityResult,
)


SUPPORTED = {
    "codex", "claude-code", "cursor", "grok", "workbuddy", "kimi-code",
    "deepseek-harness",
}


def routing(environment="worktree"):
    return RepositoryTaskRouting(
        project_id="project-1",
        host_id="host-1",
        environment=environment,
        worktree="/repo-wt" if environment == "worktree" else "/repo",
        branch="codex/issue" if environment == "worktree" else "main",
    )


def codex_bridge(**overrides):
    calls = []
    functions = {
        "create_thread": lambda request: {"threadId": "thread-1", "hostId": "host-1"},
        "navigate_to_codex_page": lambda request: calls.append(("navigate", request)),
        "send_message_to_thread": lambda request: calls.append(("send", request)),
        "wait_threads": lambda request: {
            "timedOut": False,
            "wake": "completed",
            "polls": [{
                "threadId": "thread-1", "hostId": "host-1", "cursor": "c2",
                "thread": {"status": "complete"},
            }],
        },
        "list_threads": lambda request: {
            "pinnedThreads": [{"id": "thread-1", "hostId": "host-1"}],
            "threads": [],
        },
    }
    functions.update(overrides)
    return CodexAppBridge(**functions), calls


def routed_codex_provider(bridge=None, environment="worktree"):
    provider = AdapterRegistry().get("codex").task_provider
    provider.bridge = bridge or codex_bridge()[0]
    provider.routing = routing(environment)
    return provider


def background_result(role="developer", issue_id="N4"):
    return {
        "handle": "bg-1",
        "provider": "cursor-background",
        "mode": "background",
        "host": "local",
        "role": role,
        "issue_id": issue_id,
        "worktree": "/repo-wt",
        "branch": "codex/n4",
        "status_file": "status.txt",
        "handoff_file": "handoff.md",
    }


class AdapterTests(unittest.TestCase):
    def test_production_registry_exposes_exact_seven(self):
        self.assertEqual(set(AdapterRegistry().ids), SUPPORTED)

    def test_production_registry_rejects_any_missing_supported_id(self):
        registry = AdapterRegistry()
        manifests = [registry.get(name).manifest for name in registry.ids]
        for missing in SUPPORTED:
            partial = [item for item in manifests if item["id"] != missing]
            with self.subTest(missing=missing), self.assertRaises(ManifestError):
                AdapterRegistry.from_manifests(partial)
        self.assertEqual(set(AdapterRegistry.from_manifests(manifests).ids), SUPPORTED)
        self.assertEqual(AdapterRegistry.custom_from_manifests([manifests[0]]).ids, (manifests[0]["id"],))

    def test_manifest_schema_rejects_empty_duplicate_and_invalid(self):
        with self.assertRaises(ManifestError):
            AdapterRegistry(Path("missing-manifest-dir"))
        codex = AdapterRegistry().get("codex").manifest
        with self.assertRaises(ManifestError):
            AdapterRegistry.custom_from_manifests([codex, dict(codex)])
        invalid = dict(codex); invalid["session_prompt"] = "{private_api}"
        with self.assertRaises(ManifestError):
            AdapterRegistry.custom_from_manifests([invalid])

    def test_checked_in_manifests_are_namespaced_and_machine_readable(self):
        root = Path(__file__).parent.parent / "vibe_guide" / "adapters" / "manifests"
        for path in root.glob("*.yaml"):
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all(p["name"].startswith(data["id"] + ".") for p in data["probes"]))

    def test_visible_evidence_is_strict_namespaced_and_reuses_n0_contract(self):
        env = Environment(
            commands={"codex.agent": True},
            facts={
                "codex.shell": True, "codex.subprocess": True, "codex.worktree": True,
                "codex.visible_task.create": True, "codex.visible_task.enter": True,
                "codex.visible_task.resume": True, "codex.visible_task.wait": True,
            },
            provenance={"codex.visible_task.create": "public-tool-schema"},
        )
        capabilities = AdapterRegistry().get("codex").detect(env).capabilities
        self.assertIsInstance(capabilities, SharedAgentCapabilities)
        self.assertEqual((capabilities.level, capabilities.mode), ("full", "visible"))
        self.assertEqual(capabilities.provenance["codex.visible_task.create"], "public-tool-schema")
        with self.assertRaises(ValueError):
            AdapterRegistry().get("codex").detect(Environment(commands={"codex.agent": "false"}))
        cursor = AdapterRegistry().get("cursor").detect(env).capabilities
        self.assertNotEqual(cursor.level, "full")

    def test_background_not_advertised_without_verified_launcher(self):
        env = Environment(
            commands={"cursor.agent": True},
            facts={"cursor.shell": True, "cursor.subprocess": True, "cursor.worktree": True},
        )
        self.assertEqual(AdapterRegistry().get("cursor").detect(env).capabilities.mode, "guide")
        adapter = AdapterRegistry(background_launchers={"cursor": lambda *args: background_result()}).get("cursor")
        capabilities = adapter.detect(env).capabilities
        self.assertEqual((capabilities.level, capabilities.mode), ("background", "background"))
        self.assertEqual(adapter.provider_for(capabilities).provider, capabilities.provider)

    def test_guide_has_no_task_provider(self):
        adapter = AdapterRegistry().get("grok")
        capabilities = adapter.detect(Environment(commands={"grok.agent": True})).capabilities
        self.assertEqual(capabilities.mode, "guide")
        self.assertIsNone(adapter.provider_for(capabilities))

    def test_repository_creation_fails_before_create_without_routing(self):
        bridge, _ = codex_bridge(create_thread=lambda request: self.fail("create must not run"))
        provider = AdapterRegistry().get("codex").task_provider
        provider.bridge = bridge
        provider.routing = None
        with self.assertRaises(ProviderUnavailable):
            provider.create("developer", "N4", Path("contract.md"))

    def test_repository_worktree_target_and_binding_are_exact(self):
        captured = {}
        bridge, _ = codex_bridge(create_thread=lambda request: captured.setdefault("request", request) or {})
        # setdefault returns request, so use an explicit callable for the result.
        def create(request):
            captured["request"] = request
            return {"threadId": "thread-1", "hostId": "host-1"}
        bridge, _ = codex_bridge(create_thread=create)
        binding = routed_codex_provider(bridge).create("developer", "N4", Path("contract.md"))
        self.assertEqual(captured["request"]["target"], {
            "type": "project",
            "projectId": "project-1",
            "environment": {
                "type": "worktree",
                "startingState": {"type": "branch", "branchName": "codex/issue"},
            },
        })
        self.assertEqual((binding.host, binding.worktree, binding.branch), ("host-1", "/repo-wt", "codex/issue"))

    def test_repository_local_target_is_exact(self):
        captured = {}
        def create(request):
            captured["request"] = request
            return {"threadId": "thread-1", "hostId": "host-1"}
        provider = routed_codex_provider(codex_bridge(create_thread=create)[0], "local")
        binding = provider.create("reviewer", "N4", Path("contract.md"))
        self.assertEqual(captured["request"]["target"], {
            "type": "project", "projectId": "project-1", "environment": {"type": "local"},
        })
        self.assertEqual((binding.worktree, binding.branch), ("/repo", "main"))

    def test_codex_public_request_shapes_for_enter_resume_and_wait(self):
        calls = []
        bridge, _ = codex_bridge(
            navigate_to_codex_page=lambda request: calls.append(("navigate", request)),
            send_message_to_thread=lambda request: calls.append(("send", request)),
            wait_threads=lambda request: calls.append(("wait", request)) or {
                "timedOut": False,
                "polls": [{"threadId": "thread-1", "hostId": "host-1", "cursor": "c2", "thread": {"status": "complete"}}],
            },
        )
        provider = routed_codex_provider(bridge)
        binding = provider.create("developer", "N4", Path("contract.md"))
        provider.enter_or_locate(binding); provider.resume(binding, Path("rework.md"))
        update = provider.wait(binding, "c1")
        self.assertEqual(calls[0], ("navigate", {"threadId": "thread-1"}))
        self.assertEqual(calls[1][1]["hostId"], "host-1")
        self.assertEqual(calls[2], ("wait", {"targets": [{"threadId": "thread-1", "hostId": "host-1", "afterCursor": "c1"}], "timeoutMs": 120000}))
        self.assertEqual((update.cursor, update.status), ("c2", "complete"))

    def test_codex_wait_parses_public_polls_error_and_timeout(self):
        responses = iter([
            {"timedOut": False, "wake": "attention", "polls": [{"threadId": "t1", "hostId": "h1", "cursor": "c3", "latestTurn": {"status": "needs_attention"}}]},
            {"timedOut": False, "polls": [{"threadId": "t1", "hostId": "h1", "cursor": "c4", "error": {"message": "lost"}}]},
            {"timedOut": True, "polls": [{"threadId": "t1", "hostId": "h1", "cursor": "c5", "timedOut": True}]},
        ])
        bridge, _ = codex_bridge(wait_threads=lambda request: next(responses))
        binding = TaskBinding("codex-app-visible", "visible", "developer", "N4", task_id="t1", host="h1")
        updates = [bridge.wait(binding, None) for _ in range(3)]
        self.assertEqual([(u.cursor, u.status) for u in updates], [("c3", "needs_attention"), ("c4", "error"), ("c5", "timeout")])
        self.assertEqual(updates[1].payload["error"]["message"], "lost")

    def test_codex_list_visibility_uses_public_id_and_host(self):
        provider = routed_codex_provider()
        binding = provider.create("developer", "N4", Path("contract.md"))
        result = provider.visibility(binding)
        self.assertIsInstance(result, VisibilityResult)
        self.assertTrue(result.visible)

    def test_pending_client_id_never_becomes_thread_id_or_invented_recovery(self):
        bridge, _ = codex_bridge(
            create_thread=lambda request: {"clientThreadId": "client-1"},
            list_threads=lambda request: {"threads": [{"id": "thread-2", "hostId": "host-2"}]},
        )
        provider = routed_codex_provider(bridge)
        binding = provider.create("developer", "N4", Path("contract.md"))
        self.assertIsNone(binding.task_id)
        self.assertEqual(binding.client_thread_id, "client-1")
        with self.assertRaises(ProviderPending):
            provider.resolve_pending(binding)
        with self.assertRaises(ProviderPending):
            provider.enter_or_locate(binding)

    def test_background_create_requires_verified_launcher_and_durable_routing(self):
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("cursor-background").create("developer", "N4", Path("contract.md"))
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("cursor-background", lambda *args: {"handle": "ghost"}).create("developer", "N4", Path("contract.md"))
        binding = BackgroundTaskProvider("cursor-background", lambda *args: background_result()).create("developer", "N4", Path("contract.md"))
        self.assertEqual((binding.task_id, binding.worktree, binding.branch), ("bg-1", "/repo-wt", "codex/n4"))

    def test_background_binding_validates_provider_mode_role_and_issue(self):
        wrong = TaskBinding(
            "grok-background", "background", "reviewer", "OTHER", task_id="x",
            worktree="/wt", branch="b", status_file="status", handoff_file="handoff",
        )
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("grok-background", lambda *args: wrong).create("developer", "N4", Path("contract.md"))
        wrong_provider = TaskBinding(
            "other", "background", "developer", "N4", task_id="x",
            worktree="/wt", branch="b", status_file="status", handoff_file="handoff",
        )
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("grok-background", lambda *args: wrong_provider).create("developer", "N4", Path("contract.md"))
        wrong_mapping = background_result(); wrong_mapping["mode"] = "visible"
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("cursor-background", lambda *args: wrong_mapping).create("developer", "N4", Path("contract.md"))

    def test_session_prompt_and_monitor_command_remain_short_stable(self):
        adapter = AdapterRegistry().get("codex")
        self.assertEqual(adapter.session_prompt("启动监工", "plan-7"), "请启动监工，计划 plan-7。")
        self.assertEqual(adapter.monitor_command("plan-7", True), ["vibe", "monitor", "--plan", "plan-7", "--json"])


if __name__ == "__main__":
    unittest.main()
