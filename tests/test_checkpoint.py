import tempfile
import unittest
import ast
from typing import Any
from types import SimpleNamespace
from unittest.mock import patch
from pathlib import Path

from vibe_guide.checkpoint import (
    ContextBudgetEstimator,
    ContextBudgetPolicy,
    MonitorCheckpoint,
    load_checkpoint,
    write_checkpoint,
)


class Tokenizer:
    def encode(self, value):
        return list(value)


class CheckpointContractTests(unittest.TestCase):
    def test_unknown_limit_policy_is_blocked_unknown(self):
        estimate = ContextBudgetEstimator(
            ContextBudgetPolicy("observed-model-limit")
        ).estimate("system", "input", "events", "checkpoint", "output")
        self.assertEqual(estimate.status, "blocked_unknown")

    def test_cli_monitor_construction_carries_context_policy(self):
        source = Path(__file__).resolve().parents[1] / "vibe_guide" / "cli.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Monitor"
        ]
        self.assertGreaterEqual(len(calls), 3)
        self.assertTrue(all(
            any(keyword.arg == "context_policy" for keyword in call.keywords)
            for call in calls
        ))

    def test_reauthorize_dispatch_paths_are_budget_gated(self):
        source = Path(__file__).resolve().parents[1] / "vibe_guide" / "monitor.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "reauthorize"
        )
        schedule_calls = [
            node for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_schedule_ready"
        ]
        gate_calls = [
            node for node in ast.walk(method)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_context_allows_dispatch"
        ]
        self.assertEqual(len(schedule_calls), 2)
        self.assertGreaterEqual(len(gate_calls), 2)

    def test_runner_context_budget_exception_defaults_to_unknown_policy(self):
        source = Path(__file__).resolve().parents[1] / "vibe_guide" / "cli.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_context_policy_for_runner"
        )
        namespace = {"ContextBudgetPolicy": ContextBudgetPolicy, "Any": Any}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

        class RaisingRunner:
            def context_budget(self):
                raise RuntimeError("provider observation failed")

        policy = namespace["_context_policy_for_runner"](RaisingRunner())
        self.assertEqual(policy.context_limit_tokens, "observed-model-limit")

    def test_cli_malformed_context_budget_does_not_fall_back_to_explicit_limit(self):
        source = Path(__file__).resolve().parents[1] / "vibe_guide" / "cli.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_context_policy_for_runner"
        )
        namespace = {"ContextBudgetPolicy": ContextBudgetPolicy, "Any": Any}
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

        class MalformedRunner:
            context_limit_tokens = 10000

            def context_budget(self):
                return ["unverifiable"]

        policy = namespace["_context_policy_for_runner"](MalformedRunner())
        self.assertEqual(policy.context_limit_tokens, "observed-model-limit")

    def test_monitor_context_budget_observation_is_exception_guarded(self):
        source = Path(__file__).resolve().parents[1] / "vibe_guide" / "monitor.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        method = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_context_estimate"
        )
        guarded = False
        for try_node in [node for node in ast.walk(method) if isinstance(node, ast.Try)]:
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "supplied"
                for node in ast.walk(try_node)
            ):
                guarded = True
                break
        self.assertTrue(guarded)

    def test_monitor_explicit_policy_is_overridden_on_observation_exception(self):
        source = Path(__file__).resolve().parents[1] / "vibe_guide" / "monitor.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_context_estimate"
        )
        namespace = {
            "ContextBudgetPolicy": ContextBudgetPolicy,
            "ContextBudgetEstimator": ContextBudgetEstimator,
            "load_events": lambda *_args: [],
            "json": __import__("json"),
            "RunSnapshot": object,
            "Runner": object,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)

        class RaisingRunner:
            context_input = {
                "system_prompt": "",
                "current_input": "",
                "event_summary": "",
                "checkpoint": "",
                "expected_output": "",
            }

            def context_budget(self):
                raise RuntimeError("provider observation failed")

        fake_self = SimpleNamespace(
            context_policy=ContextBudgetPolicy(10000),
            paths=SimpleNamespace(root=Path("/tmp")),
        )
        estimate = namespace["_context_estimate"](
            fake_self, SimpleNamespace(run_id="run-1"), RaisingRunner()
        )
        self.assertEqual(estimate.status, "blocked_unknown")

    def test_monitor_explicit_policy_is_overridden_on_malformed_observation(self):
        source = Path(__file__).resolve().parents[1] / "vibe_guide" / "monitor.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        function = next(
            node for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_context_estimate"
        )
        namespace = {
            "ContextBudgetPolicy": ContextBudgetPolicy,
            "ContextBudgetEstimator": ContextBudgetEstimator,
            "load_events": lambda *_args: [],
            "json": __import__("json"),
            "RunSnapshot": object,
            "Runner": object,
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"), namespace)
        fake_self = SimpleNamespace(
            context_policy=ContextBudgetPolicy(10000),
            paths=SimpleNamespace(root=Path("/tmp")),
        )
        for malformed in (
            [],
            "invalid",
            {"context_limit_tokens": "invalid"},
            {"reserve_tokens": 2},
            {"context_limit_tokens": 10000, "warning_ratio": "invalid"},
        ):
            class MalformedRunner:
                context_input = {
                    "system_prompt": "",
                    "current_input": "",
                    "event_summary": "",
                    "checkpoint": "",
                    "expected_output": "",
                }

                def context_budget(self, value=malformed):
                    return value

            estimate = namespace["_context_estimate"](
                fake_self, SimpleNamespace(run_id="run-1"), MalformedRunner()
            )
            self.assertEqual(estimate.status, "blocked_unknown")

    def test_monitor_gate_blocks_dispatch_and_records_checkpoint_for_malformed_observation(self):
        source = Path(__file__).resolve().parents[1] / "vibe_guide" / "monitor.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        methods = {
            node.name: node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name in {"_context_estimate", "_context_allows_dispatch"}
        }
        namespace = {
            "ContextBudgetPolicy": ContextBudgetPolicy,
            "ContextBudgetEstimator": ContextBudgetEstimator,
            "load_events": lambda *_args: [],
            "json": __import__("json"),
            "RunSnapshot": object,
            "Runner": object,
        }
        exec(
            compile(
                ast.Module(body=[methods["_context_estimate"]], type_ignores=[]),
                str(source),
                "exec",
            ),
            namespace,
        )
        exec(
            compile(
                ast.Module(body=[methods["_context_allows_dispatch"]], type_ignores=[]),
                str(source),
                "exec",
            ),
            namespace,
        )

        class MalformedRunner:
            context_input = {
                "system_prompt": "",
                "current_input": "",
                "event_summary": "",
                "checkpoint": "",
                "expected_output": "",
            }

            def context_budget(self):
                return ["not", "a", "budget"]

        checkpoint_calls = []
        dispatch_calls = []
        fake_monitor = SimpleNamespace(
            context_policy=ContextBudgetPolicy(10000),
            paths=SimpleNamespace(root=Path("/tmp")),
        )
        fake_monitor._context_estimate = lambda snapshot, runner: namespace[
            "_context_estimate"
        ](fake_monitor, snapshot, runner)
        fake_monitor._checkpoint_context = (
            lambda snapshot, reason, exhausted=False, estimate=None: checkpoint_calls.append(
                (reason, exhausted, estimate.status)
            )
        )
        fake_monitor._record = lambda *args, **kwargs: None
        fake_monitor._schedule_ready = lambda *args, **kwargs: dispatch_calls.append(args)

        snapshot = SimpleNamespace(run_id="run-1")
        runner = MalformedRunner()
        allowed = namespace["_context_allows_dispatch"](fake_monitor, snapshot, runner)
        if allowed:
            fake_monitor._schedule_ready(snapshot, runner)

        self.assertFalse(allowed)
        self.assertEqual(dispatch_calls, [])
        self.assertEqual(
            checkpoint_calls,
            [("context budget status: blocked_unknown", True, "blocked_unknown")],
        )

    def test_estimate_counts_all_context_sections_with_tokenizer(self):
        estimate = ContextBudgetEstimator(ContextBudgetPolicy(100, 20)).estimate(
            "sys", "input", "events", "checkpoint", "output", Tokenizer()
        )
        self.assertEqual(estimate.source, "tokenizer")
        self.assertEqual(estimate.total_tokens, 30)
        self.assertEqual(estimate.breakdown["event_summary"], 6)

    def test_fallback_is_conservative_and_records_method(self):
        estimate = ContextBudgetEstimator(ContextBudgetPolicy(100, 20)).estimate(
            "abcd", "中文", "", "", "", None
        )
        self.assertEqual(estimate.source, "conservative_characters")
        self.assertEqual(estimate.breakdown["system_prompt"], 1)
        self.assertEqual(estimate.breakdown["current_input"], 2)

    def test_thresholds_are_explicit(self):
        estimator = ContextBudgetEstimator(ContextBudgetPolicy(100, 20))
        self.assertEqual(estimator.classify(69), "normal")
        self.assertEqual(estimator.classify(70), "warning")
        self.assertEqual(estimator.classify(80), "checkpoint")
        self.assertEqual(estimator.classify(90), "hard_stop")
        self.assertEqual(estimator.classify(60, next_action_tokens=30), "checkpoint")

    def test_explicit_reserve_cannot_exceed_verified_limit(self):
        with self.assertRaises(ValueError):
            ContextBudgetPolicy(100, 101)

    def test_unverified_limit_is_blocked_unknown(self):
        estimate = ContextBudgetEstimator(
            ContextBudgetPolicy("observed-model-limit", 20)
        ).estimate("a", "b", "c", "d", "e")
        self.assertEqual(estimate.status, "blocked_unknown")
        self.assertIsNone(estimate.limit_tokens)

    def test_checkpoint_round_trip_and_corrupt_current_falls_back(self):
        with tempfile.TemporaryDirectory() as root:
            paths = Path(root)
            first = MonitorCheckpoint(
                run_id="run-1", plan_revision="plan-1@1", state_version=1,
                last_event_seq=2, next_action="poll", stop_conditions=["one writer"],
                authorization_digest="a" * 64, handles={"n1": "h1"},
            )
            second = MonitorCheckpoint(
                run_id="run-1", plan_revision="plan-1@1", state_version=1,
                last_event_seq=3, next_action="resume", stop_conditions=["one writer"],
                authorization_digest="a" * 64, handles={"n1": "h1"},
            )
            write_checkpoint(paths, first)
            self.assertEqual(load_checkpoint(paths, "run-1").last_event_seq, 2)
            write_checkpoint(paths, second)
            current = paths / ".vibe" / "runs" / "run-1" / "monitor_checkpoint.json"
            current.write_text("{broken", encoding="utf-8")
            self.assertEqual(load_checkpoint(paths, "run-1").last_event_seq, 2)

    def test_checkpoint_digest_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            paths = Path(root)
            checkpoint = MonitorCheckpoint("run-1", "plan-1@1", 1, 1, "poll", ["one writer"])
            write_checkpoint(paths, checkpoint)
            path = paths / ".vibe" / "runs" / "run-1" / "monitor_checkpoint.json"
            text = path.read_text(encoding="utf-8").replace(checkpoint.sha256, "0" * 64)
            path.write_text(text, encoding="utf-8")
            with self.assertRaises(ValueError):
                load_checkpoint(paths, "run-1")

    def test_resume_rejects_plan_revision_drift(self):
        with tempfile.TemporaryDirectory() as root:
            paths = Path(root)
            checkpoint = MonitorCheckpoint(
                run_id="run-1", plan_revision="plan-old@1", state_version=1,
                last_event_seq=1, next_action="resume", stop_conditions=["one writer"],
                handles={}, nodes={},
            )
            write_checkpoint(paths, checkpoint)
            snapshot = SimpleNamespace(
                schema_version=1,
                event_sequence=1,
                handles={},
                nodes={},
                plan_id="plan-current",
                plan_version=1,
                authorization_digest="",
                node_contract_digest="",
                capability_contract_digest="",
            )
            fake_state = SimpleNamespace(load_snapshot=lambda _paths, _run_id: snapshot)
            with patch.dict("sys.modules", {"vibe_guide.state": fake_state}):
                with self.assertRaises(ValueError):
                    from vibe_guide.checkpoint import resume_from_checkpoint
                    resume_from_checkpoint(paths, "run-1")


if __name__ == "__main__":
    unittest.main()
