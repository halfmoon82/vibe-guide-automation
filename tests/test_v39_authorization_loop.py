import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.contracts import RunHandle
from vibe_guide.models import AgentCapabilities, DAGNode, Plan
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.state import map_user_status
from vibe_guide.task_registry import TaskBinding
from vibe_guide.adapters.task_provider import ProviderPending, ProviderUnavailable
from vibe_guide.capability_contract import build_contract, save_contract
from vibe_guide.runners.provider_action import ProviderActionRunner


class _PendingBindingRunner(FakeRunner):
    """Provider-shaped runner: first binding probe is pending, second succeeds."""

    def __init__(self):
        super().__init__()
        self.binding_calls = 0
        self.contracts = []

    def task_binding(self, contract, worktree, run_id, status):
        self.binding_calls += 1
        self.contracts.append(dict(contract))
        if self.binding_calls == 1:
            raise ProviderPending("binding probe pending")
        return TaskBinding(
            provider="codex-app-visible",
            mode="visible",
            issue_id=contract["node_id"],
            role=contract["role"],
            task_id="task-1",
            host="host-1",
            worktree=str(worktree),
            branch=contract["branch"],
            run_id=run_id,
            status=status,
            generation=contract["generation"],
        )

    def start(self, contract, worktree):
        self.start_calls.append(dict(contract))
        return RunHandle("provider-handle-1")


class _UnavailableBindingRunner(_PendingBindingRunner):
    def task_binding(self, contract, worktree, run_id, status):
        self.binding_calls += 1
        self.contracts.append(dict(contract))
        raise ProviderUnavailable("binding evidence unavailable")


