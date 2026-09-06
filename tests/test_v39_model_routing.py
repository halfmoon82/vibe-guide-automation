import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderPending, ProviderUnavailable
from vibe_guide.model_router import (
    ModelRouter,
    WorkerUnavailable,
    build_worker_profile,
    provider_thinking_for,
)
from vibe_guide.models import IssueComplexity, LocalModel, WorkerProfile
from vibe_guide.models import DAGNode
from vibe_guide.monitor import Monitor
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.task_registry import TaskBinding


def _issue(issue_id="I-routing", *, complexity_band="complex"):
    return IssueComplexity(
        issue_id,
        "spec:" + issue_id,
        4,
        3,
        3,
        4,
        3,
        "large",
        ["cross_module"] if complexity_band == "complex" else [],
        complexity_band,
        "evidence:" + issue_id,
    )


class V39ModelRoutingTests(unittest.TestCase):
    def test_build_profile_has_stable_route_digest(self):
        models = [
            LocalModel("gpt-5.6-sol", ["visible_task"], 64_000, ["normal", "deep"], True)
        ]
        profile = build_worker_profile(_issue(), ["visible_task"], models)
        self.assertEqual(profile.model, "gpt-5.6-sol")
        self.assertEqual(profile.reasoning, "deep")
        self.assertRegex(profile.route_digest, r"^[0-9a-f]{64}$")
        self.assertEqual(profile.route_digest, build_worker_profile(_issue(), ["visible_task"], models).route_digest)

    def test_provider_create_carries_selected_profile_to_request(self):
        profile = WorkerProfile(
            "developer", "gpt-5.6-sol", "deep", [],
            {"issue_complexity_ref": "I-routing", "reasoning": "deep", "availability_evidence": "probe"},
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": "run-routing",
                "node_id": "I-routing",
                "role": "developer",
                "generation": 1,
                "project_id": "project-1",
                "worker_profile": profile.to_dict(),
            }
            captured = []

            def capture_then_pending(_contract, _run_id, _operation, request):
                captured.append(request)
                raise ProviderPending("create pending")

            with patch.object(runner, "_require_result", side_effect=capture_then_pending):
                with self.assertRaises(ProviderPending):
                    runner.task_binding(contract, paths.root, "run-routing", "start_pending")
            self.assertEqual(len(captured), 1)
            request = captured[0]
            self.assertEqual(request["model"], profile.model)
            self.assertEqual(request["thinking"], "high")
            self.assertEqual(request["route_digest"], profile.route_digest)

    def test_create_result_requires_actual_model_and_thinking_before_followup(self):
        profile = WorkerProfile(
            "developer", "gpt-5.6-sol", "deep", [],
            {"issue_complexity_ref": "I-routing", "reasoning": "deep", "availability_evidence": "probe"},
        )
        cases = (
            {"actual_model": None, "actual_thinking": "high"},
            {"actual_model": "", "actual_thinking": "high"},
            {"actual_model": "unknown", "actual_thinking": "high"},
            {"actual_model": {"name": "gpt-5.6-sol"}, "actual_thinking": "high"},
            {"actual_model": profile.model, "actual_thinking": None},
            {"actual_model": profile.model, "actual_thinking": ""},
            {"actual_model": profile.model, "actual_thinking": "unknown"},
            {"actual_model": profile.model, "actual_thinking": {"level": "high"}},
        )
        for actual in cases:
            with self.subTest(actual=actual), tempfile.TemporaryDirectory() as directory:
                paths = ProjectPaths(Path(directory))
                runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
                contract = {
                    "run_id": "run-routing",
                    "node_id": "I-routing",
                    "role": "developer",
                    "generation": 1,
                    "project_id": "project-1",
                    "routing_required": True,
                    "worker_profile": profile.to_dict(),
                }
                calls = []

                def create_only(_contract, _run_id, operation, _request):
                    calls.append(operation)
                    return {**actual, "binding": {"threadId": "task-1", "hostId": "host-1"}}

                with patch.object(runner, "_require_result", side_effect=create_only):
                    with self.assertRaises(ProviderUnavailable):
                        runner.task_binding(contract, paths.root, "run-routing", "start_pending")
                self.assertEqual(calls, ["create"])

    def test_invalid_branch_blocks_before_provider_io(self):
        for branch in ("/absolute-branch", "", " branch", "branch ", "bad branch", "a..b", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b]", ".hidden", "branch.", "branch.lock", "foo/@{bar}"):
            with self.subTest(branch=branch), tempfile.TemporaryDirectory() as directory:
                paths = ProjectPaths(Path(directory))
                runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
                contract = {
                    "run_id": "run-routing",
                    "node_id": "I-routing",
                    "role": "developer",
                    "generation": 1,
                    "binding_contract_version": "3.9",
                    "binding_probe": True,
                    "project_id": "project-1",
                    "worktree": str(paths.root / "worker"),
                    "managed_root": str(paths.root),
                    "branch": branch,
                    "base_sha": "a" * 40,
                }
                with patch.object(runner, "_require_result") as create:
                    with self.assertRaises(ProviderUnavailable):
                        runner.task_binding(contract, paths.root / "worker", "run-routing", "start_pending")
                    create.assert_not_called()

    def test_unsupported_reasoning_mapping_fails_closed(self):
        with self.assertRaises(ValueError):
            provider_thinking_for("unsupported")

    def test_unsupported_profile_blocks_before_provider_create(self):
        profile = {
            "worker": "developer",
            "model": "gpt-5.6-sol",
            "reasoning": "unsupported",
            "fallbacks": [],
            "selection_basis": {"issue_complexity_ref": "I-routing"},
        }
        with tempfile.TemporaryDirectory() as directory:
            runner = ProviderActionRunner(
                ProjectPaths(Path(directory)), "codex-app-visible", "codex-app-visible"
            )
            contract = {
                "run_id": "run-routing",
                "node_id": "I-routing",
                "role": "developer",
                "generation": 1,
                "project_id": "project-1",
                "routing_required": True,
                "worker_profile": profile,
            }
            with patch.object(runner, "_require_result") as create:
                with self.assertRaises(Exception):
                    runner.task_binding(contract, Path(directory), "run-routing", "start_pending")
                create.assert_not_called()

    def test_unknown_probe_remains_blocked(self):
        with self.assertRaises(WorkerUnavailable) as raised:
            ModelRouter().select(
                _issue(),
                ["visible_task"],
                [LocalModel("m1", ["visible_task"], 64_000, ["deep"], None)],
            )
        self.assertEqual(raised.exception.status, "blocked_unknown")

    def test_monitor_resolves_profile_from_issue_and_probe_contract(self):
        node = DAGNode(
            "I-routing", "routing", [], [], "routing", {
                "worker": "developer",
                "files": ["vibe_guide/model_router.py"],
                "issue_complexity": _issue().to_dict(),
                "required_capabilities": ["visible_task"],
                "model_probes": [
                    LocalModel("gpt-5.6-sol", ["visible_task"], 64_000, ["deep"], True).to_dict()
                ],
                "routing_required": True,
            }, "ready",
        )
        with tempfile.TemporaryDirectory() as directory:
            monitor = Monitor(ProjectPaths(Path(directory)), None, [node])
            current = {"worktree": ".worktrees/I-routing", "branch": "branch-I-routing", "contract_overrides": {}}
            profile = monitor._prepare_worker_profile(node, dict(node.contract), current, "developer")
            self.assertEqual(profile.model, "gpt-5.6-sol")
            self.assertEqual(current["contract_overrides"]["worker_profile"]["route_digest"], profile.route_digest)

    def test_task_binding_persists_route_identity_fields(self):
        binding = TaskBinding(
            provider="codex-app-visible",
            mode="visible",
            issue_id="I-routing",
            role="developer",
            task_id="task-1",
            host="host-1",
            worktree="/tmp/worktree",
            branch="branch-routing",
            run_id="run-routing",
            route_digest="a" * 64,
            model="gpt-5.6-sol",
            reasoning="deep",
        )
        data = binding.to_dict()
        self.assertEqual(data["route_digest"], "a" * 64)
        self.assertEqual(data["model"], "gpt-5.6-sol")
        self.assertEqual(data["reasoning"], "deep")


if __name__ == "__main__":
    unittest.main()
