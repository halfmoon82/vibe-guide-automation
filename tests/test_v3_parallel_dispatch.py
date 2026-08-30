import unittest

from vibe_guide.adapters.task_provider import (
    ProviderActionStore,
    RepositoryTaskRouting,
    TaskBinding,
    VisibleTaskProvider,
)
from vibe_guide.models import DAGNode, dispatch_ready_nodes, dispatch_ready_set
from vibe_guide.task_registry import (
    TaskBinding as RegistryTaskBinding,
    register_expected_binding_route,
)


class Bridge:
    def __init__(self):
        self.calls = []

    def create(self, role, issue_id, contract_path):
        self.calls.append((role, issue_id))
        return TaskBinding(
            provider="codex-app-visible", mode="visible", role=role,
            issue_id=issue_id, task_id=role + "-thread-" + issue_id,
            host="host-1", worktree=".vibe/worktrees/a",
            branch="codex/a", visible=True,
        )


def node(node_id):
    return DAGNode(node_id, node_id, [], [], "g", {
        "provider": "codex-app-visible", "adapter_id": "codex",
        "environment": "worktree", "writer": "dev-" + node_id,
        "reviewer": "rev-" + node_id, "supervisor": "sup",
        "host": "host-1", "parent_run_id": "run-parent",
        "worktree": ".vibe/worktrees/" + node_id,
        "branch": "codex/" + node_id, "allowlist": [node_id + ".py"],
        "input": ["input"], "output": ["output"],
        "error_behavior": ["error"], "acceptance_examples": ["accepted"],
    }, "ready")


