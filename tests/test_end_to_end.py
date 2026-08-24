import json
import shutil
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "e2e-project"


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


if __name__ == "__main__":
    unittest.main()
