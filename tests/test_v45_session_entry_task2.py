import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.planner import TaskContext, route_task
from vibe_guide.session_entry import (
    build_session_entry,
    default_s1_context,
    stable_plan_id,
)


class V45SessionEntryTask2Tests(unittest.TestCase):
    def test_stable_defaults_are_repeatable_and_do_not_require_plan_fields(self):
        request = "设计并实现一个支付系统，编写测试并部署"
        first = build_session_entry(request)
        second = build_session_entry(request)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(first.plan_id, stable_plan_id(request))
        self.assertTrue(first.node_spec["nodes"])
        self.assertNotIn("provider_task", first.node_spec)
        self.assertNotIn("writer", first.node_spec["nodes"][0])

    def test_valid_explicit_s1_takes_precedence_over_s0_heuristic(self):
        entry = build_session_entry("设计并实现一个系统", s1="1,1,1,1,1")
        self.assertEqual(entry.s1.total, 5)
        self.assertEqual(entry.route.route, "simple")

    def test_default_s1_context_is_bounded(self):
        context = default_s1_context("设计并实现一个系统，编写测试并部署")
        self.assertIsInstance(context, TaskContext)
        self.assertTrue(all(0 <= value <= 5 for value in (
            context.steps, context.domains, context.uncertainty,
            context.failure_cost, context.toolchain,
        )))

    def test_explicitly_complex_request_reaches_complex_without_s1(self):
        entry = build_session_entry("设计并实现支付系统，迁移数据、集成接口、编写测试并部署")
        self.assertGreater(entry.s1.total, 15)
        self.assertEqual(entry.route.route, "complex")

    def test_empty_request_is_rejected(self):
        with self.assertRaises(ValueError):
            build_session_entry("")

    def test_cli_complex_request_without_plan_id_or_node_spec_is_accepted(self):
        from vibe_guide.cli import run_cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".project-root").write_text("fixture\n", encoding="utf-8")
            result = run_cli(["plan", "--request", "设计并实现一个系统并编写测试并部署", "--json"], root)
            self.assertEqual(result.exit_code, 0)
            self.assertIn(result.payload["route"], {"simple", "light_plan", "complex"})
            self.assertIn("plan_id", result.payload)
            self.assertIn("node_spec", result.payload)
            if result.payload["route"] == "complex":
                artifact_root = root / result.payload["materialized_path"]
                self.assertTrue((artifact_root / "plan.json").is_file())
                self.assertTrue((artifact_root / "node-spec.json").is_file())

    def test_materialization_does_not_overwrite_non_draft_plan(self):
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.session_entry import materialize_session_entry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root)
            entry = build_session_entry("设计并实现一个系统并编写测试并部署")
            target = paths.resolve_vibe_path(Path("plans") / entry.plan_id)
            target.mkdir(parents=True)
            (target / "plan.json").write_text(json.dumps({"status": "authorized"}), encoding="utf-8")
            with self.assertRaises(ValueError):
                materialize_session_entry(paths, entry)

    def test_materialization_rejects_symlinked_plan_directory(self):
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.session_entry import materialize_session_entry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = ProjectPaths(root)
            entry = build_session_entry("设计并实现一个系统并编写测试并部署")
            plans = paths.vibe / "plans"
            plans.mkdir(parents=True)
            redirected = root / "redirected"
            redirected.mkdir()
            (plans / entry.plan_id).symlink_to(redirected, target_is_directory=True)
            with self.assertRaises(ValueError):
                materialize_session_entry(paths, entry)

    def test_materialization_rejects_symlinked_vibe_root(self):
        from vibe_guide.paths import ProjectPaths
        from vibe_guide.session_entry import materialize_session_entry
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            redirected = root / "redirected"
            redirected.mkdir()
            (root / ".vibe").symlink_to(redirected, target_is_directory=True)
            paths = ProjectPaths(root)
            entry = build_session_entry("设计并实现一个系统并编写测试并部署")
            with self.assertRaises(ValueError):
                materialize_session_entry(paths, entry)

    def test_cli_rejects_invalid_plan_id_and_empty_request(self):
        from vibe_guide.cli import run_cli
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = run_cli(["plan", "--request", "实现功能", "--plan-id", "../bad", "--json"], root)
            empty = run_cli(["plan", "--json"], root)
        self.assertEqual(invalid.exit_code, 3)
        self.assertEqual(invalid.payload["status"], "blocked")
        self.assertEqual(empty.exit_code, 3)
        self.assertEqual(empty.payload["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
