import tempfile
import unittest
import subprocess
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

from vibe_guide.adapters.task_provider import ProviderPending, ProviderUnavailable
from vibe_guide.models import (
    BindingIntent,
    BindingObservation,
    BindingVerification,
    WaitThreadsCursorObservation,
)
from vibe_guide.paths import ProjectPaths
from vibe_guide.runners.provider_action import ProviderActionRunner
from vibe_guide.state import acquire_writer_lease, read_writer_lease, supervisor_lease_id
from vibe_guide.task_registry import TaskBinding, runtime_binding_gate, save_task_binding, load_task_binding
from vibe_guide.monitor import Monitor


class V39ProviderBindingEntryPointTests(unittest.TestCase):
    def _evidence(self, paths):
        node = "BUG-V3-005"
        worktree = str(paths.root / "worker")
        run_id = "run-v39"
        self.assertTrue(acquire_writer_lease(paths, node, worktree, run_id))
        lease = read_writer_lease(paths, node, worktree)
        cursor = WaitThreadsCursorObservation.from_wait_threads("task-1", "host-1", "cursor-1")
        intent = BindingIntent(
            project_id="project-1", task_id="task-1", host_id="host-1",
            node_id=node, worktree=worktree, managed_root=str(paths.root),
            branch="codex/bug-v3-005", base_sha="a" * 40,
            head_sha="b" * 40, clean=True, lease_id=supervisor_lease_id(node, worktree, run_id),
            cursor="cursor-1",
        )
        observation = BindingObservation(
            project_id="project-1", task_id="task-1", host_id="host-1",
            node_id=node, worktree=worktree, managed_root=str(paths.root),
            branch="codex/bug-v3-005", base_sha="a" * 40, head_sha="b" * 40,
            clean=True, lease=lease, cursor="cursor-1",
            source="codex_app__wait_threads", cursor_source="codex_app__wait_threads",
            cursor_task_id="task-1", cursor_host_id="host-1",
            cursor_lineage=cursor.lineage, cursor_observation=cursor,
        )
        return intent, observation, worktree, run_id

    def _binding(self, paths, intent, observation, worktree, run_id, verified=True):
        return TaskBinding(
            provider="codex-app-visible", mode="visible", issue_id="BUG-V3-005",
            role="developer", task_id="task-1", host="host-1", worktree=worktree,
            branch="codex/bug-v3-005", run_id=run_id, generation=1,
            binding_intent=intent, binding_observation=observation,
            binding_state="binding_verified" if verified else "blocked_unknown",
            business_write_allowed=verified,
        )

    def test_provider_action_start_is_red_without_v39_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = TaskBinding(
                provider="codex-app-visible", mode="visible", issue_id="BUG-V3-005",
                role="developer", task_id="task-1", host="host-1", worktree=worktree,
                branch="codex/bug-v3-005", run_id=run_id, generation=1,
            )
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": run_id, "node_id": "BUG-V3-005", "role": "developer",
                "generation": 1, "binding_contract_version": "3.9",
                "binding_intent": None, "binding_observation": None,
            }
            with patch("vibe_guide.runners.provider_action.load_task_binding", return_value=binding):
                with self.assertRaises(ProviderUnavailable):
                    runner.start(contract, Path(worktree))

    def test_provider_action_start_allows_verified_v39_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": run_id, "node_id": "BUG-V3-005", "role": "developer",
                "generation": 1, "binding_contract_version": "3.9",
                "binding_intent": intent, "binding_observation": observation,
            }
            with patch("vibe_guide.runners.provider_action.load_task_binding", return_value=binding):
                handle = runner.start(contract, Path(worktree))
            self.assertTrue(handle.run_id)
            self.assertEqual(runner.store.pending(), [])

    def test_plain_dict_evidence_cannot_bypass_v39_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = TaskBinding(
                provider="codex-app-visible", mode="visible", issue_id="BUG-V3-005",
                role="developer", task_id="task-1", host="host-1", worktree=worktree,
                branch="codex/bug-v3-005", run_id=run_id, generation=1,
            )
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": run_id, "node_id": "BUG-V3-005", "role": "developer",
                "generation": 1, "binding_contract_version": "3.9",
                "binding_intent": intent.to_dict(), "binding_observation": observation.to_dict(),
            }
            with patch("vibe_guide.runners.provider_action.load_task_binding", return_value=binding):
                with self.assertRaises(ProviderUnavailable):
                    runner.start(contract, Path(worktree))

    def test_binding_identity_drift_blocks_before_handle_write(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            binding.host = "wrong-host"
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": run_id, "node_id": "BUG-V3-005", "role": "developer",
                "generation": 1, "binding_contract_version": "3.9",
                "binding_intent": intent, "binding_observation": observation,
            }
            with patch("vibe_guide.runners.provider_action.load_task_binding", return_value=binding):
                with self.assertRaises(ProviderUnavailable):
                    runner.start(contract, Path(worktree))
            self.assertFalse((paths.vibe / "provider-actions" / "handles").exists())

    def test_monitor_start_chain_runs_probe_and_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            contract = {
                "binding_contract_version": "3.9",
                "binding_intent": intent,
                "binding_observation": observation,
            }
            class Runner:
                def __init__(self):
                    self.probes = 0
                    self.gates = 0
                def provider_binding_probe(self, c, b):
                    self.probes += 1
                    return runtime_binding_gate(c, b)
                def binding_gate(self, c, b):
                    self.gates += 1
                    return runtime_binding_gate(c, b)
            runner = Runner()
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor._require_binding_gate(contract, binding, runner)
            self.assertEqual((runner.probes, runner.gates), (1, 1))

    def test_monitor_rejects_forged_runner_verification(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            contract = {"binding_contract_version": "3.9", "binding_intent": intent, "binding_observation": observation}
            class ForgedRunner:
                def binding_gate(self, c, b):
                    return type("Forged", (), {"verified": True, "missing": [], "conflicts": []})()
            with self.assertRaises(ValueError):
                monitor = Monitor.__new__(Monitor)
                monitor.paths = paths
                monitor._require_binding_gate(contract, binding, ForgedRunner())

    def test_monitor_preflight_blocks_before_provider_task_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor.plan = SimpleNamespace(version=1)
            monitor._binding_cache = {}
            with self.assertRaises(ValueError):
                monitor._preflight_binding_before_provider({"binding_contract_version": "3.9"})

    def test_create_request_is_explicit_binding_probe(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": "run-v39", "node_id": "BUG-V3-005", "role": "developer",
                "generation": 1, "binding_contract_version": "3.9",
                "binding_probe": True, "project_id": "project-1",
                "worktree": str(paths.root / "worker"),
                "managed_root": str(paths.root),
                "branch": "codex/bug-v3-005", "base_sha": "a" * 40,
            }
            captured = []
            def probe_then_pending(c, r, op, req):
                captured.append(req)
                from vibe_guide.adapters.task_provider import ProviderPending
                raise ProviderPending("probe pending")
            with patch.object(runner, "_require_result", side_effect=probe_then_pending):
                with self.assertRaises(Exception):
                    runner.task_binding(contract, paths.root / "worker", "run-v39", "start_pending")
            self.assertEqual(len(captured), 1)
            self.assertTrue(captured[0].get("binding_probe"))
            self.assertFalse(captured[0].get("business_write_allowed", True))

    def test_create_probe_request_carries_structured_repository_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": "run-v39",
                "node_id": "BUG-V3-005",
                "role": "developer",
                "generation": 1,
                "binding_contract_version": "3.9",
                "binding_probe": True,
                "project_id": "project-1",
                "worktree": str(paths.root / "worker"),
                "managed_root": str(paths.root),
                "branch": "codex/bug-v3-005",
                "base_sha": "a" * 40,
            }
            captured = []

            def probe_then_pending(c, r, op, req):
                captured.append(req)
                raise ProviderPending("probe pending")

            with patch.object(runner, "_require_result", side_effect=probe_then_pending):
                with self.assertRaises(ProviderPending):
                    runner.task_binding(
                        contract,
                        paths.root / "worker",
                        "run-v39",
                        "start_pending",
                    )
            self.assertEqual(len(captured), 1)
            target = captured[0]["target"]
            self.assertEqual(target["projectId"], "project-1")
            binding_contract = target["binding_contract"]
            self.assertEqual(binding_contract["project_id"], contract["project_id"])
            self.assertEqual(binding_contract["worktree"], contract["worktree"])
            self.assertEqual(binding_contract["managed_root"], contract["managed_root"])
            self.assertEqual(binding_contract["branch"], contract["branch"])
            self.assertEqual(binding_contract["base_sha"], contract["base_sha"])

    def test_create_probe_allows_restricted_parent_managed_root(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            worktree = paths.root / "worker"
            contract = {
                "run_id": "run-v39",
                "node_id": "BUG-V3-005",
                "role": "developer",
                "generation": 1,
                "binding_contract_version": "3.9",
                "binding_probe": True,
                "project_id": "project-1",
                "worktree": str(worktree),
                "managed_root": str(paths.root.parent),
                "branch": "codex/bug-v3-005",
                "base_sha": "a" * 40,
            }
            with patch.object(
                runner, "_require_result", side_effect=ProviderPending("probe pending")
            ) as require_result:
                with self.assertRaises(ProviderPending):
                    runner.task_binding(contract, worktree, "run-v39", "start_pending")
            self.assertEqual(require_result.call_count, 1)
            request = require_result.call_args.args[3]
            self.assertEqual(request["target"]["binding_contract"]["managed_root"], str(paths.root.parent))

    def test_create_probe_allows_managed_root_unrelated_to_project_root(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            managed_root = paths.root.parent / "provider-managed"
            worktree = managed_root / "c3ad" / "开发辅助"
            contract = {
                "run_id": "run-v39",
                "node_id": "BUG-V3-005",
                "role": "developer",
                "generation": 1,
                "binding_contract_version": "3.9",
                "binding_probe": True,
                "project_id": "project-1",
                "worktree": str(worktree),
                "managed_root": str(managed_root),
                "branch": "codex/bug-v3-005",
                "base_sha": "a" * 40,
            }
            with patch.object(
                runner, "_require_result", side_effect=ProviderPending("probe pending")
            ) as require_result:
                with self.assertRaises(ProviderPending):
                    runner.task_binding(contract, worktree, "run-v39", "start_pending")
            self.assertEqual(require_result.call_count, 1)

    def test_create_probe_blocks_incomplete_or_drifting_repository_binding_before_request(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            worktree = str(paths.root / "worker")
            base_contract = {
                "run_id": "run-v39",
                "node_id": "BUG-V3-005",
                "role": "developer",
                "generation": 1,
                "binding_contract_version": "3.9",
                "binding_probe": True,
                "project_id": "project-1",
                "worktree": worktree,
                "managed_root": str(paths.root),
                "branch": "codex/bug-v3-005",
                "base_sha": "a" * 40,
            }
            invalid = {
                "missing_worktree": lambda c: c.pop("worktree"),
                "missing_managed_root": lambda c: c.pop("managed_root"),
                "missing_branch": lambda c: c.pop("branch"),
                "missing_base_sha": lambda c: c.pop("base_sha"),
                "empty_project_id": lambda c: c.update(project_id=""),
                "empty_managed_root": lambda c: c.update(managed_root=""),
                "empty_branch": lambda c: c.update(branch=""),
                "empty_base_sha": lambda c: c.update(base_sha=""),
                "wrong_type_project_id": lambda c: c.update(project_id=7),
                "wrong_type_worktree": lambda c: c.update(worktree=7),
                "wrong_type_managed_root": lambda c: c.update(managed_root=7),
                "wrong_type_branch": lambda c: c.update(branch=7),
                "wrong_type_base_sha": lambda c: c.update(base_sha=7),
                "worktree_drift": lambda c: c.update(worktree=str(paths.root / "other")),
                "managed_root_drift": lambda c: c.update(managed_root=str(paths.root / "other")),
                "managed_root_filesystem_root": lambda c: c.update(
                    managed_root=str(Path(c["managed_root"]).anchor)
                ),
            }
            for name, mutate in invalid.items():
                with self.subTest(name=name):
                    contract = dict(base_contract)
                    mutate(contract)
                    with patch.object(
                        runner,
                        "_require_result",
                        side_effect=ProviderPending("create must not be reached"),
                    ) as require_result:
                        with self.assertRaises((ProviderUnavailable, ProviderPending, ValueError)):
                            runner.task_binding(
                                contract,
                                Path(worktree),
                                "run-v39",
                                "start_pending",
                            )
                    self.assertEqual(require_result.call_count, 0)

    def test_provider_self_reported_nested_evidence_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            with patch.object(runner, "_require_result", side_effect=[
                {"binding": {"task_id": "task-1", "host": "host-1", "binding_intent": intent.to_dict(), "binding_observation": observation.to_dict()}},
                {"located": True}, {"visible": True, "direct_enter": True}
            ]) as require_result:
                with self.assertRaises(Exception):
                    runner.task_binding({"run_id": run_id, "node_id": "BUG-V3-005", "role": "developer", "generation": 1, "binding_contract_version": "3.9", "project_id": "project-1"}, Path(worktree), run_id, "start_pending")
            self.assertEqual(require_result.call_count, 0)

    def test_persisted_binding_cannot_resume_without_live_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            save_task_binding(paths, binding)
            loaded = load_task_binding(paths, "BUG-V3-005", "developer", run_id=run_id)
            self.assertEqual(loaded.binding_state, "blocked_unknown")
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            with self.assertRaises(ProviderUnavailable):
                runner.start({"run_id": run_id, "node_id": "BUG-V3-005", "role": "developer", "generation": 1, "binding_contract_version": "3.9", "continuation": True}, Path(worktree))

    def test_resume_reinjects_live_evidence_and_keeps_task_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            save_task_binding(paths, binding)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            with patch.object(runner, "_action", return_value={"action_id": "resume-1"}):
                with patch.object(runner.store, "_atomic") as atomic:
                    handle = runner.start({
                        "run_id": run_id, "node_id": "BUG-V3-005", "role": "developer",
                        "generation": 1, "binding_contract_version": "3.9", "continuation": True,
                        "binding_intent": intent, "binding_observation": observation,
                    }, Path(worktree))
            self.assertTrue(handle.run_id)
            metadata = atomic.call_args[0][1]
            self.assertEqual(metadata["task_id"], "task-1")
            self.assertFalse(metadata["successor"])

    def test_binding_bootstrap_blocks_dirty_and_wrong_base_without_git_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            result = runner.binding_bootstrap({"binding_contract_version": "3.9", "branch": "codex/bug-v3-005", "base_sha": "a" * 40, "managed_root": str(paths.root)}, TaskBinding(provider="codex-app-visible", mode="visible", issue_id="BUG-V3-005", role="developer", task_id="task-1", host="host-1", worktree=str(paths.root / "worker"), branch="codex/bug-v3-005", run_id="run-v39"), paths.root / "worker")
            self.assertFalse(getattr(result, "verified", False))

    def test_binding_bootstrap_never_reuses_verified_evidence_on_dirty_wrong_base_or_git_error(self):
        cases = {
            "dirty": {
                "rev-parse": (0, "a" * 40 + "\n"),
                "status": (0, " M business.py\n"),
            },
            "wrong_base": {
                "rev-parse": (0, "c" * 40 + "\n"),
                "status": (0, ""),
            },
            "git_error": {
                "rev-parse": (2, ""),
                "status": (0, ""),
            },
            "occupied": {
                "rev-parse": (0, "a" * 40 + "\n"),
                "status": (0, ""),
                "symbolic-ref": (1, ""),
                "show-ref": (0, ""),
                "worktree": (0, "branch refs/heads/codex/bug-v3-005\n"),
            },
        }
        for name, outcomes in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                paths = ProjectPaths(Path(directory))
                intent, observation, worktree, run_id = self._evidence(paths)
                binding = self._binding(paths, intent, observation, worktree, run_id)
                runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")

                def fake_run(args, **kwargs):
                    command = args[args.index("git") + 2]
                    rc, out = outcomes.get(command, (1, ""))
                    return subprocess.CompletedProcess(args, rc, out, "")

                contract = {
                    "binding_contract_version": "3.9", "managed_root": str(paths.root),
                    "branch": intent.branch, "base_sha": intent.base_sha,
                    "binding_intent": intent, "binding_observation": observation,
                }
                with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=fake_run):
                    result = runner.binding_bootstrap(contract, binding, Path(worktree))
                self.assertFalse(result.verified)
                self.assertFalse(result.business_write_allowed)

    def test_binding_bootstrap_blocks_any_git_observation_command_error(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            Path(worktree).mkdir(parents=True, exist_ok=True)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")

            def failed_observation(args, **kwargs):
                command = args[3]
                if command == "rev-parse":
                    return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
                if command == "status":
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == "symbolic-ref":
                    return subprocess.CompletedProcess(args, 1, "", "")
                if command == "show-ref":
                    return subprocess.CompletedProcess(args, 2, "", "git error")
                if command == "worktree":
                    return subprocess.CompletedProcess(args, 0, "", "")
                raise AssertionError(command)

            contract = {
                "binding_contract_version": "3.9",
                "managed_root": str(paths.root),
                "branch": intent.branch,
                "base_sha": intent.base_sha,
                "binding_intent": intent,
                "binding_observation": observation,
            }
            with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=failed_observation):
                result = runner.binding_bootstrap(contract, binding, Path(worktree))
            self.assertFalse(result.verified)
            self.assertFalse(result.business_write_allowed)

    def test_binding_bootstrap_never_switches_when_worktree_reports_occupied_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            Path(worktree).mkdir(parents=True, exist_ok=True)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            switch_calls = []

            def occupied_worktree(args, **kwargs):
                command = args[3]
                if command == "rev-parse":
                    return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
                if command == "status":
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == "symbolic-ref":
                    return subprocess.CompletedProcess(args, 1, "", "")
                if command == "show-ref":
                    return subprocess.CompletedProcess(args, 1, "", "")
                if command == "worktree":
                    return subprocess.CompletedProcess(
                        args, 0, "worktree /other\nbranch refs/heads/{}\n".format(intent.branch), ""
                    )
                if command == "switch":
                    switch_calls.append(args)
                    return subprocess.CompletedProcess(args, 0, "", "")
                raise AssertionError(command)

            contract = {
                "binding_contract_version": "3.9",
                "managed_root": str(paths.root),
                "branch": intent.branch,
                "base_sha": intent.base_sha,
                "binding_intent": intent,
                "binding_observation": observation,
            }
            with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=occupied_worktree):
                result = runner.binding_bootstrap(contract, binding, Path(worktree))
            self.assertFalse(result.verified)
            self.assertFalse(result.business_write_allowed)
            self.assertEqual(switch_calls, [])

    def test_v39_task_binding_requires_explicit_probe_even_with_complete_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": run_id, "node_id": "BUG-V3-005", "role": "developer",
                "generation": 1, "binding_contract_version": "3.9", "project_id": "project-1",
                "binding_intent": intent, "binding_observation": observation,
            }
            with patch.object(runner, "_require_result") as require_result:
                with self.assertRaises(ProviderUnavailable):
                    runner.task_binding(contract, Path(worktree), run_id, "start_pending")
            require_result.assert_not_called()

    def test_monitor_provider_unavailable_is_persisted_blocked_unknown_without_successor(self):
        monitor = Monitor.__new__(Monitor)
        monitor.paths = ProjectPaths(Path(tempfile.mkdtemp()))
        monitor.plan = SimpleNamespace(version=1)
        node = SimpleNamespace(contract={"files": []})
        monitor.nodes = {"BUG-V3-005": node}
        current = {
            "status": "planned", "worker": "worker", "worktree": ".", "branch": "branch",
            "developer_generation": 0, "review_generation": 0,
            "reviewer_started": False, "retryable_action": None,
            "active_task": None, "active_role": None, "start_intent": None,
        }
        snapshot = SimpleNamespace(
            run_id="run-v39", nodes={"BUG-V3-005": current}, handles={},
            tasks={}, capability_contract_digest="", authorization_digest="x",
        )
        record = SimpleNamespace(allowed_actions=[], file_scope=[])
        monitor._require_snapshot_authorization = lambda snap: record
        monitor._consistency_binding = lambda rec, n: {}
        monitor._binding_for = lambda *args, **kwargs: (_ for _ in ()).throw(ProviderUnavailable("missing live evidence"))
        monitor._record = lambda *args, **kwargs: None
        monitor._save_task_binding = lambda *args, **kwargs: None
        with patch("vibe_guide.monitor.validate_runtime_contract", side_effect=lambda c, **kwargs: c), \
             patch("vibe_guide.monitor.save_snapshot") as save_snapshot:
            result = monitor._start_task(snapshot, "BUG-V3-005", "developer", "implement", object(), False)
        self.assertFalse(result)
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertIsNone(current["active_task"])
        self.assertFalse(current.get("successor", False))
        save_snapshot.assert_called()

    def test_monitor_start_recovery_wires_binding_bootstrap_before_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor._load_task_binding = lambda *args: binding
            monitor._worktree_path = lambda current: Path(worktree)
            calls = []
            class Runner:
                def binding_bootstrap(self, contract, observed, root):
                    calls.append((contract.get("binding_bootstrap"), observed.task_id, root))
                    return runtime_binding_gate(contract, observed)
            contract = {
                "binding_contract_version": "3.9", "binding_bootstrap": True,
                "binding_intent": intent, "binding_observation": observation,
            }
            snapshot = SimpleNamespace(run_id=run_id)
            current = {}
            monitor._run_binding_bootstrap(snapshot, "BUG-V3-005", "developer", contract, Runner(), current)
            self.assertEqual(calls, [(True, "task-1", Path(worktree))])

    def test_binding_bootstrap_detached_at_base_may_attach_new_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            worker = paths.root / "worker"
            worker.mkdir()
            intent, observation, worktree, run_id = self._evidence(paths)
            intent = replace(intent, base_sha="b" * 40, head_sha="b" * 40)
            observation = replace(observation, base_sha="b" * 40, head_sha="b" * 40)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            calls = {"symbolic": 0, "ref": 0}
            def result(args, **kwargs):
                if args[-2:] == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(args, 0, "b" * 40 + "\n", "")
                if args[-3:] == ["status", "--porcelain", "--untracked-files=all"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if args[-4:] == ["symbolic-ref", "--short", "-q", "HEAD"]:
                    calls["symbolic"] += 1
                    return subprocess.CompletedProcess(args, 1 if calls["symbolic"] == 1 else 0, "" if calls["symbolic"] == 1 else "codex/bug-v3-005\n", "")
                if args[-4:] == ["show-ref", "--verify", "--quiet", "refs/heads/codex/bug-v3-005"]:
                    calls["ref"] += 1
                    return subprocess.CompletedProcess(args, 1 if calls["ref"] == 1 else 0, "", "")
                if args[-3:] == ["worktree", "list", "--porcelain"]:
                    return subprocess.CompletedProcess(args, 0, ("worktree {}\nHEAD {}\nbranch refs/heads/codex/bug-v3-005\n".format(worker, "b" * 40)) if calls["ref"] > 1 else "worktree {}\nHEAD {}\ndetached\n".format(worker, "b" * 40), "")
                if args[-4:] == ["switch", "-c", "codex/bug-v3-005", "b" * 40]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                raise AssertionError(args)
            contract = {"binding_contract_version": "3.9", "managed_root": str(paths.root), "branch": intent.branch, "base_sha": "b" * 40, "binding_intent": intent, "binding_observation": observation}
            with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=result):
                outcome = runner.binding_bootstrap(contract, binding, worker)
            self.assertTrue(outcome.verified)

    def test_binding_bootstrap_blocks_path_drift_and_occupied_branch(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            binding.worktree = str(paths.root / "other")
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            outcome = runner.binding_bootstrap({"binding_contract_version": "3.9", "managed_root": str(paths.root), "branch": intent.branch, "base_sha": intent.base_sha, "binding_intent": intent, "binding_observation": observation}, binding, Path(worktree))
            self.assertFalse(outcome.verified)

    def test_contract_evidence_drift_is_blocked_even_with_verified_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            contract = {"binding_contract_version": "3.9", "binding_intent": intent, "binding_observation": observation, "branch": "codex/drift"}
            self.assertFalse(runtime_binding_gate(contract, binding).verified)

    def test_runtime_gate_checks_provider_task_and_all_contract_binding_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            fields = {
                "provider_task_id": "other-task", "host_id": "other-host",
                "worktree": str(paths.root / "other"), "managed_root": str(paths.root / "other-root"),
                "branch": "codex/other", "base_sha": "c" * 40, "head_sha": "d" * 40,
                "clean": False, "cursor": "other-cursor", "lease": {"source": "provider"},
            }
            for field, value in fields.items():
                with self.subTest(field=field):
                    contract = {"binding_contract_version": "3.9", "binding_intent": intent, "binding_observation": observation, field: value}
                    result = runtime_binding_gate(contract, binding)
                    self.assertFalse(result.verified)
                    self.assertIn(field if field != "provider_task_id" else "task_id", result.conflicts)

    def test_monitor_probe_must_be_verified_and_match_local_gate(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)

            class Runner:
                def provider_binding_probe(self, contract, observed):
                    return BindingVerification("blocked_unknown", False, ["cursor"], [])

            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            with self.assertRaises(ValueError):
                monitor._require_binding_gate(
                    {"binding_contract_version": "3.9", "binding_intent": intent, "binding_observation": observation},
                    binding,
                    Runner(),
                )

    def test_pending_successor_is_blocked_without_successor_retry(self):
        monitor = Monitor.__new__(Monitor)
        monitor.paths = ProjectPaths(Path(tempfile.mkdtemp()))
        monitor.plan = SimpleNamespace(version=1)
        monitor.nodes = {"BUG-V3-005": SimpleNamespace(contract={"files": []})}
        current = {
            "status": "planned", "worker": "worker", "worktree": ".", "branch": "branch",
            "developer_generation": 0, "review_generation": 0, "reviewer_started": False,
            "retryable_action": None, "active_task": None, "active_role": None, "start_intent": None,
        }
        snapshot = SimpleNamespace(
            run_id="run-v39", nodes={"BUG-V3-005": current}, handles={}, tasks={},
            capability_contract_digest="", authorization_digest="x",
        )
        monitor._require_snapshot_authorization = lambda snap: SimpleNamespace(allowed_actions=[], file_scope=[])
        monitor._consistency_binding = lambda rec, node: {}
        monitor._binding_for = lambda *args, **kwargs: (_ for _ in ()).throw(ProviderPending("unknown create"))
        monitor._record = lambda *args, **kwargs: None
        monitor._save_task_binding = lambda *args, **kwargs: None
        with patch("vibe_guide.monitor.validate_runtime_contract", side_effect=lambda c, **kwargs: c), \
             patch("vibe_guide.monitor.save_snapshot") as save_snapshot:
            result = monitor._start_task(
                snapshot, "BUG-V3-005", "developer", "implement", object(), False, successor=True
            )
        self.assertFalse(result)
        self.assertEqual(current["status"], "blocked_unknown")
        self.assertFalse(current.get("retryable_action", {}).get("successor", False))
        self.assertIsNone(current.get("active_task"))
        save_snapshot.assert_called()

    def test_provider_pending_from_successor_start_forces_same_task_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor.plan = SimpleNamespace(version=1)
            monitor.nodes = {"BUG-V3-005": SimpleNamespace(contract={"files": []})}
            monitor._binding_cache = {}
            current = {
                "status": "planned", "worker": "worker", "worktree": worktree,
                "branch": binding.branch, "developer_generation": 0, "review_generation": 0,
                "reviewer_started": False, "retryable_action": None,
                "active_task": None, "active_role": None, "start_intent": None,
            }
            snapshot = SimpleNamespace(
                run_id=run_id, nodes={"BUG-V3-005": current}, handles={}, tasks={},
                capability_contract_digest="", authorization_digest="x",
            )
            monitor._require_snapshot_authorization = lambda snap: SimpleNamespace(allowed_actions=[], file_scope=[])
            monitor._consistency_binding = lambda rec, node: {}
            monitor._binding_for = lambda *args, **kwargs: binding
            monitor._save_task_binding = lambda *args, **kwargs: None
            monitor._record = lambda *args, **kwargs: None
            class Runner:
                def start(self, contract, root):
                    raise ProviderPending("resume pending")
            with patch("vibe_guide.monitor.validate_runtime_contract", side_effect=lambda c, **kwargs: c), \
                 patch("vibe_guide.monitor.save_snapshot"):
                result = monitor._start_task(
                    snapshot, "BUG-V3-005", "developer", "rework", Runner(), True, successor=True
                )
            self.assertFalse(result)
            self.assertEqual(current["status"], "blocked_unknown")
            self.assertFalse(current["retryable_action"]["successor"])
            self.assertTrue(current["retryable_action"]["same_task"])

    def test_binding_bootstrap_requires_current_root_and_branch_in_worktree_list(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            Path(worktree).mkdir(parents=True, exist_ok=True)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")

            def missing_root(args, **kwargs):
                command = args[3]
                if command == "rev-parse":
                    return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
                if command == "status":
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == "symbolic-ref":
                    return subprocess.CompletedProcess(args, 0, intent.branch + "\n", "")
                if command == "show-ref":
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == "worktree":
                    return subprocess.CompletedProcess(args, 0, "worktree /other\nbranch refs/heads/{}\n".format(intent.branch), "")
                raise AssertionError(command)

            contract = {
                "binding_contract_version": "3.9", "managed_root": str(paths.root),
                "branch": intent.branch, "base_sha": intent.base_sha,
                "binding_intent": intent, "binding_observation": observation,
            }
            with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=missing_root):
                result = runner.binding_bootstrap(contract, binding, Path(worktree))
            self.assertFalse(result.verified)
            self.assertFalse(result.business_write_allowed)

    def test_binding_bootstrap_blocks_target_branch_owned_by_another_worktree(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            Path(worktree).mkdir(parents=True, exist_ok=True)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")

            def wrong_owner(args, **kwargs):
                command = args[3]
                if command == "rev-parse":
                    return subprocess.CompletedProcess(args, 0, "a" * 40 + "\n", "")
                if command == "status":
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == "symbolic-ref":
                    return subprocess.CompletedProcess(args, 0, intent.branch + "\n", "")
                if command == "show-ref":
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == "worktree":
                    return subprocess.CompletedProcess(args, 0, "worktree {}\nbranch refs/heads/other\n\nworktree /other\nbranch refs/heads/{}\n".format(worktree, intent.branch), "")
                if command == "switch":
                    raise AssertionError("switch must not run")
                raise AssertionError(command)

            contract = {
                "binding_contract_version": "3.9", "managed_root": str(paths.root),
                "branch": intent.branch, "base_sha": intent.base_sha,
                "binding_intent": intent, "binding_observation": observation,
            }
            with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=wrong_owner):
                result = runner.binding_bootstrap(contract, binding, Path(worktree))
            self.assertFalse(result.verified)
            self.assertFalse(result.business_write_allowed)

    def test_binding_probe_requires_literal_true(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            for probe in ("yes", 1, [], {"enabled": True}):
                with self.subTest(probe=probe):
                    contract = {
                        "run_id": "run-v39", "node_id": "BUG-V3-005", "role": "developer",
                        "generation": 1, "binding_contract_version": "3.9", "project_id": "project-1",
                        "binding_probe": probe,
                    }
                    with patch.object(runner, "_require_result", side_effect=AssertionError("create called")) as require_result:
                        with self.assertRaises(ProviderUnavailable):
                            runner.task_binding(contract, paths.root / "worker", "run-v39", "start_pending")
                    require_result.assert_not_called()

    def test_monitor_generic_runner_refreshes_supervisor_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            with patch("vibe_guide.monitor.read_writer_lease", return_value=None) as read_lease:
                with self.assertRaises(ValueError):
                    monitor._require_binding_gate(
                        {"binding_contract_version": "3.9", "binding_intent": intent, "binding_observation": observation},
                        binding,
                        object(),
                    )
            read_lease.assert_called_once_with(paths, "BUG-V3-005", worktree)

    def test_explicit_probe_invokes_local_runtime_gate_before_allowing_probe(self):
        monitor = Monitor.__new__(Monitor)
        calls = []

        def local_gate(contract):
            calls.append(contract.get("binding_probe"))
            return BindingVerification("binding_verified", True, [], [])

        with patch("vibe_guide.monitor.runtime_binding_gate", side_effect=local_gate):
            monitor._preflight_binding_before_provider(
                {"binding_contract_version": "3.9", "binding_probe": True}
            )
        self.assertEqual(calls, [True])

    def test_monitor_start_orders_preflight_before_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            intent, observation, worktree, run_id = self._evidence(paths)
            binding = self._binding(paths, intent, observation, worktree, run_id)
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor.plan = SimpleNamespace(version=1)
            monitor.nodes = {"BUG-V3-005": SimpleNamespace(contract={"files": []})}
            monitor._binding_cache = {}
            current = {
                "status": "planned", "worker": "worker", "worktree": worktree,
                "branch": binding.branch, "developer_generation": 0, "review_generation": 0,
                "reviewer_started": False, "retryable_action": None,
                "active_task": None, "active_role": None, "start_intent": None,
            }
            snapshot = SimpleNamespace(
                run_id=run_id, nodes={"BUG-V3-005": current}, handles={}, tasks={},
                capability_contract_digest="", authorization_digest="x",
            )
            record = SimpleNamespace(allowed_actions=[], file_scope=[])
            monitor._require_snapshot_authorization = lambda snap: record
            monitor._consistency_binding = lambda rec, node: {}
            order = []
            monitor._preflight_binding_before_provider = lambda contract: order.append("preflight")
            monitor._run_binding_bootstrap = lambda *args, **kwargs: order.append("bootstrap")
            monitor._binding_for = lambda *args, **kwargs: (order.append("binding") or binding)
            monitor._require_binding_gate = lambda *args, **kwargs: order.append("gate")
            monitor._save_task_binding = lambda *args, **kwargs: order.append("savebinding")
            monitor._record = lambda *args, **kwargs: None
            class Runner:
                def start(self, contract, root):
                    order.append("start")
                    return SimpleNamespace(run_id="handle-1")
                def is_pending(self, handle):
                    return False
            with patch("vibe_guide.monitor.validate_runtime_contract", side_effect=lambda c, **kwargs: c), \
                 patch("vibe_guide.monitor.save_snapshot"):
                monitor._start_task(snapshot, "BUG-V3-005", "developer", "implement", Runner(), False)
            self.assertLess(order.index("preflight"), order.index("bootstrap"))

    def test_continuation_without_persisted_provenance_reaches_bootstrap_recovery(self):
        """A JSON-restored continuation must not be rejected before bootstrap."""
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            worker = paths.root / "worker"
            worker.mkdir()
            save_task_binding(
                paths,
                TaskBinding(
                    provider="codex-app-visible",
                    mode="visible",
                    issue_id="BUG-V3-005",
                    role="developer",
                    task_id="task-1",
                    host="host-1",
                    worktree=str(worker),
                    branch="codex/bug-v3-005",
                    run_id="run-v39",
                    status="running",
                    generation=1,
                ),
            )
            monitor = Monitor.__new__(Monitor)
            monitor.paths = paths
            monitor.plan = SimpleNamespace(version=1)
            monitor.nodes = {
                "BUG-V3-005": SimpleNamespace(
                    contract={
                        "binding_contract_version": "3.9",
                        "binding_bootstrap": True,
                        "managed_root": str(paths.root),
                        "branch": "codex/bug-v3-005",
                        "base_sha": "a" * 40,
                    }
                )
            }
            monitor._binding_cache = {}
            current = {
                "status": "running",
                "worker": "worker",
                "worktree": str(worker),
                "branch": "codex/bug-v3-005",
                "developer_generation": 1,
                "review_generation": 0,
                "reviewer_started": False,
                "developer_identity": "task-1",
                "retryable_action": None,
                "active_task": None,
                "active_role": None,
                "start_intent": None,
            }
            snapshot = SimpleNamespace(
                run_id="run-v39",
                nodes={"BUG-V3-005": current},
                handles={},
                tasks={},
                capability_contract_digest="",
                authorization_digest="x",
            )
            record = SimpleNamespace(allowed_actions=[], file_scope=[])
            monitor._require_snapshot_authorization = lambda snap: record
            monitor._consistency_binding = lambda rec, node: {}
            monitor._record = lambda *args, **kwargs: None
            calls = []

            class Runner:
                def binding_bootstrap(self, contract, observed, root):
                    calls.append("bootstrap")
                    raise ProviderUnavailable("live lease/cursor must be re-injected")

            with patch("vibe_guide.monitor.validate_runtime_contract", side_effect=lambda c, **kwargs: c), \
                 patch("vibe_guide.monitor.save_snapshot"):
                result = monitor._start_task(
                    snapshot,
                    "BUG-V3-005",
                    "developer",
                    "rework",
                    Runner(),
                    True,
                )

            self.assertFalse(result)
            self.assertEqual(calls, ["bootstrap"])
            self.assertEqual(current["status"], "blocked_unknown")
            self.assertFalse(snapshot.handles)
            self.assertFalse(snapshot.tasks)

    def test_recovery_bootstrap_issues_bound_wait_probe_for_live_cursor(self):
        """Recovery must obtain cursor evidence through the Store wait action."""
        with tempfile.TemporaryDirectory() as directory:
            paths = ProjectPaths(Path(directory))
            worker = paths.root / "worker"
            worker.mkdir()
            run_id = "run-v39"
            node_id = "BUG-V3-005"
            branch = "codex/bug-v3-005"
            base_sha = "a" * 40
            self.assertTrue(acquire_writer_lease(paths, node_id, str(worker), run_id))
            paths.vibe.mkdir(parents=True, exist_ok=True)
            (paths.vibe / "state.json").write_text("{}", encoding="utf-8")
            binding = TaskBinding(
                provider="codex-app-visible",
                mode="visible",
                issue_id=node_id,
                role="developer",
                task_id="task-1",
                host="host-1",
                worktree=str(worker),
                branch=branch,
                run_id=run_id,
                status="running",
                generation=1,
            )
            runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
            contract = {
                "run_id": run_id,
                "node_id": node_id,
                "role": "developer",
                "generation": 2,
                "task_id": "task-1",
                "binding_contract_version": "3.9",
                "binding_bootstrap": True,
                "continuation": True,
                "project_id": "project-1",
                "managed_root": str(paths.root),
                "branch": branch,
                "base_sha": base_sha,
            }

            def git_observation(args, **kwargs):
                command = args[3:]
                if command == ["rev-parse", "HEAD"]:
                    return subprocess.CompletedProcess(args, 0, base_sha + "\n", "")
                if command == ["status", "--porcelain", "--untracked-files=all"]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == ["symbolic-ref", "--short", "-q", "HEAD"]:
                    return subprocess.CompletedProcess(args, 0, branch + "\n", "")
                if command == ["show-ref", "--verify", "--quiet", "refs/heads/" + branch]:
                    return subprocess.CompletedProcess(args, 0, "", "")
                if command == ["worktree", "list", "--porcelain"]:
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        "worktree {}\nHEAD {}\nbranch refs/heads/{}\n".format(
                            worker, base_sha, branch
                        ),
                        "",
                    )
                raise AssertionError(command)

            with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=git_observation), \
                 patch("vibe_guide.adapters.task_provider.require_entry"):
                with self.assertRaises(ProviderPending):
                    runner.binding_bootstrap(contract, binding, worker)

            pending = runner.store.pending()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["operation"], "wait")
            self.assertEqual(pending[0]["native_tool"], "codex_app__wait_threads")
            request = pending[0]["request"]
            self.assertEqual(request["purpose"], "binding_probe")
            self.assertFalse(request["business_write_allowed"])
            self.assertEqual(request["targets"], [{"threadId": "task-1", "hostId": "host-1"}])

            runner.store.complete(
                pending[0]["action_id"],
                {
                    "polls": [
                        {
                            "thread": {"id": "task-1", "hostId": "host-1"},
                            "cursor": "cursor-live",
                        }
                    ]
                },
            )
            with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=git_observation), \
                 patch("vibe_guide.adapters.task_provider.require_entry"):
                outcome = runner.binding_bootstrap(contract, binding, worker)
            self.assertTrue(outcome.verified)
            self.assertIsInstance(binding.binding_intent, BindingIntent)
            self.assertIsInstance(binding.binding_observation, BindingObservation)
            self.assertEqual(binding.cursor, "cursor-live")
            self.assertEqual(binding.binding_observation.cursor, "cursor-live")
            self.assertEqual(runner.store.pending(), [])

    def test_recovery_bootstrap_rejects_contract_live_field_drift(self):
        """Recovery must not overwrite an explicit contract constraint."""
        drift_cases = {
            "head_sha": "b" * 40,
            "clean": False,
            "cursor": "attacker-cursor",
            "lease": {"source": "provider"},
        }
        for field, drift in drift_cases.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                paths = ProjectPaths(Path(directory))
                worker = paths.root / "worker"
                worker.mkdir()
                run_id = "run-v39"
                node_id = "BUG-V3-005"
                branch = "codex/bug-v3-005"
                base_sha = "a" * 40
                self.assertTrue(acquire_writer_lease(paths, node_id, str(worker), run_id))
                paths.vibe.mkdir(parents=True, exist_ok=True)
                (paths.vibe / "state.json").write_text("{}", encoding="utf-8")
                binding = TaskBinding(
                    provider="codex-app-visible", mode="visible", issue_id=node_id,
                    role="developer", task_id="task-1", host="host-1",
                    worktree=str(worker), branch=branch, run_id=run_id,
                    status="running", generation=1,
                )
                runner = ProviderActionRunner(paths, "codex-app-visible", "codex-app-visible")
                contract = {
                    "run_id": run_id, "node_id": node_id, "role": "developer",
                    "generation": 2, "task_id": "task-1",
                    "binding_contract_version": "3.9", "binding_bootstrap": True,
                    "continuation": True, "project_id": "project-1",
                    "managed_root": str(paths.root), "branch": branch,
                    "base_sha": base_sha, field: drift,
                }

                def git_observation(args, **kwargs):
                    command = args[3:]
                    if command == ["rev-parse", "HEAD"]:
                        return subprocess.CompletedProcess(args, 0, base_sha + "\n", "")
                    if command == ["status", "--porcelain", "--untracked-files=all"]:
                        return subprocess.CompletedProcess(args, 0, "", "")
                    if command == ["symbolic-ref", "--short", "-q", "HEAD"]:
                        return subprocess.CompletedProcess(args, 0, branch + "\n", "")
                    if command == ["show-ref", "--verify", "--quiet", "refs/heads/" + branch]:
                        return subprocess.CompletedProcess(args, 0, "", "")
                    if command == ["worktree", "list", "--porcelain"]:
                        return subprocess.CompletedProcess(
                            args, 0,
                            "worktree {}\nHEAD {}\nbranch refs/heads/{}\n".format(
                                worker, base_sha, branch
                            ),
                            "",
                        )
                    raise AssertionError(command)

                with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=git_observation), \
                     patch("vibe_guide.adapters.task_provider.require_entry"):
                    with self.assertRaises(ProviderPending):
                        runner.binding_bootstrap(contract, binding, worker)
                pending = runner.store.pending()
                self.assertEqual(len(pending), 1)
                runner.store.complete(
                    pending[0]["action_id"],
                    {"polls": [{"thread": {"id": "task-1", "hostId": "host-1"}, "cursor": "cursor-live"}]},
                )
                with patch("vibe_guide.runners.provider_action.subprocess.run", side_effect=git_observation), \
                     patch("vibe_guide.adapters.task_provider.require_entry"):
                    outcome = runner.binding_bootstrap(contract, binding, worker)
                self.assertFalse(outcome.verified)
                self.assertIn(field, outcome.conflicts)
                self.assertEqual(len(runner.store.pending()), 0)


if __name__ == "__main__":
    unittest.main()
