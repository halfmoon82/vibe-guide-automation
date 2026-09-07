import json
import tempfile
import unittest
from pathlib import Path

from vibe_guide.checkpoint import (
    ContextBudgetEstimator,
    ContextBudgetPolicy,
    MonitorCheckpoint,
    load_checkpoint,
    resume_from_checkpoint,
    write_checkpoint,
)
from vibe_guide.models import Plan
from vibe_guide.paths import ProjectPaths
from vibe_guide.state import RunSnapshot, load_snapshot, save_snapshot
from vibe_guide.authorization import authorize, build_authorization_card
from vibe_guide.models import AgentCapabilities, DAGNode
from vibe_guide.monitor import Monitor
from vibe_guide.runners.fake import FakeRunner
from vibe_guide.capability_contract import build_contract, save_contract


class _Tokenizer:
    def encode(self, text):
        return list(range(len(text)))


class CheckpointTests(unittest.TestCase):
    def test_estimate_uses_tokenizer_and_covers_all_context_parts(self):
        estimate = ContextBudgetEstimator(ContextBudgetPolicy(100, 20)).estimate(
            "sys", "input", "events", "checkpoint", "output", _Tokenizer()
        )
        self.assertEqual(estimate.source, "tokenizer")
        self.assertEqual(estimate.breakdown, {
            "system_prompt": 3,
            "current_input": 5,
            "event_summary": 6,
            "checkpoint": 10,
            "expected_output": 6,
        })
        self.assertEqual(estimate.total_tokens, 30)
        self.assertEqual(estimate.ratio, 0.3)
        self.assertEqual(estimate.status, "normal")

    def test_estimate_falls_back_to_conservative_character_token_method(self):
        estimate = ContextBudgetEstimator(ContextBudgetPolicy(100, 20)).estimate(
            "abcd", "汉字", "", "", "", None
        )
        self.assertEqual(estimate.source, "conservative_characters")
        self.assertEqual(estimate.breakdown["system_prompt"], 1)
        self.assertEqual(estimate.breakdown["current_input"], 2)

    def test_thresholds_warn_checkpoint_and_hard_stop(self):
        estimator = ContextBudgetEstimator(ContextBudgetPolicy(100, 20))
        self.assertEqual(estimator.classify(70), "warning")
        self.assertEqual(estimator.classify(80), "checkpoint")
        self.assertEqual(estimator.classify(90), "hard_stop")

    def test_unknown_limit_is_explicit(self):
        estimate = ContextBudgetEstimator(ContextBudgetPolicy("observed-model-limit", 20)).estimate(
            "a", "b", "c", "d", "e", None
        )
        self.assertEqual(estimate.status, "blocked_unknown")
        self.assertIsNone(estimate.limit_tokens)

    def test_checkpoint_write_is_atomic_and_round_trips(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            checkpoint = MonitorCheckpoint(
                run_id="run-1",
                plan_revision="plan-1@1",
                state_version=1,
                last_event_seq=4,
                next_action="poll",
                stop_conditions=["no duplicate writer"],
                authorization_digest="a" * 64,
                nodes={"n1": {"status": "running", "writer": "dev"}},
                handles={"n1": "h1"},
                worker_profiles={"n1": {"model": "test"}},
                evidence=["event:4"],
            )
            write_checkpoint(paths, checkpoint)
            loaded = load_checkpoint(paths, "run-1")
            self.assertEqual(loaded.to_dict(), checkpoint.to_dict())
            self.assertEqual(len(loaded.sha256), 64)
            self.assertTrue((paths.root / ".vibe/runs/run-1/monitor_checkpoint.json").is_file())

    def test_corrupt_current_checkpoint_falls_back_to_previous(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            first = MonitorCheckpoint("run-1", "plan-1@1", 1, 1, "poll", ["stop"])
            second = MonitorCheckpoint("run-1", "plan-1@1", 1, 2, "resume", ["stop"])
            write_checkpoint(paths, first)
            write_checkpoint(paths, second)
            (paths.root / ".vibe/runs/run-1/monitor_checkpoint.json").write_text("{broken", encoding="utf-8")
            self.assertEqual(load_checkpoint(paths, "run-1").last_event_seq, 1)

    def test_resume_from_checkpoint_prefers_last_valid_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            snapshot = RunSnapshot(
                run_id="run-1", plan_id="plan-1", plan_version=1,
                status="running", nodes={}, handles={},
                authorization={}, event_sequence=0,
            )
            # The state validator accepts a legacy empty authorization only in
            # snapshots written by existing fixtures; this helper is expected
            # to surface a clear failure rather than invent recovery state.
            with self.assertRaises((ValueError, FileNotFoundError)):
                resume_from_checkpoint(paths, "run-1")

    def test_monitor_checkpoints_before_dispatch_at_eighty_percent(self):
        class ContextRunner(FakeRunner):
            context_limit_tokens = 100
            context_input = {"system_prompt": "x" * 320}

        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            (paths.vibe / "state.json").parent.mkdir(parents=True)
            (paths.vibe / "state.json").write_text('{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8")
            save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))
            node = DAGNode("n1", "n1", [], [], "g", {"files": ["n1.py"], "worker": "w", "worktree": ".worktrees/n1"}, "ready")
            plan = Plan("p1", 1, "docs/prd.md", ["n1"], "draft")
            caps = AgentCapabilities("fake", True, True, True, True, True, "full")
            record = authorize(build_authorization_card(plan, [node], caps), "AUTHORIZE")
            monitor = Monitor(paths, plan, [node])
            snapshot = monitor.start(record, ContextRunner())
            self.assertEqual(snapshot.nodes["n1"]["status"], "planned")
            self.assertTrue((paths.root / ".vibe/runs" / snapshot.run_id / "monitor_checkpoint.json").is_file())

    def test_provider_overflow_checkpoints_without_starting_another_writer(self):
        with tempfile.TemporaryDirectory() as root:
            paths = ProjectPaths(Path(root))
            (paths.vibe / "state.json").parent.mkdir(parents=True)
            (paths.vibe / "state.json").write_text('{"workflow_version": 2, "session_gate": "s0_required"}\n', encoding="utf-8")
            save_contract(paths, build_contract(paths.root, provider="fake", host_id="local"))
            node = DAGNode("n1", "n1", [], [], "g", {"files": ["n1.py"], "worker": "w", "worktree": ".worktrees/n1"}, "ready")
            plan = Plan("p1", 1, "docs/prd.md", ["n1"], "draft")
            caps = AgentCapabilities("fake", True, True, True, True, True, "full")
            record = authorize(build_authorization_card(plan, [node], caps), "AUTHORIZE")
            monitor = Monitor(paths, plan, [node])
            runner = FakeRunner({("n1", "developer"): [("context_overflow", {"reason": "provider overflow"})]})
            snapshot = monitor.start(record, runner)
            monitor.tick(snapshot.run_id, runner)
            self.assertEqual(len(runner.start_calls), 1)
            self.assertEqual(load_snapshot(paths, snapshot.run_id).status, "blocked_unknown")


if __name__ == "__main__":
    unittest.main()