class AuthorizationLoopTests(unittest.TestCase):
    def _monitor(self, paths):
        node = DAGNode(
            "BUG-V3-001",
            "authorization loop",
            [],
            [],
            "control",
            {
                "binding_contract_version": "3.9",
                "project_id": "project-1",
                "worktree": ".worktrees/BUG-V3-001",
                "branch": "codex/bug-v3-001-authorization-loop-rev4",
                "base_sha": "a" * 40,
                "files": ["vibe_guide/monitor.py"],
                "worker": "developer_worker",
            },
            "planned",
        )
        plan = Plan("v39-rev4", 4, "docs/prd.md", [node.id], "draft")
        card = build_authorization_card(
            plan,
            [node],
            AgentCapabilities("fake", True, True, True, True, True, "full"),
        )
        return Monitor(paths, plan, [node]), authorize(card, "AUTHORIZE")

    def test_authorized_provider_start_internally_emits_binding_probe(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            monitor, record = self._monitor(paths)
            runner = _PendingBindingRunner()
            snapshot = monitor.start(record, runner)

            self.assertEqual(snapshot.nodes["BUG-V3-001"]["status"], "running")
            self.assertTrue(runner.contracts[0]["binding_probe"])
            self.assertEqual(snapshot.nodes["BUG-V3-001"]["binding_phase"], "retry_pending")
            self.assertEqual(snapshot.nodes["BUG-V3-001"]["user_status"], "自动修复中")

    def test_pending_probe_retry_keeps_same_generation_and_no_successor(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            (paths.vibe / "state.json").parent.mkdir(parents=True, exist_ok=True)
            (paths.vibe / "state.json").write_text(
                '{"workflow_version": 2, "session_gate": "s0_required"}\n',
                encoding="utf-8",
            )
            save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))
            monitor, record = self._monitor(paths)
            runner = _PendingBindingRunner()
            snapshot = monitor.start(record, runner)
            resumed = monitor.resume(snapshot.run_id, runner, poll_handles=False)

            self.assertEqual(runner.binding_calls, 2)
            self.assertEqual(runner.contracts[0]["generation"], runner.contracts[1]["generation"])
            self.assertTrue(runner.contracts[1]["binding_probe"])
            self.assertFalse(runner.contracts[1].get("successor", False))
            self.assertEqual(resumed.nodes["BUG-V3-001"]["status"], "blocked_unknown")

    def test_internal_states_map_to_only_four_user_statuses(self):
        expected = {
            "planned": "准备中",
            "start_pending": "准备中",
            "retry_pending": "自动修复中",
            "blocked_unknown": "自动修复中",
            "running": "已启动",
            "review": "已启动",
            "delivered": "已启动",
            "accepted": "已启动",
            "blocked_design": "需要你决定",
            "blocked_deploy": "需要你决定",
        }
        for internal, visible in expected.items():
            with self.subTest(internal=internal):
                self.assertEqual(map_user_status(internal), visible)

    def test_product_or_permission_block_maps_to_user_decision(self):
        self.assertEqual(
            map_user_status("blocked_unknown", "product scope changed"),
            "需要你决定",
        )

    def test_retry_marker_overrides_running_internal_status(self):
        self.assertEqual(
            map_user_status(
                {"status": "running", "retryable_action": {"phase": "develop"}}
            ),
            "自动修复中",
        )

    def test_provider_create_request_carries_contract_target_constraints(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            captured = []

            def pending(contract, worktree, operation, request):
                captured.append(request)
                raise ProviderPending("probe pending")

            contract = {
                "run_id": "run-1",
                "node_id": "BUG-V3-001",
                "role": "developer",
                "generation": 1,
                "binding_contract_version": "3.9",
                "binding_probe": True,
                "project_id": "project-1",
                "worktree": "/project/.worktrees/BUG-V3-001",
                "managed_root": "/project",
                "branch": "codex/bug-v3-001",
                "base_sha": "a" * 40,
            }
            with patch.object(runner, "_require_result", side_effect=pending):
                with self.assertRaises(ProviderPending):
                    runner.task_binding(contract, Path(contract["worktree"]), "run-1", "start_pending")
            self.assertEqual(
                captured[0]["binding"],
                {
                    "worktree": contract["worktree"],
                    "managed_root": contract["managed_root"],
                    "branch": contract["branch"],
                    "base_sha": contract["base_sha"],
                },
            )

    def test_prewrite_binding_failure_retains_same_task_retry_marker(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            monitor, record = self._monitor(paths)
            runner = _UnavailableBindingRunner()
            first = monitor.start(record, runner)
            self.assertEqual(first.nodes["BUG-V3-001"]["status"], "blocked_unknown")
            self.assertTrue(first.nodes["BUG-V3-001"]["retryable_action"]["same_task"])
            second = monitor.tick(first.run_id, runner)
            self.assertEqual(runner.binding_calls, 2)
            self.assertEqual(second.nodes["BUG-V3-001"]["status"], "blocked_unknown")
            self.assertFalse(second.nodes["BUG-V3-001"]["retryable_action"]["successor"])

    def test_v39_create_rejects_incomplete_or_drifting_binding_before_provider_request(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            worktree = str(Path(root) / "worker")
            valid = {
                "run_id": "run-1", "node_id": "BUG-V3-001", "role": "developer",
                "generation": 1, "binding_contract_version": "3.9",
                "binding_probe": True, "project_id": "project-1",
                "worktree": worktree, "managed_root": str(Path(root)),
                "branch": "codex/bug-v3-001", "base_sha": "a" * 40,
            }
            invalid = []
            for field in ("project_id", "worktree", "managed_root", "branch", "base_sha"):
                for value in (None, "", 1):
                    case = dict(valid)
                    case[field] = value
                    invalid.append((field + "=" + repr(value), case))
            for field, value in (
                ("worktree", "worker"),
                ("managed_root", "relative-root"),
                ("worktree", str(Path(root).parent / "outside")),
                ("managed_root", str(Path(root) / "other-managed")),
                ("branch", "/absolute-branch"),
                ("base_sha", "not-a-sha"),
            ):
                case = dict(valid)
                case[field] = value
                invalid.append((field + " drift=" + repr(value), case))

            for label, contract in invalid:
                with self.subTest(label=label):
                    with patch.object(runner, "_require_result") as require_result:
                        with self.assertRaises((ProviderUnavailable, ValueError)):
                            runner.task_binding(
                                contract, Path(worktree), "run-1", "start_pending"
                            )
                    require_result.assert_not_called()

    def test_v39_create_allows_provider_managed_parent_root(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            project_root = root / "supervisor-project"
            managed_root = root / "provider-managed"
            worktree = managed_root / "worker"
            project_root.mkdir()
            managed_root.mkdir()
            paths = ProjectPaths(project_root)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": "run-1", "node_id": "BUG-V3-001", "role": "developer",
                "generation": 1, "binding_contract_version": "3.9",
                "binding_probe": True, "project_id": "project-1",
                "worktree": str(worktree), "managed_root": str(managed_root),
                "branch": "codex/bug-v3-001", "base_sha": "a" * 40,
            }
            with patch.object(
                runner, "_require_result", side_effect=ProviderPending("probe pending")
            ) as require_result:
                try:
                    runner.task_binding(contract, worktree, "run-1", "start_pending")
                except ProviderPending:
                    pass
                except (ProviderUnavailable, ValueError) as exc:
                    self.fail("provider-managed parent root was rejected: %s" % exc)
            require_result.assert_called_once()


if __name__ == "__main__":
    unittest.main()