class V3ParallelDispatchTests(unittest.TestCase):
    def test_non_conflicting_ready_nodes_create_distinct_visible_pairs(self):
        bridge = Bridge()
        provider = VisibleTaskProvider(
            "codex-app-visible", bridge,
            routing=RepositoryTaskRouting("project-1", "host-1", "worktree", ".vibe/worktrees/a", "codex/a"),
        )
        pairs = dispatch_ready_nodes([node("a")], provider, "contract.md")
        self.assertEqual([pair.node_id for pair in pairs], ["a"])
        self.assertEqual(len(bridge.calls), 2)
        self.assertEqual(len({pairs[0].developer.task_id, pairs[0].reviewer.task_id}), 2)
        self.assertTrue(pairs[0].developer.visible and pairs[0].reviewer.visible)

    def test_non_project_or_background_provider_is_rejected_without_fallback(self):
        bridge = Bridge()
        provider = VisibleTaskProvider(
            "background", bridge,
            routing=RepositoryTaskRouting("project-1", "host-1", "local", ".", "main"),
        )
        with self.assertRaises(ValueError):
            dispatch_ready_nodes([node("a")], provider, "contract.md")
        self.assertEqual(bridge.calls, [])

    def test_provider_mapping_rejects_wrong_worktree_and_branch(self):
        class WrongRouteBridge(Bridge):
            def create(self, role, issue_id, contract_path):
                self.calls.append((role, issue_id))
                return {"task_id": role + "-thread", "hostId": "host-1",
                        "worktree": "WRONG-WORKTREE", "branch": "WRONG-BRANCH"}

        provider = VisibleTaskProvider(
            "codex-app-visible", WrongRouteBridge(),
            routing=RepositoryTaskRouting("project-1", "host-1", "worktree", ".vibe/worktrees", "codex/v3-4"),
        )
        with self.assertRaises(Exception):
            provider.create("developer", "a", "contract.md")

    def test_provider_mapping_rejects_missing_host_without_fallback(self):
        class MissingHostBridge(Bridge):
            def create(self, role, issue_id, contract_path):
                return {"task_id": role + "-thread", "worktree": ".vibe/worktrees",
                        "branch": "codex/v3-4"}

        provider = VisibleTaskProvider(
            "codex-app-visible", MissingHostBridge(),
            routing=RepositoryTaskRouting("project-1", "host-1", "worktree", ".vibe/worktrees", "codex/v3-4"),
        )
        with self.assertRaises(Exception):
            provider.create("developer", "a", "contract.md")

    def test_provider_mapping_rejects_conflicting_identity_aliases(self):
        class ConflictingAliasBridge(Bridge):
            def create(self, role, issue_id, contract_path):
                return {"task_id": "task-a", "threadId": "thread-a", "host": "host-1",
                        "worktree": ".vibe/worktrees/a", "branch": "codex/a"}

        provider = VisibleTaskProvider(
            "codex-app-visible", ConflictingAliasBridge(),
            routing=RepositoryTaskRouting("project-1", "host-1", "worktree", ".vibe/worktrees/a", "codex/a"),
        )
        with self.assertRaises(Exception):
            provider.create("developer", "a", "contract.md")

    def test_monitor_dispatch_rejects_conflicting_alias_mapping(self):
        import sys
        import types
        from pathlib import Path
        from unittest.mock import patch

        diagnostics = types.ModuleType("vibe_guide.diagnostics")
        for name in ("Proposal", "SkillDiagnostic", "ContractCheck", "PlanningGate", "SessionGate",
                     "diagnose_skill", "build_skill_reference_proposal", "check_agents_contract",
                     "build_agentsmd_proposal", "assert_planning_gate", "require_execution_ready",
                     "screen_session", "require_session_screened", "validate_child_session_binding"):
            setattr(diagnostics, name, type(name, (), {}) if name[0].isupper() else (lambda *args, **kwargs: None))
        paths_module = types.ModuleType("vibe_guide.paths")
        paths_module.ProjectPaths = object
        planner_module = types.ModuleType("vibe_guide.planner")
        planner_module.resolve_consistency = lambda *args, **kwargs: None
        sys.modules.setdefault("vibe_guide.diagnostics", diagnostics)
        sys.modules.setdefault("vibe_guide.paths", paths_module)
        sys.modules.setdefault("vibe_guide.planner", planner_module)
        sys.modules["vibe_guide.planner"].resolve_consistency = lambda *args, **kwargs: None
        from vibe_guide.monitor import Monitor

        class AliasBridge:
            def create(self, role, issue_id, contract_path):
                return {"task_id": "chosen", "threadId": "different", "host": "host-1",
                        "hostId": "other-host", "worktree": ".vibe/worktrees/a", "branch": "codex/a"}

        monitor = Monitor.__new__(Monitor)
        monitor.nodes = {"a": node("a")}
        monitor.plan = type("Plan", (), {"complexity": "complex"})()
        monitor.paths = type("Paths", (), {"vibe": Path(".vibe")})()
        snapshot = type("Snapshot", (), {
            "run_id": "run-1",
            "nodes": {"a": {"status": "ready", "worktree": ".vibe/worktrees/a", "branch": "codex/a"}},
            "tasks": {},
        })()
        record = type("Record", (), {"active_pair_limit": 1})()
        provider = VisibleTaskProvider(
            "codex-app-visible", AliasBridge(),
            routing=RepositoryTaskRouting("project-1", "host-1", "worktree", ".vibe/worktrees/a", "codex/a"),
        )
        monitor._require_record = lambda value: None
        monitor._require_snapshot_authorization = lambda value: record
        with patch("vibe_guide.monitor.load_snapshot", return_value=snapshot), \
             patch("vibe_guide.monitor.validate_topology", return_value=type("Topology", (), {
                 "valid": True, "applies": True, "ready_nodes": ("a",), "reasons": (),
             })()), \
             patch("vibe_guide.monitor.acquire_writer_lease", return_value=True), \
             patch("vibe_guide.monitor.release_writer_lease"):
            with self.assertRaises(Exception):
                monitor.dispatch_ready_set(
                    provider, "contract.md", record=record, run_id="run-1",
                    caller_identity="sup", supervisor="sup",
                )

    def test_node_provider_route_must_match_exactly(self):
        bridge = Bridge()
        provider = VisibleTaskProvider(
            "codex-app-visible", bridge,
            routing=RepositoryTaskRouting("project-1", "host-1", "worktree", ".vibe/worktrees", "codex/v3-4"),
        )
        with self.assertRaises(Exception):
            dispatch_ready_nodes([node("a")], provider, "contract.md")

    def test_public_dispatch_alias_is_fail_closed(self):
        with self.assertRaises(PermissionError):
            dispatch_ready_set([node("a")], Bridge(), "contract.md")

    def test_monitor_runtime_rejects_background_binding_for_s1_node(self):
        import sys
        import types
        diagnostics = types.ModuleType("vibe_guide.diagnostics")
        for name in ("Proposal", "SkillDiagnostic", "ContractCheck", "PlanningGate", "SessionGate",
                     "diagnose_skill", "build_skill_reference_proposal", "check_agents_contract",
                     "build_agentsmd_proposal", "assert_planning_gate", "require_execution_ready",
                     "screen_session", "require_session_screened", "validate_child_session_binding"):
            setattr(diagnostics, name, type(name, (), {}) if name[0].isupper() else (lambda *args, **kwargs: None))
        paths_module = types.ModuleType("vibe_guide.paths")
        paths_module.ProjectPaths = object
        planner_module = types.ModuleType("vibe_guide.planner")
        planner_module.resolve_consistency = lambda *args, **kwargs: None
        sys.modules.setdefault("vibe_guide.diagnostics", diagnostics)
        sys.modules.setdefault("vibe_guide.paths", paths_module)
        sys.modules.setdefault("vibe_guide.planner", planner_module)
        sys.modules["vibe_guide.planner"].resolve_consistency = lambda *args, **kwargs: None
        from vibe_guide.monitor import Monitor
        monitor = Monitor.__new__(Monitor)
        monitor.plan = type("Plan", (), {"complexity": "complex"})()
        monitor.nodes = {"a": node("a")}
        monitor.paths = type("Paths", (), {"root": __import__("pathlib").Path(".")})()
        class Snapshot:
            run_id = "run-parent"
            nodes = {"a": {"worktree": ".vibe/worktrees/a", "branch": "codex/a"}}
        class BackgroundRunner:
            provider = "codex-app-visible"
            mode = "visible"
            def task_binding(self, contract, worktree, run_id, status):
                return RegistryTaskBinding(provider="runner", mode="background", issue_id="a",
                                           role="developer", task_id="task-a", run_id=run_id,
                                           worktree=str(worktree), branch="codex/a", status=status,
                                           generation=1)
        with self.assertRaises(ValueError):
            monitor._binding_for(Snapshot(), "a", "developer", "task-a", 1,
                                 "start_pending", BackgroundRunner(), node("a").contract, False)

    def test_monitor_start_rejects_background_runner_at_s1_entry(self):
        import sys
        import types
        diagnostics = types.ModuleType("vibe_guide.diagnostics")
        for name in ("Proposal", "SkillDiagnostic", "ContractCheck", "PlanningGate", "SessionGate",
                     "diagnose_skill", "build_skill_reference_proposal", "check_agents_contract",
                     "build_agentsmd_proposal", "assert_planning_gate", "require_execution_ready",
                     "screen_session", "require_session_screened", "validate_child_session_binding"):
            setattr(diagnostics, name, type(name, (), {}) if name[0].isupper() else (lambda *args, **kwargs: None))
        paths_module = types.ModuleType("vibe_guide.paths")
        paths_module.ProjectPaths = object
        planner_module = types.ModuleType("vibe_guide.planner")
        planner_module.resolve_consistency = lambda *args, **kwargs: None
        sys.modules.setdefault("vibe_guide.diagnostics", diagnostics)
        sys.modules.setdefault("vibe_guide.paths", paths_module)
        sys.modules.setdefault("vibe_guide.planner", planner_module)
        sys.modules["vibe_guide.planner"].resolve_consistency = lambda *args, **kwargs: None
        from vibe_guide.monitor import Monitor
        from vibe_guide.models import TopologyValidation
        monitor = Monitor.__new__(Monitor)
        monitor.nodes = {"a": node("a")}
        monitor.plan = type("Plan", (), {"complexity": "complex", "plan_id": "p", "version": 1})()
        monitor._require_record = lambda record: None
        monitor._topology_complexity = lambda: "complex"
        monitor.validate_topology = lambda **kwargs: TopologyValidation("valid", ready_nodes=("a",))
        class BackgroundRunner:
            provider = "runner"
            mode = "background"
        with self.assertRaises(PermissionError):
            Monitor.start(monitor, object(), BackgroundRunner(), caller_identity="sup")

    def test_provider_action_runner_binding_rejects_wrong_route(self):
        import vibe_guide.runners.provider_action as provider_action
        ProviderActionRunner = provider_action.ProviderActionRunner
        contract = dict(node("a").contract)
        contract.update({"node_id": "a", "role": "developer", "generation": 1,
                         "project_id": "project-1", "run_id": "run-1"})
        runner = ProviderActionRunner.__new__(ProviderActionRunner)
        runner.provider = "codex-app-visible"
        runner.paths = object()
        runner._consistency_instruction = lambda contract: ""
        provider_action.load_task_binding = lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError())
        register_expected_binding_route("run-1", "a", "developer", "host-1", ".vibe/worktrees/a", "codex/a")
        def wrong_result(contract, run_id, operation, request):
            if operation == "create":
                return {"binding": {"threadId": "thread-a", "hostId": "WRONG-HOST",
                                     "worktree": ".vibe/worktrees/a", "branch": "codex/a"}}
            if operation == "locate":
                return {"located": True}
            return {"visible": True, "direct_enter": True}
        runner._require_result = wrong_result
        with self.assertRaises(ValueError):
            runner.task_binding(contract, __import__("pathlib").Path(".vibe/worktrees/a"), "run-1", "running")
        import vibe_guide.adapters.task_provider as task_provider
        task_provider._OBSERVED_PROVIDER_ROUTES["thread-a"] = {
            "host": "host-1", "worktree": None, "branch": None,
        }
        runner._require_result = lambda *args: {"binding": {
            "threadId": "thread-a", "hostId": "host-1"}}
        with self.assertRaises(ValueError):
            runner.task_binding(contract, __import__("pathlib").Path(".vibe/worktrees/a"), "run-1", "running")

    def test_provider_action_runner_existing_binding_requires_route_provenance(self):
        import tempfile
        from pathlib import Path
        from vibe_guide.task_registry import save_task_binding
        import vibe_guide.runners.provider_action as provider_action

        with tempfile.TemporaryDirectory() as root:
            class Paths:
                vibe = Path(root) / ".vibe"

            binding = RegistryTaskBinding(
                provider="codex-app-visible", mode="visible", issue_id="existing",
                role="developer", task_id="thread-existing", host="WRONG-HOST",
                worktree="WRONG-WORKTREE", branch="WRONG-BRANCH", run_id="run-existing-6",
                status="running", visible=True, generation=1,
            )
            save_task_binding(Paths(), binding)
            runner = provider_action.ProviderActionRunner.__new__(provider_action.ProviderActionRunner)
            runner.paths = Paths()
            runner.provider = "codex-app-visible"
            contract = {
                "node_id": "existing", "role": "developer", "generation": 2,
                "run_id": "run-existing-6", "host": "host-expected",
                "worktree": ".vibe/worktrees/existing", "branch": "codex/existing",
            }
            with self.assertRaises(ValueError):
                runner.task_binding(contract, Path(contract["worktree"]), contract["run_id"], "running")

    def test_dispatch_rejects_provider_returned_route_drift(self):
        class DriftBridge(Bridge):
            def create(self, role, issue_id, contract_path):
                return TaskBinding(
                    provider="codex-app-visible", mode="visible", role=role,
                    issue_id=issue_id, task_id=role + "-thread-" + issue_id,
                    host="host-1", worktree="WRONG-WT", branch="WRONG-BR", visible=True,
                )
        provider = VisibleTaskProvider(
            "codex-app-visible", DriftBridge(),
            routing=RepositoryTaskRouting("project-1", "host-1", "worktree", ".vibe/worktrees/a", "codex/a"),
        )
        with self.assertRaises(Exception):
            dispatch_ready_nodes([node("a")], provider, "contract.md")

    def test_monitor_dispatch_rejects_missing_provider_route_without_fallback(self):
        """Monitor must not turn an incomplete provider binding into local route data."""
        import sys
        import types
        from unittest.mock import patch

        diagnostics = types.ModuleType("vibe_guide.diagnostics")
        for name in ("Proposal", "SkillDiagnostic", "ContractCheck", "PlanningGate", "SessionGate",
                     "diagnose_skill", "build_skill_reference_proposal", "check_agents_contract",
                     "build_agentsmd_proposal", "assert_planning_gate", "require_execution_ready",
                     "screen_session", "require_session_screened", "validate_child_session_binding"):
            setattr(diagnostics, name, type(name, (), {}) if name[0].isupper() else (lambda *args, **kwargs: None))
        paths_module = types.ModuleType("vibe_guide.paths")
        paths_module.ProjectPaths = object
        planner_module = types.ModuleType("vibe_guide.planner")
        planner_module.resolve_consistency = lambda *args, **kwargs: None
        sys.modules.setdefault("vibe_guide.diagnostics", diagnostics)
        sys.modules.setdefault("vibe_guide.paths", paths_module)
        sys.modules.setdefault("vibe_guide.planner", planner_module)
        sys.modules["vibe_guide.planner"].resolve_consistency = lambda *args, **kwargs: None
        from vibe_guide.monitor import Monitor

        monitor = Monitor.__new__(Monitor)
        monitor.nodes = {"a": node("a")}
        monitor.plan = type("Plan", (), {"complexity": "complex"})()
        monitor.paths = type("Paths", (), {})()
        snapshot = type("Snapshot", (), {
            "run_id": "run-1",
            "nodes": {"a": {"status": "ready", "worktree": ".vibe/worktrees/a", "branch": "codex/a"}},
            "tasks": {},
        })()
        provider = type("Provider", (), {"provider": "codex-app-visible", "mode": "visible"})()
        incomplete = type("Task", (), {
            "provider": "codex-app-visible", "mode": "visible", "task_id": "dev-a",
            "host": "host-1", "worktree": None, "branch": None,
            "status_file": "", "handoff_file": "", "cursor": None,
            "client_thread_id": None, "visible": True, "limitations": (),
        })()
        pair = type("Pair", (), {"node_id": "a", "developer": incomplete, "reviewer": incomplete})()
        record = type("Record", (), {"active_pair_limit": 1})()
        monitor._require_record = lambda value: None
        monitor._require_snapshot_authorization = lambda value: record
        with patch("vibe_guide.monitor.load_snapshot", return_value=snapshot), \
             patch("vibe_guide.monitor.validate_topology", return_value=type("Topology", (), {
                 "valid": True, "applies": True, "ready_nodes": ("a",), "reasons": (),
             })()), \
             patch("vibe_guide.monitor.acquire_writer_lease", return_value=True), \
             patch("vibe_guide.monitor.release_writer_lease"), \
             patch("vibe_guide.monitor.save_task_binding"), \
             patch("vibe_guide.monitor.save_snapshot"), \
             patch("vibe_guide.monitor.dispatch_ready_nodes", return_value=[pair]):
            with self.assertRaises(ValueError):
                monitor.dispatch_ready_set(
                    provider, "contract.md", record=record, run_id="run-1",
                    caller_identity="sup", supervisor="sup",
                )

    def test_provider_action_store_rejects_conflicting_route_aliases(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            class Paths:
                vibe = __import__("pathlib").Path(root) / ".vibe"
                resolve_vibe_path = lambda self, name: self.vibe / name
            store = ProviderActionStore(Paths())
            request = store.request(operation="create", provider="codex-app-visible",
                                    run_id="run-1", issue_id="a", role="developer",
                                    generation=1, native_tool="create", request={})
            store.complete(request["action_id"], {"binding": {
                "threadId": "thread-a", "hostId": "host-1", "host": "WRONG-HOST",
                "worktree": ".vibe/worktrees/a", "branch": "codex/a"}})
            with self.assertRaises(ValueError):
                store.result(request["action_id"])

    def test_registry_compat_read_rejects_foreign_run_without_run_id_argument(self):
        import json
        import tempfile
        from vibe_guide.task_registry import load_task_binding, save_task_binding
        with tempfile.TemporaryDirectory() as root:
            class Paths:
                vibe = __import__("pathlib").Path(root) / ".vibe"
            binding = RegistryTaskBinding(provider="runner", mode="background", issue_id="a",
                                          role="developer", task_id="task-a", run_id="run-1",
                                          worktree=".worktrees/a", branch="codex/a")
            save_task_binding(Paths(), binding)
            path = Paths.vibe / "runs" / "run-1" / "tasks.json"
            raw = json.loads(path.read_text())
            raw["bindings"][0]["run_id"] = "run-other"
            path.write_text(json.dumps(raw))
            with self.assertRaises(ValueError):
                load_task_binding(Paths(), "a", "developer")

if __name__ == "__main__":
    unittest.main()
