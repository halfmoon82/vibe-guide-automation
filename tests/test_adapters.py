import json
import unittest
from pathlib import Path

from vibe_guide.models import AgentCapabilities as SharedAgentCapabilities
from vibe_guide.adapters.base import Environment, ManifestError
from vibe_guide.adapters.registry import AdapterRegistry
from vibe_guide.adapters.task_provider import (
    BackgroundTaskProvider,
    BackgroundTaskRouting,
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
                "schemaVersion": 1, "cursor": "c2", "revision": 2, "changed": True,
                "thread": {
                    "id": "thread-1", "hostId": "host-1",
                    "status": {"type": "complete", "activeFlags": []},
                },
                "latestTurn": {"status": "complete", "error": None},
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


def background_routing():
    return BackgroundTaskRouting(
        host="local", worktree="/repo-wt", branch="codex/n4",
        status_file="status.txt", handoff_file="handoff.md",
    )


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
        self.assertEqual((capabilities.level, capabilities.mode), ("guide", "guide"))
        self.assertIsNone(adapter.provider_for(capabilities))

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
                "polls": [{
                    "schemaVersion": 1, "cursor": "c2", "revision": 2, "changed": True,
                    "thread": {"id": "thread-1", "hostId": "host-1", "status": {"type": "complete", "activeFlags": []}},
                    "latestTurn": {"status": "complete", "error": None},
                }],
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

    def test_codex_create_projects_only_public_tool_arguments(self):
        captured = {}
        def create(request):
            captured.update(request)
            return {"threadId": "thread-1", "hostId": "host-1"}

        bridge, _ = codex_bridge(create_thread=create)
        bridge.create({
            "prompt": "work",
            "target": {"type": "project", "projectId": "project-1", "environment": {"type": "local"}},
            "model": "gpt-5.6-sol",
            "thinking": "high",
            "title": "Issue task",
            "issue_id": "N4",
            "route_digest": "digest",
            "worker_profile": {"model": "gpt-5.6-sol"},
            "binding": {"branch": "codex/issue"},
        })
        self.assertEqual(set(captured), {"prompt", "target", "model", "thinking", "title"})

    def test_codex_wait_parses_public_polls_error_and_timeout(self):
        responses = iter([
            {"timedOut": False, "wake": "attention", "polls": [{"schemaVersion": 1, "cursor": "c3", "thread": {"id": "t1", "hostId": "h1", "status": {"type": "active", "activeFlags": []}}, "latestTurn": {"status": "needs_attention", "error": None}}]},
            {"timedOut": False, "polls": [{"schemaVersion": 1, "cursor": "c4", "thread": {"id": "t1", "hostId": "h1", "status": {"type": "active", "activeFlags": []}}, "latestTurn": {"status": "failed", "error": {"message": "lost"}}}]},
            {"timedOut": True, "polls": [{"schemaVersion": 1, "cursor": "c5", "thread": {"id": "t1", "hostId": "h1", "status": {"type": "active", "activeFlags": []}}, "latestTurn": None}]},
        ])
        bridge, _ = codex_bridge(wait_threads=lambda request: next(responses))
        binding = TaskBinding("codex-app-visible", "visible", "developer", "N4", task_id="t1", host="h1")
        updates = [bridge.wait(binding, None) for _ in range(3)]
        self.assertEqual([(u.cursor, u.status) for u in updates], [("c3", "needs_attention"), ("c4", "error"), ("c5", "timeout")])
        self.assertEqual(updates[1].payload["latestTurn"]["error"]["message"], "lost")

    def test_codex_wait_uses_thread_status_object_and_rejects_wrong_poll(self):
        responses = iter([
            {"timedOut": False, "polls": [{"cursor": "fresh", "thread": {"id": "t1", "hostId": "h1", "status": {"type": "active", "activeFlags": ["running"]}}, "latestTurn": None}]},
            {"timedOut": True, "polls": [{"cursor": "other", "thread": {"id": "different", "hostId": "h1", "status": {"type": "active", "activeFlags": []}}, "latestTurn": None}]},
        ])
        bridge, _ = codex_bridge(wait_threads=lambda request: next(responses))
        binding = TaskBinding("codex-app-visible", "visible", "developer", "N4", task_id="t1", host="h1")
        update = bridge.wait(binding, "old")
        self.assertEqual((update.cursor, update.status), ("fresh", "active"))
        with self.assertRaises(ProviderUnavailable):
            bridge.wait(binding, "fresh")

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
            BackgroundTaskProvider("cursor-background", expected_routing=background_routing()).create("developer", "N4", Path("contract.md"))
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("cursor-background", lambda *args: {"handle": "ghost"}, background_routing()).create("developer", "N4", Path("contract.md"))
        binding = BackgroundTaskProvider("cursor-background", lambda *args: background_result(), background_routing()).create("developer", "N4", Path("contract.md"))
        self.assertEqual((binding.task_id, binding.worktree, binding.branch), ("bg-1", "/repo-wt", "codex/n4"))

    def test_background_binding_validates_provider_mode_role_and_issue(self):
        wrong = TaskBinding(
            "grok-background", "background", "reviewer", "OTHER", task_id="x",
            worktree="/wt", branch="b", status_file="status", handoff_file="handoff",
        )
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("grok-background", lambda *args: wrong, background_routing()).create("developer", "N4", Path("contract.md"))
        wrong_provider = TaskBinding(
            "other", "background", "developer", "N4", task_id="x",
            worktree="/wt", branch="b", status_file="status", handoff_file="handoff",
        )
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("grok-background", lambda *args: wrong_provider, background_routing()).create("developer", "N4", Path("contract.md"))
        wrong_mapping = background_result(); wrong_mapping["mode"] = "visible"
        with self.assertRaises(ProviderUnavailable):
            BackgroundTaskProvider("cursor-background", lambda *args: wrong_mapping, background_routing()).create("developer", "N4", Path("contract.md"))

    def test_background_routing_rejects_each_non_empty_mismatch(self):
        expected = background_routing()
        for field in ("host", "worktree", "branch", "status_file", "handoff_file"):
            returned = background_result()
            returned[field] = "wrong-" + field
            with self.subTest(field=field), self.assertRaises(ProviderUnavailable):
                BackgroundTaskProvider(
                    "cursor-background", lambda *args, value=returned: value, expected
                ).create("developer", "N4", Path("contract.md"))

    def test_session_prompt_and_monitor_command_remain_short_stable(self):
        adapter = AdapterRegistry().get("codex")
        self.assertEqual(adapter.session_prompt("启动监工", "plan-7"), "请启动监工，计划 plan-7。")
        self.assertEqual(adapter.monitor_command("plan-7", True), ["vibe", "monitor", "--plan", "plan-7", "--json"])


EXPECTED_TOPOLOGY_MATRIX = {
    "codex": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "claude-code": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "cursor": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "kimi-code": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "deepseek-harness": {"probe_pass": "in_session_sdd", "probe_unknown": "dual-visible"},
    "workbuddy": {"probe_pass": "dual-visible", "probe_unknown": "dual-visible"},
    "grok": {"probe_pass": "dual-visible", "probe_unknown": "dual-visible"},
}

SDD_PLATFORMS = ("codex", "claude-code", "cursor", "kimi-code", "deepseek-harness")
DUAL_ONLY_PLATFORMS = ("workbuddy", "grok")


def sdd_env(adapter_id, value=True, provenance="session-contract"):
    facts = {adapter_id + ".in_session_sdd": value}
    return Environment(facts=facts, provenance={adapter_id + ".in_session_sdd": provenance})


class TopologyDecisionTests(unittest.TestCase):
    def test_every_manifest_declares_the_in_session_sdd_fact_probe(self):
        registry = AdapterRegistry()
        for adapter_id in SUPPORTED:
            manifest = registry.get(adapter_id).manifest
            probe = {"kind": "fact", "name": adapter_id + ".in_session_sdd"}
            self.assertIn(probe, [dict(item) for item in manifest["probes"]])

    def test_manifest_without_the_sdd_probe_fails_registration(self):
        manifest = dict(AdapterRegistry().get("codex").manifest)
        manifest["probes"] = [
            dict(item) for item in manifest["probes"]
            if item["name"] != "codex.in_session_sdd"
        ]
        with self.assertRaises(ManifestError):
            AdapterRegistry.custom_from_manifests([manifest])
        wrong_kind = dict(AdapterRegistry().get("codex").manifest)
        wrong_kind["probes"] = [
            dict(item) if item["name"] != "codex.in_session_sdd"
            else {"kind": "command", "name": "codex.in_session_sdd"}
            for item in wrong_kind["probes"]
        ]
        with self.assertRaises(ManifestError):
            AdapterRegistry.custom_from_manifests([wrong_kind])

    def test_dispatch_topology_matrix_matches_the_contract_row_by_row(self):
        from vibe_guide.adapters.registry import DISPATCH_TOPOLOGY_MATRIX
        self.assertEqual(DISPATCH_TOPOLOGY_MATRIX, EXPECTED_TOPOLOGY_MATRIX)
        self.assertEqual(set(DISPATCH_TOPOLOGY_MATRIX), SUPPORTED)

    def test_probe_pass_yields_in_session_sdd_for_sdd_platforms_only(self):
        registry = AdapterRegistry()
        for adapter_id in SDD_PLATFORMS:
            decision = registry.get(adapter_id).topology_decision(sdd_env(adapter_id))
            self.assertEqual(decision.topology, "in_session_sdd", adapter_id)
            self.assertEqual(decision.probe_status, "pass", adapter_id)
            self.assertEqual(decision.probe, adapter_id + ".in_session_sdd")
            self.assertEqual(decision.evidence_ref, "session-contract")
        for adapter_id in DUAL_ONLY_PLATFORMS:
            decision = registry.get(adapter_id).topology_decision(sdd_env(adapter_id))
            self.assertEqual(decision.topology, "dual-visible", adapter_id)
            self.assertEqual(decision.probe_status, "pass", adapter_id)

    def test_unknown_probe_never_yields_in_session_sdd(self):
        registry = AdapterRegistry()
        for adapter_id in SUPPORTED:
            decision = registry.get(adapter_id).topology_decision(Environment())
            self.assertEqual(decision.probe_status, "unknown", adapter_id)
            self.assertEqual(decision.topology, "dual-visible", adapter_id)
            self.assertIsNone(decision.evidence_ref, adapter_id)

    def test_false_probe_is_unsupported_and_conservative(self):
        registry = AdapterRegistry()
        for adapter_id in SUPPORTED:
            decision = registry.get(adapter_id).topology_decision(sdd_env(adapter_id, value=False))
            self.assertEqual(decision.probe_status, "unsupported", adapter_id)
            self.assertEqual(decision.topology, "dual-visible", adapter_id)
            self.assertEqual(decision.evidence_ref, "session-contract")

    def test_probe_pass_without_provenance_is_not_admissible(self):
        registry = AdapterRegistry()
        env = Environment(facts={"codex.in_session_sdd": True})
        decision = registry.get("codex").topology_decision(env)
        self.assertEqual(decision.probe_status, "unknown")
        self.assertEqual(decision.topology, "dual-visible")
        self.assertIsNone(decision.evidence_ref)

    def test_deepseek_does_not_upgrade_without_probe_evidence(self):
        registry = AdapterRegistry()
        adapter = registry.get("deepseek-harness")
        unknown = adapter.topology_decision(Environment())
        self.assertEqual((unknown.topology, unknown.upgraded), ("dual-visible", False))
        passed = adapter.topology_decision(sdd_env("deepseek-harness"))
        self.assertEqual((passed.topology, passed.upgraded), ("in_session_sdd", True))
        for adapter_id in DUAL_ONLY_PLATFORMS:
            decision = registry.get(adapter_id).topology_decision(sdd_env(adapter_id))
            self.assertFalse(decision.upgraded, adapter_id)

    def test_capability_report_records_topology_decision_and_evidence_ref(self):
        adapter = AdapterRegistry().get("codex")
        report = adapter.capability_report(sdd_env("codex"))
        self.assertEqual(report["topology"]["topology"], "in_session_sdd")
        self.assertEqual(report["topology"]["probe_status"], "pass")
        self.assertEqual(report["topology"]["evidence_ref"], "session-contract")
        self.assertEqual(report["evidence"]["codex.in_session_sdd"], True)
        unknown_report = adapter.capability_report(Environment())
        self.assertEqual(unknown_report["topology"]["topology"], "dual-visible")
        self.assertEqual(unknown_report["topology"]["probe_status"], "unknown")

    def test_registry_reports_topology_decisions_for_all_platforms(self):
        registry = AdapterRegistry()
        env = Environment(
            facts={"grok.in_session_sdd": True, "kimi-code.in_session_sdd": True},
            provenance={"grok.in_session_sdd": "session-contract",
                        "kimi-code.in_session_sdd": "session-contract"},
        )
        decisions = registry.topology_decisions(env)
        self.assertEqual(set(decisions), SUPPORTED)
        self.assertEqual(decisions["kimi-code"].topology, "in_session_sdd")
        self.assertEqual(decisions["grok"].topology, "dual-visible")
        self.assertEqual(decisions["codex"].probe_status, "unknown")


if __name__ == "__main__":
    unittest.main()
