import json
import multiprocessing
import shutil
import sys
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest import mock


FIXTURE = Path(__file__).parent / "fixtures" / "e2e-project"


def _start_local_runner_in_process(root, command, result_queue):
    from vibe_guide.runners.local import LocalRunner

    runner = LocalRunner({"fixture-agent": command}, roots=[Path(root)])
    handle = runner.start(
        {
            "adapter_id": "fixture-agent",
            "command": command,
            "node_id": "cross-process",
            "role": "developer",
            "phase": "develop",
            "action": "develop",
            "task_id": "developer:cross-process",
            "generation": 1,
            "files": ["result.txt"],
        },
        Path(root),
    )
    result_queue.put(handle.run_id)


def byte_snapshot(root):
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        shutil.copytree(FIXTURE, self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def run_cli(self, argv, runner=None):
        try:
            from vibe_guide.cli import run_cli
        except ImportError as error:
            self.fail("run_cli public entry point is missing: %s" % error)
        return run_cli(argv, self.root, runner=runner)

    def local_runner(self):
        try:
            from vibe_guide.runners.local import LocalRunner
        except ImportError as error:
            self.fail("LocalRunner public entry point is missing: %s" % error)
        return LocalRunner(
            confirmed_commands={"fixture-agent": ["python3", "fake_agent.py"]}
        )

    def initialize(self):
        result = self.run_cli(["init", "--confirm", "--json"])
        self.assertEqual(result.exit_code, 0, result.text)

    def create_plan(self, plan_id="e2e-plan", spec="plan-source.json"):
        result = self.run_cli(
            [
                "plan",
                "--request",
                "设计并实现两个契约兼容的并行节点并完成独立审查",
                "--plan-id",
                plan_id,
                "--s1",
                "4,4,4,4,4",
                "--node-spec",
                spec,
                "--json",
            ]
        )
        self.assertEqual(result.exit_code, 0, result.text)
        return result

    def drive_to_terminal(self, plan_id, runner, limit=20):
        latest = None
        for _ in range(limit):
            latest = self.run_cli(
                ["resume", "--plan", plan_id, "--json"], runner=runner
            )
            if latest.payload.get("status") in {
                "complete",
                "blocked_unknown",
                "blocked_design",
                "failed",
            }:
                return latest
            time.sleep(0.02)
        self.fail("run did not reach a terminal state: %r" % (latest.payload,))

    def test_scan_is_byte_for_byte_read_only_and_init_is_idempotent(self):
        before = byte_snapshot(self.root)
        scanned = self.run_cli(["scan", "--json"])
        self.assertEqual(scanned.exit_code, 0)
        self.assertEqual(byte_snapshot(self.root), before)

        blocked = self.run_cli(["init", "--json"])
        self.assertEqual(blocked.exit_code, 3)
        self.assertEqual(byte_snapshot(self.root), before)

        first = self.run_cli(["init", "--confirm", "--json"])
        after_first = byte_snapshot(self.root)
        second = self.run_cli(["init", "--confirm", "--json"])
        self.assertTrue(first.payload["changed"])
        self.assertFalse(second.payload["changed"])
        self.assertEqual(byte_snapshot(self.root), after_first)

    def test_simple_route_and_complex_artifacts_with_parallel_contracts(self):
        self.initialize()
        simple = self.run_cli(
            ["plan", "--request", "修正标题错别字", "--json"]
        )
        self.assertEqual((simple.exit_code, simple.payload["route"]), (0, "simple"))

        planned = self.create_plan()
        self.assertEqual(planned.payload["route"], "complex")
        plan_dir = self.root / ".vibe" / "plans" / "e2e-plan"
        required = {
            "prd.md",
            "dag.yaml",
            "plan.md",
            "plan.json",
            "nodes.json",
            "authorization-card.json",
            "specs/api.md",
            "specs/ui.md",
            "issues/api.md",
            "issues/ui.md",
        }
        self.assertTrue(required.issubset({str(path.relative_to(plan_dir)) for path in plan_dir.rglob("*") if path.is_file()}))
        nodes = json.loads((plan_dir / "nodes.json").read_text(encoding="utf-8"))
        self.assertEqual([node["status"] for node in nodes], ["planned", "planned"])
        self.assertEqual({node["parallel_group"] for node in nodes}, {"build"})
        self.assertTrue(all(node["contract"]["acceptance_example"] for node in nodes))

    def test_authorized_flow_uses_developer_then_independent_reviewer_and_resumes_without_duplicate_writer(self):
        self.initialize()
        self.create_plan()
        runner = self.local_runner()

        blocked = self.run_cli(
            ["monitor", "--plan", "e2e-plan", "--json"], runner=runner
        )
        self.assertEqual(blocked.exit_code, 3)
        self.assertEqual(runner.start_count, 0)

        started = self.run_cli(
            [
                "monitor",
                "--plan",
                "e2e-plan",
                "--authorize",
                "AUTHORIZE",
                "--json",
            ],
            runner=runner,
        )
        self.assertEqual(started.exit_code, 0, started.text)
        self.assertEqual(runner.start_count, 2)

        finished = self.drive_to_terminal("e2e-plan", runner)
        self.assertEqual((finished.exit_code, finished.payload["status"]), (0, "complete"))
        calls = Counter((item["node_id"], item["role"]) for item in runner.start_contracts)
        self.assertEqual(calls, Counter({("api", "developer"): 1, ("ui", "developer"): 1, ("api", "reviewer"): 1, ("ui", "reviewer"): 1}))

        run_id = finished.payload["run_id"]
        run_dir = self.root / ".vibe" / "runs" / run_id
        tasks = json.loads((run_dir / "tasks.json").read_text(encoding="utf-8"))["bindings"]
        for issue in ("api", "ui"):
            identities = {item["role"]: item["task_id"] for item in tasks if item["issue_id"] == issue}
            self.assertNotEqual(identities["developer"], identities["reviewer"])
        self.assertGreater(len((run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()), 4)

    def test_public_cli_uses_provider_action_bridge_without_injected_runner(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.paths import ProjectPaths

        self.initialize()
        store = ProviderActionStore(ProjectPaths(self.root))
        store.publish_capabilities(
            "codex",
            {
                "codex.shell": True,
                "codex.subprocess": True,
                "codex.worktree": True,
                "codex.visible_task.create": True,
                "codex.visible_task.enter": True,
                "codex.visible_task.resume": True,
                "codex.visible_task.wait": True,
            },
            provenance="codex-app-session-bridge",
        )
        source = json.loads((self.root / "plan-source.json").read_text(encoding="utf-8"))
        source["capabilities"]["agent_id"] = "codex"
        source["active_pair_limit"] = 1
        source["nodes"] = [source["nodes"][0]]
        source["nodes"][0]["contract"].update(
            {
                "adapter_id": "codex",
                "project_id": "project-fixture",
                "host": "local",
                "naming": "契约并行",
            }
        )
        source["nodes"][0]["contract"].pop("command", None)
        source["nodes"][0]["contract"].pop("provider", None)
        source["nodes"][0]["contract"].pop("mode", None)
        visible_spec = self.root / "visible-plan.json"
        visible_spec.write_text(json.dumps(source), encoding="utf-8")
        self.create_plan("visible-plan", "visible-plan.json")

        result = self.run_cli(
            [
                "monitor",
                "--plan",
                "visible-plan",
                "--authorize",
                "AUTHORIZE",
                "--json",
            ]
        )
        self.assertEqual(result.exit_code, 4)
        self.assertNotEqual(result.payload.get("reason"), "runner unavailable")

        observed_tools = set()
        wait_counts = {"developer": 0, "reviewer": 0}
        consistency_bindings = {}
        for _ in range(40):
            for action in store.pending():
                observed_tools.add(action["native_tool"])
                role = action["role"]
                task_id = "thread-api-" + role
                operation = action["operation"]
                if operation == "create":
                    self.assertEqual(
                        set(action["request"]), {"prompt", "target"}
                    )
                    marker = "一致性纠偏证据必须原样绑定："
                    self.assertIn(marker, action["request"]["prompt"])
                    consistency_bindings[role] = json.loads(
                        action["request"]["prompt"].split(marker, 1)[1]
                    )
                    payload = {
                        "binding": {"threadId": task_id, "hostId": "local"}
                    }
                elif operation == "locate":
                    payload = {"located": True}
                elif operation == "visibility":
                    payload = {"visible": True, "direct_enter": True}
                elif operation == "resume":
                    marker = "一致性纠偏证据必须原样绑定："
                    self.assertIn(marker, action["request"]["prompt"])
                    self.assertEqual(
                        json.loads(action["request"]["prompt"].split(marker, 1)[1]),
                        consistency_bindings[role],
                    )
                    payload = {"resumed": True}
                elif operation == "wait":
                    wait_counts[role] += 1
                    target = action["request"]["targets"][0]
                    if role == "developer" and wait_counts[role] == 1:
                        self.assertNotIn("afterCursor", target)
                        payload = {
                            "status": "timeout",
                            "cursor": "cursor-developer-timeout",
                        }
                    elif role == "developer" and wait_counts[role] == 2:
                        self.assertEqual(
                            target.get("afterCursor"),
                            "cursor-developer-timeout",
                        )
                        payload = {
                            "status": "completed",
                            "cursor": "cursor-developer-complete",
                            "event": "complete",
                            "evidence": "verified-developer",
                        }
                    elif role == "developer":
                        self.assertEqual(
                            target.get("afterCursor"),
                            "cursor-developer-complete",
                        )
                        payload = {
                            "status": "completed",
                            "cursor": "cursor-developer-rework",
                            "event": "complete",
                            "evidence": "verified-developer-rework",
                        }
                    elif wait_counts[role] == 1:
                        payload = {
                            "status": "completed",
                            "cursor": "cursor-review-finding",
                            "event": "review_finding",
                            "finding": "stale lower-priority name",
                            "in_contract": False,
                            "consistency": {
                                "field": "naming",
                                "action": "rework",
                                "files": ["api.txt"],
                                "candidates": [
                                    {
                                        "source": "approved_prd",
                                        "value": "契约并行",
                                        "binding": consistency_bindings["reviewer"],
                                    },
                                    {
                                        "source": "implementation",
                                        "value": "stale-name",
                                    },
                                ],
                            },
                        }
                    else:
                        payload = {
                            "status": "completed",
                            "cursor": "cursor-{}-{}".format(
                                role, wait_counts[role]
                            ),
                            "event": (
                                "complete" if role == "developer" else "accepted"
                            ),
                            "evidence": "verified-" + role,
                        }
                else:
                    self.fail("unexpected provider action: %r" % action)
                store.complete(action["action_id"], payload)
            result = self.run_cli(
                ["resume", "--plan", "visible-plan", "--json"]
            )
            if result.payload.get("status") == "complete":
                break

        self.assertEqual((result.exit_code, result.payload["status"]), (0, "complete"))
        self.assertTrue(
            {
                "codex_app__create_thread",
                "codex_app__navigate_to_codex_page",
                "codex_app__send_message_to_thread",
                "codex_app__wait_threads",
            }.issubset(observed_tools)
        )
        run_id = result.payload["run_id"]
        bindings = json.loads(
            (self.root / ".vibe/runs" / run_id / "tasks.json").read_text(
                encoding="utf-8"
            )
        )["bindings"]
        self.assertEqual({item["status"] for item in bindings}, {"archived"})
        self.assertEqual(
            {item["task_id"] for item in bindings},
            {"thread-api-developer", "thread-api-reviewer"},
        )
        self.assertGreaterEqual(wait_counts["developer"], 2)
        snapshot = json.loads(
            (self.root / ".vibe/runs" / run_id / "state.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            snapshot["nodes"]["api"]["contract_overrides"],
            {"naming": "契约并行"},
        )
        self.assertEqual(
            snapshot["nodes"]["api"]["corrections"][0][
                "consistency_binding"
            ],
            consistency_bindings["reviewer"],
        )

    def test_doctor_accepts_verified_provider_bridge_without_local_agent_command(self):
        from vibe_guide.adapters.task_provider import ProviderActionStore
        from vibe_guide.paths import ProjectPaths

        self.initialize()
        (self.root / ".vibe/config.json").write_text(
            json.dumps(
                {
                    "skills": [
                        {
                            "name": "architecture-skill-pack",
                            "source": (
                                "https://github.com/lov-team/"
                                "architecture-skill-pack"
                            ),
                            "commit": "a" * 40,
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        ProviderActionStore(ProjectPaths(self.root)).publish_capabilities(
            "codex",
            {
                "codex.shell": True,
                "codex.subprocess": True,
                "codex.worktree": True,
                "codex.visible_task.create": True,
                "codex.visible_task.enter": True,
                "codex.visible_task.resume": True,
                "codex.visible_task.wait": True,
            },
            provenance="codex-app-session-bridge",
        )

        with mock.patch("vibe_guide.scanner.shutil.which", return_value=None):
            result = self.run_cli(["doctor", "--json"])

        self.assertEqual((result.exit_code, result.payload["status"]), (0, "ok"))
        self.assertIsNotNone(result.payload["provider_bridge"])
        self.assertNotIn("no candidate Agent command found", result.payload["issues"])

    def test_design_change_invalidates_authorization_unknown_is_not_success_and_deploy_is_excluded(self):
        self.initialize()
        self.create_plan("design-plan")
        runner = self.local_runner()
        started = self.run_cli(
            ["monitor", "--plan", "design-plan", "--authorize", "AUTHORIZE", "--json"],
            runner=runner,
        )
        self.assertEqual(started.exit_code, 0)
        nodes_path = self.root / ".vibe/plans/design-plan/nodes.json"
        nodes = json.loads(nodes_path.read_text(encoding="utf-8"))
        nodes[0]["contract"]["acceptance_example"] = "changed product result"
        nodes_path.write_text(json.dumps(nodes), encoding="utf-8")
        changed = self.run_cli(
            ["resume", "--plan", "design-plan", "--json"], runner=runner
        )
        self.assertEqual((changed.exit_code, changed.payload["status"]), (3, "blocked_design"))
        self.assertIn("authorization", changed.payload["reason"])
        persisted = self.run_cli(
            ["status", "--plan", "design-plan", "--json"], runner=runner
        )
        self.assertEqual(
            (persisted.exit_code, persisted.payload["status"]),
            (3, "blocked_design"),
        )
        from vibe_guide.contracts import RunHandle
        design_run = json.loads(
            (self.root / ".vibe/plans/design-plan/current-run.json").read_text(
                encoding="utf-8"
            )
        )["run_id"]
        design_state = json.loads(
            (self.root / ".vibe/runs" / design_run / "state.json").read_text(
                encoding="utf-8"
            )
        )
        for handle_id in design_state["handles"].values():
            runner.stop(RunHandle(handle_id))
            runner.poll(RunHandle(handle_id))

        unknown_spec = json.loads((self.root / "plan-source.json").read_text(encoding="utf-8"))
        unknown_spec["nodes"] = [unknown_spec["nodes"][0]]
        unknown_spec["nodes"][0]["id"] = "unknown-api"
        unknown_spec_path = self.root / "unknown-plan.json"
        unknown_spec_path.write_text(json.dumps(unknown_spec), encoding="utf-8")
        self.create_plan("unknown-plan", "unknown-plan.json")
        unknown_runner = self.local_runner()
        self.run_cli(
            ["monitor", "--plan", "unknown-plan", "--authorize", "AUTHORIZE", "--json"],
            runner=unknown_runner,
        )
        unknown = self.drive_to_terminal("unknown-plan", unknown_runner)
        self.assertEqual((unknown.exit_code, unknown.payload["status"]), (4, "blocked_unknown"))
        self.assertNotIn(unknown.payload["status"], {"ok", "success", "no-op", "complete"})

        deploy_spec = json.loads((self.root / "plan-source.json").read_text(encoding="utf-8"))
        deploy_spec["nodes"][0]["contract"]["actions"] = ["test", "deploy"]
        deploy_path = self.root / "deploy-plan.json"
        deploy_path.write_text(json.dumps(deploy_spec), encoding="utf-8")
        rejected = self.run_cli(
            [
                "plan", "--request", "实现并部署两个模块", "--plan-id", "deploy-plan",
                "--s1", "4,4,4,4,4", "--node-spec", "deploy-plan.json", "--json",
            ]
        )
        self.assertEqual(rejected.exit_code, 3)
        self.assertIn("excluded", rejected.payload["reason"])
        self.assertFalse((self.root / ".vibe/plans/deploy-plan").exists())

    def test_public_reauthorization_continues_same_plan_run_and_preserves_cursor(self):
        from vibe_guide.authorization import AuthorizationRecord
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.runners.fake import FakeRunner
        from vibe_guide.state import load_events, load_snapshot
        from vibe_guide.task_registry import load_task_binding, save_task_binding

        self.initialize()
        source = json.loads((self.root / "plan-source.json").read_text(encoding="utf-8"))
        source["nodes"] = [source["nodes"][0]]
        source_path = self.root / "reauth-plan.json"
        source_path.write_text(json.dumps(source), encoding="utf-8")
        self.create_plan("reauth-plan", "reauth-plan.json")
        runner = FakeRunner()
        started = self.run_cli(
            ["monitor", "--plan", "reauth-plan", "--authorize", "AUTHORIZE", "--json"],
            runner=runner,
        )
        self.assertEqual(started.exit_code, 0)
        run_id = started.payload["run_id"]
        paths = ProjectPaths(self.root)
        old_snapshot = load_snapshot(paths, run_id)
        old_authorization = dict(old_snapshot.authorization)
        binding = load_task_binding(paths, "api", "developer", run_id=run_id)
        binding.cursor = "cursor-before-reauthorization"
        save_task_binding(paths, binding)

        nodes_path = self.root / ".vibe/plans/reauth-plan/nodes.json"
        nodes = json.loads(nodes_path.read_text(encoding="utf-8"))
        nodes[0]["contract"]["acceptance_example"] = "corrected unique implementation outcome"
        nodes_path.write_text(json.dumps(nodes), encoding="utf-8")
        invalidated = self.run_cli(
            ["resume", "--plan", "reauth-plan", "--json"], runner=runner
        )
        self.assertEqual(
            (invalidated.exit_code, invalidated.payload["status"]),
            (3, "blocked_design"),
        )

        reauthorized = self.run_cli(
            ["monitor", "--plan", "reauth-plan", "--authorize", "AUTHORIZE", "--json"],
            runner=runner,
        )

        self.assertEqual(reauthorized.exit_code, 0, reauthorized.text)
        self.assertEqual(reauthorized.payload["run_id"], run_id)
        self.assertEqual(reauthorized.payload["status"], "running")
        self.assertFalse(
            (self.root / ".vibe/plans/reauth-plan/authorization-invalidated.json").exists()
        )
        current = load_snapshot(paths, run_id)
        self.assertNotEqual(current.authorization_digest, old_snapshot.authorization_digest)
        self.assertEqual(runner.start_calls[-1]["task_id"], binding.task_id)
        self.assertEqual(runner.start_calls[-1]["phase"], "rework")
        self.assertTrue(runner.start_calls[-1]["continuation"])

        transition = [
            record for record in load_events(paths, run_id)
            if record["event"] == "authorization_reauthorized"
        ][0]["data"]
        self.assertEqual(
            AuthorizationRecord.from_dict(
                transition["previous_authorization"]
            ).to_dict(),
            old_authorization,
        )
        self.assertEqual(
            transition["new_authorization"]["digest"], current.authorization_digest
        )
        self.assertIn("contract", transition["change_reason"])
        self.assertEqual(
            transition["continuation"]["api:developer"]["cursor"],
            "cursor-before-reauthorization",
        )

        visible = self.run_cli(["status", "--plan", "reauth-plan", "--json"])
        self.assertEqual((visible.exit_code, visible.payload["status"]), (0, "running"))

    def test_local_runner_rejects_unconfirmed_command_stops_and_persists_only_safe_metadata(self):
        runner = self.local_runner()
        with self.assertRaises(PermissionError):
            runner.start(
                {"adapter_id": "fixture-agent", "command": ["python3", "other.py"]},
                self.root,
            )

        handle = runner.start(
            {
                "adapter_id": "fixture-agent",
                "command": ["python3", "fake_agent.py"],
                "node_id": "slow",
                "role": "developer",
                "task_id": "developer:slow",
                "generation": 1,
            },
            self.root,
        )
        runner.stop(handle)
        events = runner.poll(handle)
        self.assertEqual(events[0].event, "stopped")
        metadata_path = self.root / ".vibe/local-runner" / (handle.run_id + ".json")
        metadata = metadata_path.read_text(encoding="utf-8")
        self.assertIn('"exit_status"', metadata)
        self.assertIn('"pid"', metadata)
        self.assertNotIn("stdout", metadata)
        self.assertNotIn("stderr", metadata)
        self.assertNotIn("token", metadata.casefold())

    def test_local_runner_stop_fails_closed_when_persisted_identity_is_stale(self):
        from vibe_guide.contracts import RunHandle
        from vibe_guide.runners.local import LocalRunner, _process_start_token

        command = [sys.executable, "-c", "import time; time.sleep(5)"]
        runner = LocalRunner(
            confirmed_commands={"fixture-agent": command}
        )
        handle = runner.start(
            {
                "adapter_id": "fixture-agent",
                "command": command,
                "node_id": "stale-identity",
                "role": "developer",
                "task_id": "developer:slow-stale-identity",
                "generation": 1,
            },
            self.root,
        )
        metadata_path = self.root / ".vibe/local-runner" / (handle.run_id + ".json")
        result_path = metadata_path.with_name(handle.run_id + ".result.json")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        original_identity = metadata["process_identity"]
        pid = metadata["pid"]
        metadata["process_identity"] = "tampered-or-reused-process-identity"
        metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

        try:
            with self.assertRaisesRegex(ValueError, "identity"):
                runner.stop(handle)
            self.assertEqual(_process_start_token(pid), original_identity)
            self.assertFalse(result_path.exists())
        finally:
            metadata["process_identity"] = original_identity
            metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
            runner.stop(RunHandle(handle.run_id))
            runner.poll(RunHandle(handle.run_id))

    def test_local_runner_process_b_reattaches_without_duplicate_writer(self):
        command = [
            sys.executable,
            "-c",
            (
                "import json,time; time.sleep(0.3); "
                "print(json.dumps({'event':'complete','data':{'evidence':'done'}}))"
            ),
        ]
        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process_a = context.Process(
            target=_start_local_runner_in_process,
            args=(str(self.root), command, result_queue),
        )
        process_a.start()
        handle_id = result_queue.get(timeout=10)
        process_a.join(timeout=10)
        self.assertEqual(process_a.exitcode, 0)

        from vibe_guide.contracts import RunHandle
        from vibe_guide.runners.local import LocalRunner

        process_b = LocalRunner(
            {"fixture-agent": command}, roots=[self.root]
        )
        events = []
        for _ in range(100):
            events = process_b.poll(RunHandle(handle_id))
            if events:
                break
            time.sleep(0.05)

        self.assertEqual([event.event for event in events], ["complete"])
        self.assertEqual(events[0].data["node_id"], "cross-process")
        self.assertEqual(process_b.start_count, 0)


if __name__ == "__main__":
    unittest.main()
